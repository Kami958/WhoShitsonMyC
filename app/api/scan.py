"""扫描（后台线程 + 进度推送）（JS-API Mixin）。"""

from __future__ import annotations

import gc
import os
import threading
import time

from core import applog, i18n, store
from core.compress import CompressError, compress_db
from app.bootstrap import _data_dir_unavailable_message, _snapshot_dir_error_message
from core.mft.scan import is_mft_eligible
from core.packenv import check_pack_env
from core.scanner import ScanCancelled, scan_to_snapshot
from core.snapshots_store import SnapshotInfo
from core.timing_probe import start_scan_timer


# 延迟回收的代次：连续扫两次时，上一轮排队的回收要让位，
# 否则它会在新一次扫描正忙时做完整回收，反而制造卡顿。
_gc_lock = threading.Lock()
_gc_token = 0


def _collect_garbage_later(delay: float = 2.0) -> None:
    """扫描线程退出后再回收一次内存。

    扫描期间为了不被回收打断关掉了自动回收，重新打开开关并不会立刻
    收一次，那一段积累的临时对象会一直占着内存。这里等界面把结果
    展示完再收，既不拖慢收尾，也不会在用户眼前卡一下。
    """
    global _gc_token
    with _gc_lock:
        _gc_token += 1
        token = _gc_token

    def run() -> None:
        time.sleep(delay)
        with _gc_lock:
            if token != _gc_token:
                # 已经开了新一次扫描，这一轮的回收交给新的去做
                return
        try:
            gc.collect()
        except Exception as exc:
            applog.exception("扫描结束后回收内存失败", exc)

    threading.Thread(target=run, daemon=True, name="wsmc-gc").start()


def _cancel_pending_gc() -> None:
    """扫描开始时作废上一轮排队的回收。

    上一轮扫描排队的回收可能正好在本次扫描中途醒来，那时做一次完整回收
    会把用户正等着的这次扫描卡一下。推进代次让它直接退出。
    """
    global _gc_token
    with _gc_lock:
        _gc_token += 1


def _scan_snapshot_payload(path: str, meta, *, compressed: bool) -> dict:
    """把刚扫完的元信息拼成与快照列表同一套字段。

    两处各写一份同名字段，早晚会漏掉某个新字段，于是界面上“刚扫完这一行”
    和“重新列一遍”长得不一样。统一走同一个摘要对象，字段集天然一致。
    """
    info = SnapshotInfo(
        path=path,
        root=meta.root,
        scanned_at=meta.scanned_at,
        total_size=meta.total_size,
        file_count=meta.file_count,
        skipped_count=len(meta.skipped),
        compressed=compressed,
        format_version=meta.format_version,
        actual_size_total=meta.actual_size_total,
        actual_size_recorded=bool(meta.actual_size_recorded),
        actual_size_unrecorded=int(meta.actual_size_unrecorded or 0),
    )
    return info.to_dict()


class ScanMixin:
    """发起/取消扫描，线程体内推 scan-progress / scan-done 等事件。"""

    def start_scan(
        self, root: str, follow_symlinks: bool = False, no_parallel: bool = False
    ) -> dict:
        """在后台线程扫描 ``root``，进度与结果通过事件推送。

        事件：``scan-progress`` {files, current} /
        ``scan-done`` {snapshot, elapsed_s} /
        ``scan-error`` {message} / ``scan-cancelled`` {}。

        Args:
            root: 扫描根目录。
            follow_symlinks: 是否跟随符号链接/重解析点。
            no_parallel: 跳过快速解析，直接走目录遍历；用户在解包环境
                缺失的提示里选择「单进程处理」时传 True。

        Returns:
            立即返回 ``{"started": True}``；已有扫描在跑则返回 error。
            打包环境下展开目录读不出来时返回 ``{"error": ...,
            "pack_env_error": ...}``，前端据此弹出重启或降级的提示。
        """
        if self._scan_thread and self._scan_thread.is_alive():
            return {"error": i18n.t(
                "已有扫描正在进行", "A scan is already in progress")}
        if not os.path.isdir(root):
            return {"error": i18n.t(
                f"目录不存在：{root}", f"Folder does not exist: {root}")}
        # 只有这次真的会启用多进程解析时才需要检查展开目录：扫普通目录、
        # 非管理员、非 NTFS、或设置里关掉快速扫描时都只走多线程，展开目录
        # 坏了也不影响。判不出来按「不需要」处理，不因此拦住扫描。
        needs_pack = False
        if not no_parallel:
            try:
                needs_pack = is_mft_eligible(root)
            except Exception as exc:  # noqa: BLE001 - 判定失败不拦扫描
                applog.warn(
                    f"mft eligibility check failed | reason={type(exc).__name__}"
                )
        if needs_pack:
            # 打包运行时展开目录若读不出来，新起的解析进程会读不到标准库
            # 而各弹一个错误框，且父进程收不到任何结果。这里先行探测，
            # 读不到就不启动扫描，把情况交给前端让用户选重启还是降级。
            pack_ok, pack_detail = check_pack_env()
            if not pack_ok:
                applog.warn(
                    f"pack env unreadable before scan | detail={pack_detail}"
                )
                return {
                    "error": i18n.t(
                        "程序解包环境缺失，无法启动并行扫描。",
                        "The program's unpacked environment is missing; "
                        "parallel scanning cannot start.",
                    ),
                    "pack_env_error": pack_detail,
                }
        status = store.storage_status()
        if status.get("snapshot_dir_unavailable"):
            return {
                "error": _snapshot_dir_error_message(
                    str(status.get("reason") or "")
                )
            }
        if not status.get("ok"):
            return {"error": _data_dir_unavailable_message(
                str(status.get("path") or ""),
                str(status.get("reason") or ""),
            )}

        self._cancel.clear()
        # 上一次扫描排队的回收可能正好在本次扫描中途醒来，先作废它
        _cancel_pending_gc()
        # 不记录 root 路径（隐私）；只记并发度等元数据。
        # workers 仅表示常规目录扫描线程数；MFT 解析进程数另按核/活量自推导。
        applog.info(
            f"Scan starting workers={store.get_scan_workers()} "
            f"compress={store.get_compress_snapshots()} "
            f"mft={store.get_use_mft()} follow_symlinks={bool(follow_symlinks)}"
        )
        self._scan_thread = threading.Thread(
            target=self._run_scan, args=(root, follow_symlinks, no_parallel), daemon=True
        )
        self._scan_thread.start()
        return {"started": True}

    def cancel_scan(self) -> dict:
        """请求取消正在进行的扫描。"""
        self._cancel.set()
        return {"ok": True}

    def _run_scan(self, root: str, follow_symlinks: bool, no_parallel: bool) -> None:
        """后台线程体：执行扫描、回报进度、发终态事件。"""
        db_path = ""
        final_path = ""
        workers = store.get_scan_workers()
        do_compress = store.get_compress_snapshots()
        # 整次扫描墙钟（含可选压缩），用于完成提示「用时」
        t_wall0 = time.perf_counter()
        # 分段计时：exe / 未开 DEBUG·WSMC_SCAN_TIMING 时为空操作；汇总进 applog。
        timer = start_scan_timer(
            root=root, workers=workers, compress_enabled=do_compress
        )

        def discard_partial() -> None:
            """扫描未完成时丢弃半成品；清理失败不能盖住真正的扫描错误。"""
            if not db_path:
                return
            try:
                store.delete_snapshot(db_path, permanent=True)
            except Exception as exc:  # noqa: BLE001
                applog.exception("discard partial snapshot failed", exc)

        # 生成快照路径也必须在 try 内：数据目录不可创建时先发 scan-error，
        # 让前端关闭扫描遮罩；不能让扫描线程静默死亡。
        try:
            db_path = store.new_snapshot_path(root)
            final_path = db_path
        except Exception as exc:  # noqa: BLE001
            applog.exception("create snapshot path failed", exc)
            timer.finish(status="error")
            self._emit("scan-error", {
                "message": i18n.t(
                    "无法创建快照文件。请确认快照存放目录可用，或在设置中选择其他文件夹后重试。",
                    "Cannot create the snapshot file. Check that the snapshot storage folder is usable, or choose another folder in Settings and try again.",
                )
            })
            return

        # 扫描线程内再兜底节流一层（scanner 已节流；压缩阶段等直接 emit 不受影响）
        _prog_lock = threading.Lock()
        _last_emit = [0.0]
        _PROG_GAP = 0.18

        def on_progress(files: int, current: str) -> None:
            now = time.perf_counter()
            with _prog_lock:
                if now - _last_emit[0] < _PROG_GAP:
                    return
                _last_emit[0] = now
            self._emit(
                "scan-progress",
                {"files": int(files), "current": current or ""},
            )

        # 按盘参数：这个盘单独设过就用它，没设过回落到全局扫描线程与自动进程数。
        # 目录遍历用其中的线程数，读 MFT 用其中的读取线程与处理进程数，
        # 同一次扫描只会走其中一条，另一项不参与。
        perf = store.get_drive_effective(root)
        workers = perf.get("dir_threads") or workers
        mft_read = perf.get("mft_read") or None
        # 解析进程数：设置里为 0 表示按核数自动，交给扫描链路自己推导
        mft_procs = perf.get("mft_procs") or None

        try:
            timer.span_start("scan_to_snapshot")
            try:
                meta = scan_to_snapshot(
                    root,
                    db_path,
                    follow_symlinks=follow_symlinks,
                    progress=on_progress,
                    cancel=self._cancel.is_set,
                    workers=workers,
                    mft_procs=mft_procs,
                    mft_read=mft_read,
                    timer=timer,
                    no_mft=no_parallel,
                )
            finally:
                timer.span_end("scan_to_snapshot")
            try:
                timer.set_meta(db_bytes=os.path.getsize(db_path))
            except OSError:
                pass
            # 扫完可选压缩：失败时保留 .db，不把整次扫描判失败。
            if do_compress:
                self._emit("scan-progress", {
                    "files": meta.file_count,
                    "current": i18n.t("正在压缩快照", "Compressing snapshot"),
                })
                try:
                    timer.span_start("compress")
                    try:
                        final_path = compress_db(db_path, meta)
                    finally:
                        timer.span_end("compress")
                    try:
                        if final_path.lower().endswith(".dbz"):
                            timer.set_meta(dbz_bytes=os.path.getsize(final_path))
                    except OSError:
                        pass
                except CompressError as exc:
                    applog.exception(
                        f"Snapshot compress failed (file_count={meta.file_count})",
                        exc,
                    )
                    timer.finish(status="compress_failed")
                    elapsed_s = max(0.0, time.perf_counter() - t_wall0)
                    self._emit(
                        "scan-done",
                        {
                            "snapshot": _scan_snapshot_payload(
                                db_path, meta, compressed=False
                            ),
                            "elapsed_s": round(elapsed_s, 2),
                            "warning": i18n.t(
                                f"快照已保存，但压缩失败，已保留未压缩文件：{exc}",
                                f"Snapshot saved, but compression failed; kept uncompressed file: {exc}",
                            ),
                        },
                    )
                    # 压缩失败也是扫描结束，回收不能因为这个提前返回跳过一次
                    _collect_garbage_later()
                    return
        except ScanCancelled:
            discard_partial()  # 丢弃不完整快照
            timer.finish(status="cancelled")
            applog.info("Scan cancelled")
            self._emit("scan-cancelled", {})
        except Exception as exc:  # noqa: BLE001 - 兜底，任何异常都不该让线程静默死掉
            discard_partial()
            # 不写扫描 root / 当前路径，避免隐私与日志膨胀
            applog.exception("Scan failed", exc)
            timer.finish(status="error")
            self._emit("scan-error", {"message": str(exc)})
        else:
            timer.finish(status="ok")
            elapsed_s = max(0.0, time.perf_counter() - t_wall0)
            applog.info(
                f"Scan done file_count={meta.file_count} "
                f"total_size={meta.total_size} "
                f"skipped={len(meta.skipped)} "
                f"compressed={str(final_path).lower().endswith('.dbz')} "
                f"elapsed_s={elapsed_s:.2f}"
            )
            self._emit(
                "scan-done",
                {
                    "snapshot": _scan_snapshot_payload(
                        final_path, meta, compressed=final_path.lower().endswith(".dbz")
                    ),
                    "elapsed_s": round(elapsed_s, 2),
                },
            )

        # 三个分支（成功 / 取消 / 出错）都要收一次：扫描过程中关掉的自动回收
        # 会积累一批临时对象，不收就一直占着内存。放在分支之后，跟线程收尾
        # 一起跑，扫完的弹窗不受影响。
        _collect_garbage_later()

    # ---- 对比 / 下钻 ---------------------------------------------------
