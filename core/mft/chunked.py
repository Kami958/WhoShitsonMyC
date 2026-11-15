"""分块双缓冲读取 $MFT：只驻留两块，读下一块时解析上一块。

整表方案要把整份元数据表放进共享内存，百万条记录时要 1.3GB 量级。
改为两块各 256MB 后，读第 k+1 块与解析第 k 块重叠，主进程常驻的只有
这两块内存，收上来的解析结果照常折进紧凑表。

约定：

- 块大小按记录大小对齐，一条记录不跨块，USA 修复仍由解析进程就地完成
- 读线程只在槽空闲时覆盖它，槽的占用计数由收集侧递减到零后放行
- 解析任务按记录区间切分，返回的紧凑结果按派发顺序收集，名字行保持
  记录号递增，建树侧的分组假设不变
"""

from __future__ import annotations

import queue
import threading
from multiprocessing import Pool, shared_memory
from multiprocessing import TimeoutError as MpTimeoutError

from .pack import CompactMftTable, CompactNames, FoldNames, _META_SIZE
from .volume import (
    _read_jobs_concurrent,
    close_handle,
    open_volume,
    prefer_overlapped_read,
    read_at_into,
    read_segments_overlapped,
)
from .worker import Cancel, Progress, _worker_parse_range

_SENTINEL = object()
# 每块缓冲大小。两块同时在手，所以实际占用是这个数的两倍。
# 从 128MB 降到 64MB 会多一倍派发次数，但实测 256MB 降到 128MB 没有变慢，
# 这里再减半，是否仍不变慢要以同段交替实测为准。
DEFAULT_SLOT_BYTES = 64 * 1024 * 1024
_CHUNK_RECORDS = 15000
_TASK_TIMEOUT_S = 120.0


class _ChunkSlot:
    """双缓冲的一格：空闲事件加未完成解析任务数。"""

    __slots__ = ("free", "pending")

    def __init__(self) -> None:
        self.free = threading.Event()
        self.free.set()
        self.pending = 0


def plan_chunk_jobs(
    jobs: list[tuple[int, int, int]],
    slot_bytes: int,
    total_bytes: int,
) -> list[list[tuple[int, int, int]]]:
    """把读取任务按块切开，返回每块的 ``(块内偏移, 磁盘偏移, 长度)``。

    一个读取任务可能跨块，跨界处拆成两段，保证每段落在一个块内。
    """
    if slot_bytes <= 0 or total_bytes <= 0:
        return []
    n_blocks = (total_bytes + slot_bytes - 1) // slot_bytes
    out: list[list[tuple[int, int, int]]] = [[] for _ in range(n_blocks)]
    for blob_off, disk_off, length in jobs:
        end = min(blob_off + length, total_bytes)
        if blob_off >= end:
            continue
        pos = blob_off
        while pos < end:
            k = pos // slot_bytes
            block_end = min(end, (k + 1) * slot_bytes)
            out[k].append(
                (pos - k * slot_bytes, disk_off + (pos - blob_off), block_end - pos)
            )
            pos = block_end
    return out


def chunk_record_ranges(
    first_rec: int, last_rec: int, chunk_records: int
) -> list[tuple[int, int]]:
    """把 ``[first_rec, last_rec)`` 切成不超过 ``chunk_records`` 的记录区间。"""
    if last_rec <= first_rec:
        return []
    step = max(1, int(chunk_records))
    out: list[tuple[int, int]] = []
    start = first_rec
    while start < last_rec:
        end = min(last_rec, start + step)
        out.append((start, end))
        start = end
    return out


class ChunkedMftReader:
    """读线程填槽、收集线程收结果的流水线。"""

    def __init__(
        self,
        *,
        shm: shared_memory.SharedMemory,
        slot_bytes: int,
        n_records: int,
        rec_size: int,
        drive_letter: str,
        pool: Pool,
        workers: int,
        read_threads: int | None = None,
        bytes_per_sector: int = 0,
        cluster_size: int = 4096,
        progress: Progress | None = None,
        cancel: Cancel | None = None,
        timer: object | None = None,
        chunk_records: int = _CHUNK_RECORDS,
    ) -> None:
        self._shm = shm
        self._slot_bytes = slot_bytes
        self._n = n_records
        self._rec = rec_size
        self._drive = drive_letter
        self._pool = pool
        self._workers = max(1, int(workers))
        # MFT 读盘线程数：实测固态上加线程反而更慢，默认 1
        self._read_threads = max(1, int(read_threads)) if read_threads else 1
        self._bps = max(0, int(bytes_per_sector))
        self._cluster = int(cluster_size)
        self._progress = progress
        self._cancel = cancel
        self._timer = timer
        self._chunk_records = max(1, int(chunk_records))
        self._slots = [_ChunkSlot(), _ChunkSlot()]
        self._lock = threading.Lock()
        self._q: queue.Queue = queue.Queue()
        self._err: BaseException | None = None
        self._stop = threading.Event()
        self._done_recs = 0
        # 默认逐段同步读；置 WSMC_OVERLAPPED=1 且在固态上才走异步提交
        self._overlapped = prefer_overlapped_read(drive_letter)

    # ---- 读线程 -------------------------------------------------

    def _block_ranges(self, k: int) -> list[tuple[int, int]]:
        first = (k * self._slot_bytes) // self._rec
        last = min(self._n, ((k + 1) * self._slot_bytes) // self._rec)
        return chunk_record_ranges(first, last, self._chunk_records)

    def _clear_gaps(
        self,
        base: int,
        segs: list[tuple[int, int, int]],
        block_bytes: int,
    ) -> None:
        """清零本块未被读取任务覆盖的区间。

        稀疏 run 的字节没有读取任务，槽复用时里面还是上一块的旧数据；
        不清零会让解析进程把上一块的记录当成本块的有效记录。
        """
        if block_bytes <= 0:
            return
        covered = sorted(
            (min(off, block_bytes), min(off + length, block_bytes))
            for off, _disk, length in segs
        )
        pos = 0
        for lo, hi in covered:
            if hi <= pos:
                continue
            if lo > pos:
                self._shm.buf[base + pos : base + lo] = bytes(lo - pos)
            pos = hi
        if pos < block_bytes:
            self._shm.buf[base + pos : base + block_bytes] = bytes(
                block_bytes - pos
            )

    def _read_loop(self, blocks: list[list[tuple[int, int, int]]]) -> None:
        span_start = getattr(self._timer, "span_start", None)
        span_end = getattr(self._timer, "span_end", None)
        handle = None
        try:
            handle = open_volume(self._drive, overlapped=self._overlapped)
            if span_start:
                span_start("mft_read")
            for k, segs in enumerate(blocks):
                if self._stop.is_set():
                    raise InterruptedError("cancelled")
                slot = self._slots[k % 2]
                while not slot.free.wait(0.2):
                    if self._stop.is_set() or (
                        self._cancel and self._cancel()
                    ):
                        raise InterruptedError("cancelled")
                if self._stop.is_set():
                    raise InterruptedError("cancelled")
                slot.free.clear()
                ranges = self._block_ranges(k)
                if not ranges:
                    slot.free.set()
                    continue
                base = (k % 2) * self._slot_bytes
                block_first = (k * self._slot_bytes) // self._rec
                block_bytes = min(
                    self._slot_bytes, self._n * self._rec - k * self._slot_bytes
                )
                self._clear_gaps(base, segs, block_bytes)
                with self._lock:
                    slot.pending = len(ranges)
                with memoryview(self._shm.buf) as shm_mv:
                    if self._overlapped:
                        read_segments_overlapped(
                            handle,
                            segs,
                            shm_mv,
                            base,
                            cancel=self._cancel,
                        )
                    elif self._read_threads > 1 and len(segs) > 1:
                        # 多线程读同一块内的各段。实测固态盘上这么做反而更慢，
                        # 默认值是 1；用户手动调大时才走这条分支。
                        _read_jobs_concurrent(
                            self._drive,
                            shm_mv,
                            [
                                (base + off, disk_off, length)
                                for off, disk_off, length in segs
                            ],
                            cancel=self._cancel,
                            total_est=block_bytes,
                            threads=self._read_threads,
                        )
                    else:
                        for off, disk_off, length in segs:
                            read_at_into(handle, disk_off, shm_mv, base + off, length)
                for first_rec, last_rec in ranges:
                    if self._cancel and self._cancel():
                        raise InterruptedError("cancelled")
                    # 解析进程按“段首记录号”折算偏移，因此这里要给段在槽内的
                    # 基址，而不是槽基址；否则除块首段外都会读到前面一段的数据
                    seg_base = base + (first_rec - block_first) * self._rec
                    args = (
                        self._shm.name,
                        self._shm.size,
                        seg_base,
                        first_rec,
                        last_rec,
                        self._rec,
                        self._bps,
                        self._cluster,
                    )
                    ar = self._pool.apply_async(_worker_parse_range, (args,))
                    self._q.put((slot, ar))
        except BaseException as exc:  # noqa: BLE001
            self._err = exc
        finally:
            if handle is not None:
                close_handle(handle)
            if span_end:
                span_end("mft_read")
            self._q.put(_SENTINEL)

    # ---- 收集 ---------------------------------------------------

    def run(self, jobs: list[tuple[int, int, int]]) -> CompactMftTable:
        """读满全部块并收齐解析结果，返回紧凑表。"""
        n = self._n
        if n <= 0:
            return CompactMftTable(n_records=0, meta=b"", names=CompactNames())

        blocks = plan_chunk_jobs(jobs, self._slot_bytes, n * self._rec)
        if not blocks:
            return CompactMftTable(n_records=0, meta=b"", names=CompactNames())

        span_start = getattr(self._timer, "span_start", None)
        span_end = getattr(self._timer, "span_end", None)

        full_meta = bytearray(n * _META_SIZE)
        # 名字行逐段折进定长数组，段对象用完即丢，不再拼成整表名字表
        all_names = FoldNames()
        all_links: list[tuple[int, str]] = []
        all_tags: list[tuple[int, int]] = []
        done = 0
        if span_start:
            span_start("mft_parse")

        reader = threading.Thread(
            target=self._read_loop,
            args=(blocks,),
            name="mft-chunk-reader",
            daemon=True,
        )
        reader.start()
        try:
            while True:
                if self._cancel and self._cancel():
                    raise InterruptedError("cancelled")
                item = self._q.get()
                if item is _SENTINEL:
                    break
                slot, ar = item
                waited = 0.0
                while True:
                    try:
                        start, meta, names, link_targets, tags = ar.get(timeout=1.0)
                        break
                    except MpTimeoutError:
                        waited += 1.0
                        if self._cancel and self._cancel():
                            raise InterruptedError("cancelled")
                        if waited >= _TASK_TIMEOUT_S:
                            raise TimeoutError("解析任务迟迟没有返回")
                off = start * _META_SIZE
                full_meta[off : off + len(meta)] = meta
                if len(names):
                    all_names.add(names)
                if link_targets:
                    all_links.extend(link_targets)
                if tags:
                    all_tags.extend(tags)
                done += len(meta) // _META_SIZE
                if self._progress is not None:
                    self._progress(done, n)
                with self._lock:
                    slot.pending -= 1
                    if slot.pending <= 0:
                        slot.free.set()
            if self._err is not None:
                raise self._err
        finally:
            if span_end:
                span_end("mft_parse")
            self._stop.set()
            for slot in self._slots:
                slot.free.set()
            reader.join(timeout=30.0)

        if self._progress is not None:
            self._progress(n, n)
        return CompactMftTable(
            n_records=n,
            meta=bytes(full_meta),
            fold=all_names,
            link_targets=all_links,
            reparse_tags=all_tags,
        )
