"""扫描（后台线程 + 进度推送）（JS-API Mixin）。"""

from __future__ import annotations

import os
import threading
import time

from core import applog, i18n, store
from core.compress import CompressError, compress_db
from core.scanner import ScanCancelled, scan_to_snapshot
from core.timing_probe import start_scan_timer


class ScanMixin:
    """发起/取消扫描，线程体内推 scan-progress / scan-done 等事件。"""

    def start_scan(self, root: str, follow_symlinks: bool = False) -> dict:
        """在后台线程扫描 ``root``，进度与结果通过事件推送。

        事件：``scan-progress`` {files, current} /
        ``scan-done`` {snapshot, elapsed_s} /
        ``scan-error`` {message} / ``scan-cancelled`` {}。

        Returns:
            立即返回 ``{"started": True}``；已有扫描在跑则返回 error。
        """
        if self._scan_thread and self._scan_thread.is_alive():
            return {"error": i18n.t(
                "已有扫描正在进行", "A scan is already in progress")}
        if not os.path.isdir(root):
            return {"error": i18n.t(
                f"目录不存在：{root}", f"Folder does not exist: {root}")}

        self._cancel.clear()
        # 不记录 root 路径（隐私）；只记并发度等元数据。
        # workers 仅表示常规目录扫描线程数；MFT 解析进程数另按核/活量自推导。
        applog.info(
            f"Scan starting workers={store.get_scan_workers()} "
            f"compress={store.get_compress_snapshots()} "
            f"mft={store.get_use_mft()} follow_symlinks={bool(follow_symlinks)}"
        )
        self._scan_thread = threading.Thread(
            target=self._run_scan, args=(root, follow_symlinks), daemon=True
        )
        self._scan_thread.start()
        return {"started": True}

    def cancel_scan(self) -> dict:
        """请求取消正在进行的扫描。"""
        self._cancel.set()
        return {"ok": True}

    def _run_scan(self, root: str, follow_symlinks: bool) -> None:
        """后台线程体：执行扫描、回报进度、发终态事件。"""
        db_path = store.new_snapshot_path(root)
        final_path = db_path
        workers = store.get_scan_workers()
        do_compress = store.get_compress_snapshots()
        # 整次扫描墙钟（含可选压缩），用于完成提示「用时」
        t_wall0 = time.perf_counter()
        # 分段计时：exe / 未开 DEBUG·WSMC_SCAN_TIMING 时为空操作；汇总进 applog。
        timer = start_scan_timer(
            root=root, workers=workers, compress_enabled=do_compress
        )

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
                    timer=timer,
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
                            "snapshot": {
                                "path": db_path,
                                "root": meta.root,
                                "scanned_at": meta.scanned_at,
                                "total_size": meta.total_size,
                                "file_count": meta.file_count,
                                "skipped_count": len(meta.skipped),
                                "compressed": False,
                            },
                            "elapsed_s": round(elapsed_s, 2),
                            "warning": i18n.t(
                                f"快照已保存，但压缩失败，已保留未压缩文件：{exc}",
                                f"Snapshot saved, but compression failed; kept uncompressed file: {exc}",
                            ),
                        },
                    )
                    return
        except ScanCancelled:
            store.delete_snapshot(db_path, permanent=True)  # 丢弃不完整快照
            timer.finish(status="cancelled")
            applog.info("Scan cancelled")
            self._emit("scan-cancelled", {})
        except Exception as exc:  # noqa: BLE001 - 兜底，任何异常都不该让线程静默死掉
            store.delete_snapshot(db_path, permanent=True)
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
                    "snapshot": {
                        "path": final_path,
                        "root": meta.root,
                        "scanned_at": meta.scanned_at,
                        "total_size": meta.total_size,
                        "file_count": meta.file_count,
                        "skipped_count": len(meta.skipped),
                        "compressed": final_path.lower().endswith(".dbz"),
                    },
                    "elapsed_s": round(elapsed_s, 2),
                },
            )

    # ---- 对比 / 下钻 ---------------------------------------------------
