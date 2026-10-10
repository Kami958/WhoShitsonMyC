"""把各段结果收成一张紧凑表：流式收集器与共享内存收口。"""

from __future__ import annotations

import threading
import time
from multiprocessing import Pool, shared_memory
from .pack import CompactMftTable, NameRow, _META_SIZE, _chunk_ranges
from .procs import _MAX_PROCS, choose_mft_procs
from .worker import Cancel, Progress, _serial_to_compact, _worker_parse_range, close_parse_pool


class _ByteCoverage:
    """合并字节区间，判断某段是否已全部就绪（读∥解析用）。"""

    __slots__ = ("_iv", "_lock")

    def __init__(self) -> None:
        self._iv: list[list[int]] = []  # [lo, hi) 已合并
        self._lock = threading.Lock()

    def add(self, lo: int, hi: int) -> None:
        if hi <= lo:
            return
        with self._lock:
            iv = self._iv
            iv.append([lo, hi])
            iv.sort()
            merged: list[list[int]] = []
            for a, b in iv:
                if not merged or a > merged[-1][1]:
                    merged.append([a, b])
                else:
                    merged[-1][1] = max(merged[-1][1], b)
            self._iv = merged

    def covers(self, lo: int, hi: int) -> bool:
        if hi <= lo:
            return True
        with self._lock:
            for a, b in self._iv:
                if a <= lo and b >= hi:
                    return True
                if a > lo:
                    return False
            return False


class StreamingCompactCollector:
    """读盘过程中按就绪区段 ``apply_async`` 派解析，读完后 ``finish`` 收齐。

    多线程读可能非顺序填洞；某 parse 块 ``[start,end)`` 的字节全部就绪才派发。
    """

    __slots__ = (
        "n",
        "_rec_size",
        "_shm_name",
        "_shm_size",
        "_bps",
        "_pool",
        "_own_pool",
        "_ranges",
        "_dispatched",
        "_asyncs",
        "_coverage",
        "_lock",
        "_progress",
        "_cancel",
        "_timer",
        "_err",
        "_done_recs",
    )

    def __init__(
        self,
        shm: shared_memory.SharedMemory,
        shm_size: int,
        rec_size: int,
        *,
        workers: int,
        pool: Pool | None = None,
        bytes_per_sector: int = 0,
        progress: Progress | None = None,
        cancel: Cancel | None = None,
        timer: object | None = None,
    ) -> None:
        self.n = shm_size // rec_size
        self._rec_size = rec_size
        self._shm_name = shm.name
        self._shm_size = shm_size
        self._bps = max(0, int(bytes_per_sector))
        self._progress = progress
        self._cancel = cancel
        self._timer = timer
        self._err: BaseException | None = None
        self._done_recs = 0
        self._lock = threading.Lock()
        self._coverage = _ByteCoverage()
        self._asyncs: list = []
        nw = max(1, min(_MAX_PROCS, int(workers)))
        self._ranges = _chunk_ranges(self.n, nw) if self.n > 0 else []
        self._dispatched = [False] * len(self._ranges)
        if pool is not None:
            self._pool = pool
            self._own_pool = False
        elif nw > 1 and len(self._ranges) > 1:
            self._pool = Pool(processes=nw)
            self._own_pool = True
        else:
            self._pool = None
            self._own_pool = False

    @property
    def can_stream(self) -> bool:
        return self._pool is not None and len(self._ranges) > 1

    def notify_range(self, blob_off: int, length: int) -> None:
        """某段字节已写入 shm；尝试派发已完全就绪的 parse 块。"""
        if length <= 0 or self._pool is None:
            return
        if self._err is not None:
            return
        self._coverage.add(blob_off, blob_off + length)
        self._try_dispatch()

    def _try_dispatch(self) -> None:
        if self._pool is None:
            return
        rs = self._rec_size
        with self._lock:
            for i, (start, end) in enumerate(self._ranges):
                if self._dispatched[i]:
                    continue
                lo = start * rs
                hi = end * rs
                if not self._coverage.covers(lo, hi):
                    continue
                self._dispatched[i] = True
                args = (
                    self._shm_name,
                    self._shm_size,
                    start,
                    end,
                    rs,
                    self._bps,
                )
                try:
                    ar = self._pool.apply_async(_worker_parse_range, (args,))
                except Exception as exc:  # noqa: BLE001
                    self._err = exc
                    return
                self._asyncs.append(ar)

    def finish(self) -> CompactMftTable:
        """确保全部派发并收齐结果。串行回退时直接解析整表。"""
        n = self.n
        if n <= 0:
            return CompactMftTable(n_records=0, meta=b"", names=[])

        _span_start = getattr(self._timer, "span_start", None)
        _span_end = getattr(self._timer, "span_end", None)

        # 无池：调用方应在读完后走 serial；此处兜底
        if self._pool is None or len(self._ranges) <= 1:
            raise RuntimeError("StreamingCompactCollector requires multi-proc pool")

        # 读完后覆盖整表，派发剩余
        self._coverage.add(0, self._shm_size)
        self._try_dispatch()

        if self._err is not None:
            if self._own_pool:
                close_parse_pool(self._pool, terminate=True)
                self._pool = None
            raise self._err

        full_meta = bytearray(n * _META_SIZE)
        all_names: list[NameRow] = []
        all_link_targets: list[tuple[int, str]] = []
        done = 0
        gather_s = 0.0

        if _span_start:
            _span_start("mft_parse_wait")
        try:
            for ar in self._asyncs:
                if self._cancel and self._cancel():
                    raise InterruptedError("cancelled")
                try:
                    start, meta, names, link_targets = ar.get()
                except Exception:
                    if self._own_pool:
                        close_parse_pool(self._pool, terminate=True)
                        self._pool = None
                    raise
                t_g0 = time.perf_counter()
                count = len(meta) // _META_SIZE
                off = start * _META_SIZE
                full_meta[off : off + len(meta)] = meta
                if names:
                    all_names.extend(names)
                if link_targets:
                    all_link_targets.extend(link_targets)
                gather_s += time.perf_counter() - t_g0
                done += count
                if self._progress is not None:
                    self._progress(done, n)
        finally:
            if _span_end:
                _span_end("mft_parse_wait")
            if self._own_pool and self._pool is not None:
                close_parse_pool(self._pool)
                self._pool = None

        set_meta = getattr(self._timer, "set_meta", None)
        if set_meta is not None and gather_s > 0:
            set_meta(mft_parse_gather_s=round(gather_s, 4))
        if self._progress is not None:
            self._progress(n, n)
        return CompactMftTable(
            n_records=n,
            meta=bytes(full_meta),
            names=all_names,
            link_targets=all_link_targets,
        )


def collect_compact_from_shared_memory(
    shm: shared_memory.SharedMemory,
    shm_size: int,
    rec_size: int,
    *,
    workers: int | None = None,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    pool: Pool | None = None,
    bytes_per_sector: int = 0,
    timer: object | None = None,
) -> CompactMftTable:
    """从 SharedMemory 解析为紧凑表（**不**还原 ParsedRecord）。

    子计时（若 timer 支持 span_*）：
    - ``mft_parse_wait``：等 worker 回传（含 worker CPU，imap 墙钟）
    - ``mft_parse_gather``：主进程拼 meta（worker 已写全局 name 下标）
    """
    _span_start = getattr(timer, "span_start", None)
    _span_end = getattr(timer, "span_end", None)

    n = shm_size // rec_size
    if n <= 0:
        return CompactMftTable(n_records=0, meta=b"", names=[])

    if workers is not None:
        nw = max(1, min(_MAX_PROCS, int(workers)))
    else:
        nw = choose_mft_procs(n)

    if nw <= 1:
        with memoryview(shm.buf) as shm_mv:
            mv = shm_mv[:shm_size]
            try:
                return _serial_to_compact(
                    mv,
                    rec_size,
                    progress=progress,
                    cancel=cancel,
                    bytes_per_sector=bytes_per_sector,
                )
            finally:
                mv.release()

    ranges = _chunk_ranges(n, nw)
    if len(ranges) <= 1:
        with memoryview(shm.buf) as shm_mv:
            mv = shm_mv[:shm_size]
            try:
                return _serial_to_compact(
                    mv,
                    rec_size,
                    progress=progress,
                    cancel=cancel,
                    bytes_per_sector=bytes_per_sector,
                )
            finally:
                mv.release()

    own_pool = pool is None
    active: Pool | None = pool
    bps = max(0, int(bytes_per_sector))
    try:
        tasks = [
            (shm.name, shm_size, start, end, rec_size, bps)
            for start, end in ranges
        ]
        if active is None:
            active = Pool(processes=nw)

        full_meta = bytearray(n * _META_SIZE)
        all_names: list[NameRow] = []
        all_link_targets: list[tuple[int, str]] = []
        done = 0
        gather_s = 0.0
        # wait：imap 墙钟（含 worker CPU + IPC + 主进程 gather）
        if _span_start:
            _span_start("mft_parse_wait")
        try:
            for start, meta, names, link_targets in active.imap_unordered(
                _worker_parse_range, tasks, chunksize=1
            ):
                if cancel and cancel():
                    raise InterruptedError("cancelled")
                t_g0 = time.perf_counter()
                count = len(meta) // _META_SIZE
                off = start * _META_SIZE
                full_meta[off : off + len(meta)] = meta
                if names:
                    # worker 已写全局下标，直接 extend
                    all_names.extend(names)
                if link_targets:
                    all_link_targets.extend(link_targets)
                gather_s += time.perf_counter() - t_g0
                done += count
                if progress is not None:
                    progress(done, n)
        finally:
            if _span_end:
                _span_end("mft_parse_wait")
        # gather 是 wait 内主进程拼接耗时（子集，便于对照）
        set_meta = getattr(timer, "set_meta", None)
        if set_meta is not None and gather_s > 0:
            set_meta(mft_parse_gather_s=round(gather_s, 4))
        if progress is not None:
            progress(n, n)
        return CompactMftTable(
            n_records=n,
            meta=bytes(full_meta),
            names=all_names,
            link_targets=all_link_targets,
        )
    except InterruptedError:
        if active is not None and own_pool:
            try:
                active.terminate()
                active.join()
            except Exception:  # noqa: BLE001
                pass
            active = None
        raise
    finally:
        if own_pool and active is not None:
            try:
                active.close()
                active.join()
            except Exception:  # noqa: BLE001
                pass
