"""MFT 读盘 → 共享内存 → 多进程解析 流水线（仅 MFT 路径）。

与 :mod:`core.scanner` 解耦：本模块只负责「把 $MFT 变成紧凑表」。
建树 / 写库由 :mod:`core.mft.scan` 编排。

两条路径：

- 分块双缓冲：共享内存只放两块（各 256MB 量级），读下一块时解析上一块，
  百万条记录时不再需要整表 1.3GB 的常驻内存
- 整表：读完整份再解析，用于单进程、表太小无法分块或分块不可用的场合

两条路径都产出 :class:`CompactMftTable`，避免主进程再膨胀 ParsedRecord。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from multiprocessing import shared_memory

from .chunked import DEFAULT_SLOT_BYTES, ChunkedMftReader
from .pack import CompactNames
from .parallel import (
    CompactMftTable,
    StreamingCompactCollector,
    begin_parse_pool,
    choose_mft_procs,
    close_parse_pool,
    collect_compact_from_shared_memory,
)
from .parallel import _serial_to_compact as serial_to_compact
from .volume import (
    BootInfo,
    MftIoError,
    _plan_read_jobs,
    fill_mft_buffer,
    resolve_mft_layout,
)

ProgressBytes = Callable[[int, int], None]  # (done, total)
ProgressParse = Callable[[int, int], None]
Cancel = Callable[[], bool]


@dataclass(slots=True)
class MftParseResult:
    """读+解析产物（紧凑表）。"""

    table: CompactMftTable
    rec_size: int
    n_records: int
    workers: int
    shm_size: int


def read_and_parse_mft(
    handle: wintypes.HANDLE,
    boot: BootInfo,
    *,
    drive_letter: str | None = None,
    workers: int | None = None,
    read_threads: int | None = None,
    on_bytes: ProgressBytes | None = None,
    on_parse: ProgressParse | None = None,
    cancel: Cancel | None = None,
    timer: object | None = None,
) -> MftParseResult:
    """打开后的卷：读 $MFT 进共享内存并多进程解析为紧凑表。

    表大到能切成两块以上时走分块双缓冲，读下一块与解析上一块重叠；
    否则回退为整表「读完再解析」。

    Raises:
        MftIoError: 读盘失败或取消。
        InterruptedError: 解析阶段用户取消。
        Exception: 其它解析失败（调用方映射为 MftUnavailable）。
    """
    span_start = getattr(timer, "span_start", None)
    span_end = getattr(timer, "span_end", None)

    parse_pool = None
    shm: shared_memory.SharedMemory | None = None
    streamer: StreamingCompactCollector | None = None
    mft_workers = 1
    usable = 0
    rec_size = boot.record_size
    nrec = 0

    try:
        runs, usable, rec_size = resolve_mft_layout(handle, boot)
        nrec = usable // rec_size if rec_size else 0

        if workers is not None:
            mft_workers = max(1, int(workers))
        else:
            mft_workers = choose_mft_procs(nrec)

        if mft_workers > 1:
            parse_pool, _ = begin_parse_pool(nrec, procs=mft_workers)
            if parse_pool is None:
                mft_workers = 1
        else:
            parse_pool = None

        if cancel and cancel():
            raise MftIoError("cancelled")

        if usable <= 0:
            return MftParseResult(
                table=CompactMftTable(
                    n_records=0, meta=b"", names=CompactNames()
                ),
                rec_size=rec_size,
                n_records=0,
                workers=1,
                shm_size=0,
            )

        bps = boot.bytes_per_sector

        # 分块双缓冲：表大到能切两块以上才用
        slot_bytes = 0
        if parse_pool is not None and mft_workers > 1 and rec_size > 0:
            slot_bytes = max(rec_size, (DEFAULT_SLOT_BYTES // rec_size) * rec_size)
            n_blocks = (nrec * rec_size + slot_bytes - 1) // slot_bytes
        else:
            n_blocks = 1

        # 分块只驻留两块；至少三块才有净收益，两块的表直接用整表更简单
        if (
            n_blocks >= 3
            and drive_letter
            and not os.environ.get("WSMC_NO_CHUNK")
        ):
            shm = shared_memory.SharedMemory(create=True, size=slot_bytes * 2)
            jobs = _plan_read_jobs(runs, boot.cluster_size)
            jobs = [
                (bo, do, min(ln, usable - bo))
                for bo, do, ln in jobs
                if bo < usable and min(ln, usable - bo) > 0
            ]
            reader = ChunkedMftReader(
                shm=shm,
                slot_bytes=slot_bytes,
                n_records=nrec,
                rec_size=rec_size,
                drive_letter=drive_letter,
                pool=parse_pool,
                workers=mft_workers,
                read_threads=read_threads,
                bytes_per_sector=bps,
                cluster_size=boot.cluster_size,
                progress=on_parse,
                cancel=cancel,
                timer=timer,
            )
            table = reader.run(jobs)
            return MftParseResult(
                table=table,
                rec_size=rec_size,
                n_records=nrec,
                workers=mft_workers,
                shm_size=slot_bytes * 2,
            )

        # 整表路径
        shm = shared_memory.SharedMemory(create=True, size=usable)

        if span_start:
            span_start("mft_read")
        try:
            if parse_pool is not None and mft_workers > 1:
                streamer = StreamingCompactCollector(
                    shm,
                    usable,
                    rec_size,
                    workers=mft_workers,
                    pool=parse_pool,
                    bytes_per_sector=bps,
                    cluster_size=boot.cluster_size,
                    progress=on_parse,
                    cancel=cancel,
                    timer=timer,
                )
                if not streamer.can_stream:
                    streamer = None

            on_range = streamer.notify_range if streamer is not None else None

            with memoryview(shm.buf) as shm_mv:
                dest = shm_mv[:usable]
                try:
                    fill_mft_buffer(
                        handle,
                        boot,
                        dest,
                        runs=runs,
                        usable=usable,
                        on_bytes=on_bytes,
                        on_range=on_range,
                        cancel=cancel,
                        drive_letter=drive_letter,
                        threads=read_threads,
                        apply_usa_now=False,
                    )
                finally:
                    dest.release()
        finally:
            if span_end:
                span_end("mft_read")

        if span_start:
            span_start("mft_parse")
        if cancel and cancel():
            raise InterruptedError("cancelled")

        table: CompactMftTable
        try:
            if streamer is not None:
                # 外借 pool：finish 不关；本函数 finally 统一 close
                table = streamer.finish()
            else:
                table = collect_compact_from_shared_memory(
                    shm,
                    usable,
                    rec_size,
                    workers=mft_workers,
                    progress=on_parse,
                    cancel=cancel,
                    pool=parse_pool,
                    bytes_per_sector=bps,
                    cluster_size=boot.cluster_size,
                    timer=timer,
                )
        except InterruptedError:
            close_parse_pool(parse_pool, terminate=True)
            parse_pool = None
            raise
        except Exception:
            if mft_workers <= 1:
                raise
            close_parse_pool(parse_pool, terminate=True)
            parse_pool = None
            with memoryview(shm.buf) as shm_mv:
                mv = shm_mv[:usable]
                try:
                    table = serial_to_compact(
                        mv,
                        rec_size,
                        progress=on_parse,
                        cancel=cancel,
                        bytes_per_sector=bps,
                        cluster_size=boot.cluster_size,
                    )
                finally:
                    mv.release()
            mft_workers = 1
        finally:
            if span_end:
                span_end("mft_parse")

        return MftParseResult(
            table=table,
            rec_size=rec_size,
            n_records=nrec,
            workers=mft_workers,
            shm_size=usable,
        )
    except Exception:
        close_parse_pool(parse_pool, terminate=True)
        parse_pool = None
        streamer = None
        raise
    finally:
        close_parse_pool(parse_pool)
        parse_pool = None
        if shm is not None:
            try:
                shm.close()
                shm.unlink()
            except Exception:  # noqa: BLE001
                pass
            shm = None
