"""MFT 扫描入口：资格判断 + 编排读解析 / 建树 / 写快照。

重活在 :mod:`core.mft.pipeline`（读+紧凑解析）与 :mod:`core.mft.tree`（建树），
本模块只做进度、取消、timer、写库线程编排，不与 scandir 路径混写。
"""

from __future__ import annotations

import gc
import os
import queue
import struct
import threading
import time
from collections.abc import Callable

from ..i18n import t
from ..models import SnapshotMeta
from ..snapshot import EntryRow, SnapshotWriter
from .pipeline import read_and_parse_mft
from .tree import MftTreeError, build_entry_rows_from_compact
from .volume import (
    BootInfo,
    MftIoError,
    close_handle,
    is_ntfs_volume,
    open_volume,
    parse_boot,
    read_at,
)

ProgressCallback = Callable[[int, str], None]
CancelCheck = Callable[[], bool]

_PROGRESS_MIN_INTERVAL = 0.22
_ROWS_DONE = object()


class MftUnavailable(Exception):
    """当前根不能或不必走 MFT，调用方应回退常规扫描。"""


class _Progress:
    """把阶段文案 thrash 到现有 progress(files, current) 回调。"""

    __slots__ = ("_cb", "_last_t")

    def __init__(self, cb: ProgressCallback | None) -> None:
        self._cb = cb
        self._last_t = 0.0

    def emit(self, files: int, current: str, *, force: bool = False) -> None:
        if self._cb is None:
            return
        now = time.perf_counter()
        if not force and now - self._last_t < _PROGRESS_MIN_INTERVAL:
            return
        self._last_t = now
        self._cb(max(0, int(files)), current)


def _drive_letter_and_root(path: str) -> tuple[str, str] | None:
    """若 path 是盘符根，返回 ``('C', 'C:\\\\')``，否则 None。"""
    abspath = os.path.abspath(path)
    if abspath.startswith("\\\\?\\"):
        abspath = abspath[4:]
    drive, tail = os.path.splitdrive(abspath)
    if not drive or len(drive) < 2 or drive[1] != ":":
        return None
    letter = drive[0].upper()
    if not ("A" <= letter <= "Z"):
        return None
    rest = tail.replace("/", "\\").strip("\\")
    if rest != "":
        return None
    return letter, f"{letter}:\\"


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _is_admin() -> bool:
    """当前进程是否具备管理员权限（读 $MFT 必需）。

    非 Windows 返回 True（本模块本就不会在非 NT 上启用）。
    """
    if os.name != "nt":
        return True
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def is_mft_eligible(root: str) -> bool:
    """是否允许尝试 MFT：Windows、管理员、盘符根、NTFS，且设置/环境未关闭。

    默认 ``store.get_use_mft()`` 为开；``WSMC_DISABLE_MFT`` 强制关，
    ``WSMC_USE_MFT`` 可在未写配置时强制开。

    **非管理员一律不走 MFT**（设置勾选可保留；扫描侧回退常规目录遍历）。
    """
    if os.name != "nt":
        return False
    if _env_truthy("WSMC_DISABLE_MFT"):
        return False
    # 读 \\\\.\\C: 需要提升权限；非管理员直接不合格，避免开卷失败再回退
    if not _is_admin():
        return False
    enabled = _env_truthy("WSMC_USE_MFT")
    if not enabled:
        try:
            from ..store import get_use_mft

            enabled = bool(get_use_mft())
        except Exception:
            enabled = True  # 与 store 默认一致
    if not enabled:
        return False
    dr = _drive_letter_and_root(root)
    if dr is None:
        return False
    _letter, drive_root = dr
    try:
        return is_ntfs_volume(drive_root)
    except Exception:
        return False


def _fmt_mib(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MiB"


class _AsyncRowWriter:
    """独立线程写 SQLite：建树线程只往队列丢 batch。

    注意：``SnapshotWriter`` 连接须 ``check_same_thread=False``（见 snapshot），
    且 **finalize 必须在本写线程 join 之后、仍由调用方线程执行**——
    本类只负责 add_rows 的并发重叠，不跨线程 finalize。
    """

    def __init__(
        self,
        writer: SnapshotWriter,
        *,
        maxsize: int = 64,
    ) -> None:
        self._writer = writer
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._err: BaseException | None = None
        self._written = 0
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="mft-db-writer", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                item = self._q.get()
                if item is _ROWS_DONE:
                    return
                rows: list[EntryRow] = item
                self._writer.add_rows(rows)
                self._written += len(rows)
        except BaseException as exc:  # noqa: BLE001
            self._err = exc

    def submit(self, rows: list[EntryRow]) -> None:
        if self._err is not None:
            raise self._err
        if self._closed:
            raise RuntimeError("row writer already finished")
        self._q.put(rows)

    def finish(self) -> int:
        """投放结束标记并 join；返回已写行数。传播写线程异常。可重复调用。

        写线程已因异常退出且队列满时，阻塞 ``put`` 会挂死——有错则
        ``put_nowait``，失败直接 join 收尸。
        """
        if not self._closed:
            self._closed = True
            if self._err is None:
                try:
                    self._q.put(_ROWS_DONE, timeout=60)
                except Exception:  # noqa: BLE001
                    try:
                        self._q.put_nowait(_ROWS_DONE)
                    except Exception:  # noqa: BLE001
                        pass
            else:
                try:
                    self._q.put_nowait(_ROWS_DONE)
                except Exception:  # noqa: BLE001
                    pass
        if self._thread.is_alive():
            self._thread.join(timeout=300)
        if self._err is not None:
            raise self._err
        return self._written

    @property
    def written(self) -> int:
        return self._written


def scan_mft_to_snapshot(
    root: str,
    db_path: str,
    *,
    follow_symlinks: bool = False,
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
    now: float | None = None,
    timer: object | None = None,
    workers: int | None = None,
) -> SnapshotMeta:
    """用 MFT 扫描盘符根并写入 ``db_path``。

    阶段：pipeline 读 $MFT+多进程紧凑解析 → 建树并异步写 SQLite → 建索引。

    Raises:
        MftUnavailable: 不满足条件或读/解析失败（应回退）。
        ScanCancelled: 用户取消（与常规扫描相同）。
    """
    from ..scanner import ScanCancelled

    _mark = getattr(timer, "mark", None)
    _span_start = getattr(timer, "span_start", None)
    _span_end = getattr(timer, "span_end", None)
    _set_meta = getattr(timer, "set_meta", None)

    if not is_mft_eligible(root):
        raise MftUnavailable("not an NTFS drive root")

    letter, drive_root = _drive_letter_and_root(root)  # type: ignore[misc]
    meta = SnapshotMeta(root=drive_root, scanned_at=now or time.time())
    pg = _Progress(progress)

    if _mark:
        _mark("mft_start")

    handle = None
    mft_workers = 1
    try:
        pg.emit(
            0,
            t("MFT：打开卷", "MFT: opening volume"),
            force=True,
        )

        try:
            handle = open_volume(letter)
            boot_raw = read_at(handle, 0, 512)
            boot: BootInfo = parse_boot(boot_raw)
        except MftIoError as exc:
            raise MftUnavailable(str(exc)) from exc
        except (OSError, ValueError, struct.error) as exc:
            raise MftUnavailable(str(exc)) from exc

        def on_bytes(done: int, total: int) -> None:
            if cancel and cancel():
                raise MftIoError("cancelled")
            mib = done // (1024 * 1024)
            if total > 0:
                pct = min(100, int(done * 100 / total))
                msg = t(
                    f"MFT：读取元数据表 {pct}%（{_fmt_mib(done)} / {_fmt_mib(total)}）",
                    f"MFT: reading metadata {pct}% ({_fmt_mib(done)} / {_fmt_mib(total)})",
                )
            else:
                msg = t(
                    f"MFT：读取元数据表 {_fmt_mib(done)}",
                    f"MFT: reading metadata {_fmt_mib(done)}",
                )
            pg.emit(mib, msg)

        def on_parse(done: int, total: int) -> None:
            if cancel and cancel():
                raise InterruptedError("cancelled")
            pct = int(done * 100 / total) if total else 100
            pg.emit(
                done,
                t(
                    f"MFT：解析记录 {pct}%（{done}/{total}）",
                    f"MFT: parsing records {pct}% ({done}/{total})",
                ),
            )

        pg.emit(
            0,
            t("MFT：读取元数据表", "MFT: reading metadata"),
            force=True,
        )

        try:
            result = read_and_parse_mft(
                handle,
                boot,
                drive_letter=letter,
                workers=workers,
                on_bytes=on_bytes,
                on_parse=on_parse,
                cancel=cancel,
                timer=timer,
            )
        except MftIoError as exc:
            if str(exc) == "cancelled":
                raise ScanCancelled() from exc
            raise MftUnavailable(str(exc)) from exc
        except InterruptedError as exc:
            raise ScanCancelled() from exc
        except (OSError, ValueError, struct.error) as exc:
            raise MftUnavailable(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise MftUnavailable(f"parse failed: {exc}") from exc

        mft_workers = result.workers
        table = result.table
        nrec = result.n_records

        pg.emit(
            nrec,
            t(
                f"MFT：解析完成（{nrec}）",
                f"MFT: parse done ({nrec})",
            ),
            force=True,
        )
        pg.emit(
            0,
            t("MFT：构建目录树", "MFT: building tree"),
            force=True,
        )

        file_count = 0
        dir_count = 0
        total_size = 0

        if _span_start:
            _span_start("mft_tree")
        # merge/emit 阶段容器对象极多，阈值触发的 gen2 回收会带来秒级抖动
        gc_was_enabled = gc.isenabled()
        if gc_was_enabled:
            gc.disable()
        try:
            with SnapshotWriter(db_path, root=meta.root) as writer:
                async_w = _AsyncRowWriter(writer)
                try:

                    def on_batch(rows: list[EntryRow]) -> None:
                        if cancel and cancel():
                            raise MftTreeError("cancelled")
                        async_w.submit(rows)
                        # 用已提交行数跳动（写线程可能略滞后）
                        pg.emit(
                            async_w.written,
                            t(
                                f"MFT：构建并写入 {async_w.written}",
                                f"MFT: building & writing {async_w.written}",
                            ),
                        )

                    if _span_start:
                        _span_start("drain_rows")
                    try:
                        _rows, file_count, dir_count, total_size = (
                            build_entry_rows_from_compact(
                                table,
                                follow_reparse=follow_symlinks,
                                progress=None,
                                cancel=cancel,
                                on_batch=on_batch,
                                timer=timer,
                            )
                        )
                        del table
                        _ = _rows
                    finally:
                        if _span_end:
                            _span_end("drain_rows")
                        # 无论成功/取消都 join，避免写线程挂在队列上
                        if _span_start:
                            _span_start("mft_write_join")
                        try:
                            async_w.finish()
                        finally:
                            if _span_end:
                                _span_end("mft_write_join")

                    meta.file_count = file_count
                    meta.dir_count = dir_count
                    meta.total_size = total_size

                    if _span_start:
                        _span_start("finalize")
                    try:
                        pg.emit(
                            file_count,
                            t("MFT：建立索引", "MFT: building index"),
                            force=True,
                        )
                        writer.finalize(meta)
                    finally:
                        if _span_end:
                            _span_end("finalize")
                except Exception:
                    # drain finally 已 join；仅异常路径下线程仍存活时再收一次
                    try:
                        if async_w._thread.is_alive():
                            async_w.finish()
                    except Exception:  # noqa: BLE001
                        pass
                    raise
        except MftTreeError as exc:
            if str(exc) == "cancelled":
                raise ScanCancelled() from exc
            raise MftUnavailable(str(exc)) from exc
        except Exception as exc:
            # 写线程异常等
            if isinstance(exc, ScanCancelled):
                raise
            raise MftUnavailable(str(exc)) from exc
        finally:
            if gc_was_enabled:
                gc.enable()
            if _span_end:
                _span_end("mft_tree")

        pg.emit(
            file_count,
            t("MFT：完成", "MFT: done"),
            force=True,
        )

        if _set_meta:
            _set_meta(
                file_count=meta.file_count,
                dir_count=meta.dir_count,
                total_size=meta.total_size,
                skipped_count=0,
                workers=mft_workers,
                backend="mft",
            )
        if _mark:
            _mark("mft_done")
        return meta
    finally:
        if handle is not None:
            close_handle(handle)
