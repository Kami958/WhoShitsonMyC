"""MFT 记录多进程解析。

纯 Python ``parse_record`` 受 GIL 限制，多线程几乎不加速；多进程才有效。
原始 $MFT 经 SharedMemory 共享，避免把数百 MB blob pickle 给每个 worker。
worker 返回紧凑二进制 meta + 文件名表，避免 pickle 海量 ``ParsedRecord``。

进程数**不**复用设置页「扫描线程数」（那是目录遍历语义，HDD 常为 1，
会废掉 MFT 纯 CPU 路径）。默认按核数 + 记录量推导；开发可用
``WSMC_MFT_WORKERS`` / ``WSMC_MFT_PROCS`` 强制覆盖。
"""

from __future__ import annotations

import os
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from multiprocessing import Pool, shared_memory

from .parse import FileNameAttr, ParsedRecord, parse_record

# flags byte
_F_IN_USE = 0x01
_F_IS_DIR = 0x02
_F_REPARSE = 0x04
_F_NONE = 0x80  # parse_record 返回 None

# flags, pad, nnames, base_record, mtime, data_size → 1+1+2+8+4+8 = 24
_META = struct.Struct("<BBHQIQ")
_META_SIZE = _META.size

# 全局导出供 tree 紧凑路径使用（避免魔法数分叉）
META = _META
META_SIZE = _META_SIZE
F_IN_USE = _F_IN_USE
F_IS_DIR = _F_IS_DIR
F_REPARSE = _F_REPARSE
F_NONE = _F_NONE

# 文件名行：record_index, parent_ref, alloc, real, flags, name, namespace
NameRow = tuple[int, int, int, int, int, str, int]


@dataclass(slots=True)
class CompactMftTable:
    """worker 紧凑产物：整表 meta + 全局文件名行（跳过 ParsedRecord 再膨胀）。"""

    n_records: int
    meta: bytes  # n_records * META_SIZE
    names: list[NameRow] = field(default_factory=list)

# 每进程至少约这么多条才值得开池（小卷单进程更快）
_RECORDS_PER_PROC = 50_000
# 任务切块：约为每进程活量的 1/3~1/4，imap_unordered 动态领活抹平 P/E 核尾部
_CHUNK_RECORDS = 35_000
# 进程数软上限：超线程收益薄，再多 spawn/IPC 易倒贴
_MAX_PROCS = 16

Progress = Callable[[int, int], None]  # (done, total)
Cancel = Callable[[], bool]


def _env_mft_procs() -> int | None:
    """开发覆盖：``WSMC_MFT_WORKERS`` 或 ``WSMC_MFT_PROCS``。"""
    for key in ("WSMC_MFT_WORKERS", "WSMC_MFT_PROCS"):
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        try:
            return max(1, min(_MAX_PROCS, int(raw)))
        except ValueError:
            continue
    return None


def choose_mft_procs(n_records: int) -> int:
    """按核数 + 记录量推导解析进程数。

    ::

        procs = min(
            max(1, cpu_count - 1),       # 留 1 逻辑核给 UI/主进程
            max(1, n_records // 50_000), # 每进程至少约 5 万条
        )

    环境变量强制覆盖时忽略活量公式（仍 cap 到 :data:`_MAX_PROCS`）。
    算出 ``1`` 时调用方应走串行，勿起 Pool。
    """
    forced = _env_mft_procs()
    if forced is not None:
        return forced
    n = max(0, int(n_records))
    if n <= 0:
        return 1
    cpu = max(1, os.cpu_count() or 1)
    by_cpu = max(1, cpu - 1)
    by_work = max(1, n // _RECORDS_PER_PROC)
    return max(1, min(_MAX_PROCS, by_cpu, by_work))


def default_mft_workers(preferred: int | None = None) -> int:
    """兼容旧名。

    ``preferred`` **已忽略**（不再继承扫描线程数）。无记录数时按核数估上限，
    真正按活量裁剪在 :func:`choose_mft_procs` / :func:`parse_records_parallel`。
    """
    _ = preferred
    forced = _env_mft_procs()
    if forced is not None:
        return forced
    cpu = max(1, os.cpu_count() or 1)
    return max(1, min(_MAX_PROCS, cpu - 1 if cpu > 1 else 1))


def _chunk_ranges(n: int, procs: int) -> list[tuple[int, int]]:
    """把 ``[0, n)`` 切成约 ``procs * 3~4`` 块（每块约 ``_CHUNK_RECORDS``）。"""
    if n <= 0:
        return []
    if procs <= 1:
        return [(0, n)]
    # 目标块数：进程数的 3~4 倍，同时单块不超过 _CHUNK_RECORDS
    target_chunks = max(procs * 3, (n + _CHUNK_RECORDS - 1) // _CHUNK_RECORDS)
    target_chunks = min(n, max(procs, target_chunks))
    chunk = max(1, (n + target_chunks - 1) // target_chunks)
    ranges: list[tuple[int, int]] = []
    s = 0
    while s < n:
        e = min(s + chunk, n)
        ranges.append((s, e))
        s = e
    return ranges


def _pack_chunk(
    records: list[ParsedRecord | None],
) -> tuple[bytes, list[tuple[int, int, int, int, int, str, int]]]:
    """把一段 ParsedRecord 压成 meta bytes + 文件名表。"""
    buf = bytearray(len(records) * _META_SIZE)
    names: list[tuple[int, int, int, int, int, str, int]] = []
    for i, rec in enumerate(records):
        if rec is None:
            _META.pack_into(buf, i * _META_SIZE, _F_NONE, 0, 0, 0, 0, 0)
            continue
        flags = 0
        if rec.in_use:
            flags |= _F_IN_USE
        if rec.is_directory:
            flags |= _F_IS_DIR
        if rec.has_reparse:
            flags |= _F_REPARSE
        fns = rec.file_names
        nnames = len(fns) if fns else 0
        if nnames > 0xFFFF:
            nnames = 0xFFFF
            fns = fns[:nnames]
        _META.pack_into(
            buf,
            i * _META_SIZE,
            flags,
            0,
            nnames,
            rec.base_record & 0xFFFFFFFFFFFFFFFF,
            rec.mtime & 0xFFFFFFFF,
            rec.data_size & 0xFFFFFFFFFFFFFFFF,
        )
        for fn in fns or ():
            names.append(
                (
                    i,
                    fn.parent_ref,
                    fn.allocated_size,
                    fn.real_size,
                    fn.flags,
                    fn.name,
                    fn.namespace,
                )
            )
    return bytes(buf), names


def _unpack_chunk(
    start: int,
    meta: bytes,
    names: list[tuple[int, int, int, int, int, str, int]],
) -> list[ParsedRecord | None]:
    n = len(meta) // _META_SIZE
    out: list[ParsedRecord | None] = [None] * n
    name_buckets: list[list[FileNameAttr]] = [[] for _ in range(n)]
    for rel, parent, alloc, real, flags, name, ns in names:
        if 0 <= rel < n:
            name_buckets[rel].append(
                FileNameAttr(
                    parent_ref=parent,
                    allocated_size=alloc,
                    real_size=real,
                    flags=flags,
                    name=name,
                    namespace=ns,
                )
            )
    for i in range(n):
        flags, _pad, nnames, base, mtime, data_size = _META.unpack_from(
            meta, i * _META_SIZE
        )
        if flags & _F_NONE:
            out[i] = None
            continue
        fns = name_buckets[i]
        _ = nnames
        out[i] = ParsedRecord(
            number=start + i,
            in_use=bool(flags & _F_IN_USE),
            is_directory=bool(flags & _F_IS_DIR),
            base_record=base,
            mtime=mtime,
            file_names=fns,
            data_size=data_size,
            has_reparse=bool(flags & _F_REPARSE),
        )
    return out


def _worker_parse_range(
    args: tuple[str, int, int, int, int, int],
) -> tuple[int, bytes, list[tuple[int, int, int, int, int, str, int]]]:
    """子进程入口：从 SharedMemory 解析 [start, end) 条记录。

    必须是模块顶层函数，Windows spawn 才能 pickle。
    返回 ``(start, meta_bytes, names)``。边解析边打包，避免整段 ParsedRecord 峰值。

    ``bytes_per_sector > 0`` 时先就地 USA（pipeline 读入时未修），再筛 free / 解析。

    注意：对 ``shm.buf`` 的 memoryview / 切片必须在 ``shm.close()`` 前全部
    ``release()``，否则 Python 3.10+ 会在 ``__del__`` 里抛
    ``BufferError: cannot close exported pointers exist``。
    """
    shm_name, shm_size, start, end, rec_size, bytes_per_sector = args
    shm = shared_memory.SharedMemory(name=shm_name)
    count = end - start
    out_buf = bytearray(count * _META_SIZE)
    names: list[tuple[int, int, int, int, int, str, int]] = []
    try:
        # 延迟导入：worker 进程避免无谓抬体积；USA 仅 pipeline 路径需要
        apply_usa = None
        if bytes_per_sector > 0:
            from .volume import apply_usa as _apply_usa

            apply_usa = _apply_usa

        with memoryview(shm.buf) as shm_mv:
            mv = shm_mv[:shm_size]
            try:
                for rel, i in enumerate(range(start, end)):
                    off = i * rec_size
                    # flags@0x16 不在扇区尾，USA 前即可筛 free / 非 FILE
                    if (
                        off + 0x18 > shm_size
                        or mv[off : off + 4] != b"FILE"
                        or (mv[off + 0x16] & 0x01) == 0
                    ):
                        _META.pack_into(
                            out_buf, rel * _META_SIZE, _F_NONE, 0, 0, 0, 0, 0
                        )
                        continue
                    # pipeline：读入后未 USA，parse 前就地修
                    if apply_usa is not None:
                        apply_usa(
                            mv, bytes_per_sector, start=off, length=rec_size
                        )
                    rec_mv = mv[off : off + rec_size]
                    try:
                        rec = parse_record(rec_mv, i)
                    finally:
                        rec_mv.release()
                    if rec is None:
                        _META.pack_into(
                            out_buf, rel * _META_SIZE, _F_NONE, 0, 0, 0, 0, 0
                        )
                        continue
                    flags = 0
                    if rec.in_use:
                        flags |= _F_IN_USE
                    if rec.is_directory:
                        flags |= _F_IS_DIR
                    if rec.has_reparse:
                        flags |= _F_REPARSE
                    fns = rec.file_names
                    nnames = len(fns) if fns else 0
                    if nnames > 0xFFFF:
                        nnames = 0xFFFF
                        fns = fns[:nnames]
                    _META.pack_into(
                        out_buf,
                        rel * _META_SIZE,
                        flags,
                        0,
                        nnames,
                        rec.base_record & 0xFFFFFFFFFFFFFFFF,
                        rec.mtime & 0xFFFFFFFF,
                        rec.data_size & 0xFFFFFFFFFFFFFFFF,
                    )
                    for fn in fns or ():
                        # 直接发全局下标，主进程 gather 可 list.extend
                        names.append(
                            (
                                i,
                                fn.parent_ref,
                                fn.allocated_size,
                                fn.real_size,
                                fn.flags,
                                fn.name,
                                fn.namespace,
                            )
                        )
            finally:
                mv.release()
        return start, bytes(out_buf), names
    finally:
        try:
            shm.close()
        except Exception:  # noqa: BLE001
            pass


def parse_records_serial(
    blob: bytes | bytearray | memoryview,
    rec_size: int,
    *,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    bytes_per_sector: int = 0,
) -> list[ParsedRecord | None]:
    """单进程顺序解析（小表 / 回退路径）。

    ``bytes_per_sector > 0`` 时先就地 USA（只读缓冲会先拷到 bytearray）。
    """
    if bytes_per_sector > 0:
        if isinstance(blob, bytes):
            blob = bytearray(blob)
        elif isinstance(blob, memoryview) and blob.readonly:
            blob = bytearray(blob)
    n = len(blob) // rec_size
    own_mv = not isinstance(blob, memoryview)
    mv = memoryview(blob) if own_mv else blob
    try:
        if bytes_per_sector > 0:
            from .volume import apply_usa

            for i in range(n):
                off = i * rec_size
                if off + 4 <= len(mv) and mv[off : off + 4] == b"FILE":
                    apply_usa(mv, bytes_per_sector, start=off, length=rec_size)

        out: list[ParsedRecord | None] = [None] * n
        step = max(1, n // 200) if n else 1
        for i in range(n):
            if i % step == 0:
                if cancel and cancel():
                    raise InterruptedError("cancelled")
                if progress is not None:
                    progress(i, n)
            off = i * rec_size
            if (
                off + 0x18 > len(mv)
                or mv[off : off + 4] != b"FILE"
                or (mv[off + 0x16] & 0x01) == 0
            ):
                continue
            out[i] = parse_record(mv[off : off + rec_size], i)
        if progress is not None:
            progress(n, n)
        return out
    finally:
        if own_mv:
            mv.release()


def _warmup_pool(pool: Pool) -> None:
    """丢一个空任务，逼 Windows spawn 把 worker 进程真正拉起来。"""
    try:
        pool.apply(int, (0,))
    except Exception:  # noqa: BLE001
        pass


def begin_parse_pool(
    n_records_hint: int = 0,
) -> tuple[Pool | None, int]:
    """在读盘阶段尽早创建进程池，与 I/O 重叠 spawn 成本。

    Returns:
        ``(pool_or_None, procs)``。``procs<=1`` 或创建失败时 pool 为 None。
    """
    procs = choose_mft_procs(max(n_records_hint, _RECORDS_PER_PROC * 2))
    if procs <= 1:
        return None, 1
    try:
        pool = Pool(processes=procs)
        _warmup_pool(pool)
        return pool, procs
    except Exception:  # noqa: BLE001
        return None, procs


def close_parse_pool(pool: Pool | None, *, terminate: bool = False) -> None:
    """关闭由 :func:`begin_parse_pool` 创建的池。"""
    if pool is None:
        return
    try:
        if terminate:
            pool.terminate()
        else:
            pool.close()
        pool.join()
    except Exception:  # noqa: BLE001
        try:
            pool.terminate()
            pool.join()
        except Exception:  # noqa: BLE001
            pass


def _serial_to_compact(
    blob: bytes | bytearray | memoryview,
    rec_size: int,
    *,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    bytes_per_sector: int = 0,
) -> CompactMftTable:
    """串行解析后压成 CompactMftTable（小表 / 回退）。"""
    parsed = parse_records_serial(
        blob,
        rec_size,
        progress=progress,
        cancel=cancel,
        bytes_per_sector=bytes_per_sector,
    )
    meta, names = _pack_chunk(parsed)
    # _pack_chunk 用相对下标 0..n-1，已是全局
    return CompactMftTable(n_records=len(parsed), meta=meta, names=names)


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
        done = 0
        gather_s = 0.0

        if _span_start:
            _span_start("mft_parse_wait")
        try:
            for ar in self._asyncs:
                if self._cancel and self._cancel():
                    raise InterruptedError("cancelled")
                try:
                    start, meta, names = ar.get()
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
            n_records=n, meta=bytes(full_meta), names=all_names
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
        done = 0
        gather_s = 0.0
        # wait：imap 墙钟（含 worker CPU + IPC + 主进程 gather）
        if _span_start:
            _span_start("mft_parse_wait")
        try:
            for start, meta, names in active.imap_unordered(
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
            n_records=n, meta=bytes(full_meta), names=all_names
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


def parse_from_shared_memory(
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
) -> list[ParsedRecord | None]:
    """从已有 SharedMemory 解析全部记录（兼容路径：内部走紧凑再 unpack）。

    热路径请用 :func:`collect_compact_from_shared_memory` + tree 直通。
    """
    table = collect_compact_from_shared_memory(
        shm,
        shm_size,
        rec_size,
        workers=workers,
        progress=progress,
        cancel=cancel,
        pool=pool,
        bytes_per_sector=bytes_per_sector,
        timer=timer,
    )
    if table.n_records <= 0:
        return []
    # 整表一次 unpack（测试 / 回退）
    _span_start = getattr(timer, "span_start", None)
    _span_end = getattr(timer, "span_end", None)
    if _span_start:
        _span_start("mft_parse_unpack")
    try:
        # names 已是全局 record index；_unpack_chunk 期望相对 chunk
        # 按单 chunk start=0 处理：把 names 的 index 当 rel
        names_rel = [
            (idx, p, a, r, f, n, ns)
            for idx, p, a, r, f, n, ns in table.names
        ]
        return _unpack_chunk(0, table.meta, names_rel)
    finally:
        if _span_end:
            _span_end("mft_parse_unpack")


def parse_records_parallel(
    blob: bytes | bytearray | memoryview,
    rec_size: int,
    *,
    workers: int | None = None,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    pool: Pool | None = None,
    bytes_per_sector: int = 0,
) -> list[ParsedRecord | None]:
    """解析全部 MFT 记录；记录多时走多进程 + SharedMemory。

    Args:
        blob: 整表 $MFT。默认视为 USA 已修复；若 ``bytes_per_sector > 0``
            则在 worker / 串行路径就地 USA。
        rec_size: 单条记录字节数（通常 1024）。
        workers: 强制进程数；``None`` 按 :func:`choose_mft_procs` 推导。
            （不再表示设置页扫描线程数。）
        progress: ``(done, total)`` 进度（按已完成记录数）。
        cancel: 返回 True 时中止。
        pool: 可选，读盘阶段预热的 :class:`Pool`；用完**不会**关闭，
            由调用方 :func:`close_parse_pool`。
        bytes_per_sector: >0 时解析前就地 USA。

    Raises:
        InterruptedError: 用户取消。
    """
    n = len(blob) // rec_size
    if n <= 0:
        return []

    if workers is not None:
        nw = max(1, min(_MAX_PROCS, int(workers)))
    else:
        nw = choose_mft_procs(n)

    if nw <= 1:
        return parse_records_serial(
            blob,
            rec_size,
            progress=progress,
            cancel=cancel,
            bytes_per_sector=bytes_per_sector,
        )

    ranges = _chunk_ranges(n, nw)
    if len(ranges) <= 1:
        return parse_records_serial(
            blob,
            rec_size,
            progress=progress,
            cancel=cancel,
            bytes_per_sector=bytes_per_sector,
        )

    own_pool = pool is None
    shm: shared_memory.SharedMemory | None = None
    active: Pool | None = pool
    try:
        raw = blob if isinstance(blob, (bytes, bytearray)) else bytes(blob)
        shm_size = len(raw)
        shm = shared_memory.SharedMemory(create=True, size=shm_size)
        shm.buf[:shm_size] = raw
        del raw

        return parse_from_shared_memory(
            shm,
            shm_size,
            rec_size,
            workers=nw,
            progress=progress,
            cancel=cancel,
            pool=active if not own_pool else None,
            bytes_per_sector=bytes_per_sector,
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
    except Exception:
        if active is not None and own_pool:
            try:
                active.terminate()
                active.join()
            except Exception:  # noqa: BLE001
                pass
            active = None
        return parse_records_serial(
            blob,
            rec_size,
            progress=progress,
            cancel=cancel,
            bytes_per_sector=bytes_per_sector,
        )
    finally:
        # parse_from_shared_memory 在 own_pool=False 时不关池；
        # 此处 blob 路径若自建池，由 parse_from_shared_memory(own_pool=True) 关。
        # 仅清理我们创建的 shm。
        if shm is not None:
            try:
                shm.close()
                shm.unlink()
            except Exception:  # noqa: BLE001
                pass
