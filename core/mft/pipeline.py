"""MFT 读盘 → SharedMemory → 多进程解析 流水线（仅 MFT 路径）。

与 :mod:`core.scanner` 解耦：本模块只负责「把 $MFT 变成紧凑表」。
建树 / 写库由 :mod:`core.mft.scan` 编排。

设计要点：

- 直接把卷数据读进 SharedMemory，避免 bytearray → shm 二次拷贝
- 读盘阶段预热进程池（Windows spawn 与 I/O 重叠）
- 多进程时：**段就绪即派解析**（读∥解析），USA 在 worker 内就地修
- 产出 :class:`CompactMftTable`，避免主进程再膨胀 ParsedRecord
"""

from __future__ import annotations

from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from multiprocessing import shared_memory

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
    on_bytes: ProgressBytes | None = None,
    on_parse: ProgressParse | None = None,
    cancel: Cancel | None = None,
    timer: object | None = None,
) -> MftParseResult:
    """打开后的卷：读 $MFT 进 SharedMemory 并多进程解析为紧凑表。

    多进程路径在读盘过程中按就绪字节区间派发 worker（读∥解析）。
    单进程或流式条件不足时回退为「读完再解析」。

    Raises:
        MftIoError: 读盘失败或取消。
        InterruptedError: 解析阶段用户取消。
        Exception: 其它解析失败（调用方映射为 MftUnavailable）。
    """
    _span_start = getattr(timer, "span_start", None)
    _span_end = getattr(timer, "span_end", None)

    parse_pool = None
    shm: shared_memory.SharedMemory | None = None
    mft_workers = 1
    usable = 0
    rec_size = boot.record_size
    nrec = 0
    streamer: StreamingCompactCollector | None = None

    if _span_start:
        # 读∥解析时墙钟同时覆盖 read+部分 parse；仍用 mft_read 标物理填充段
        _span_start("mft_read")
    try:
        runs, usable, rec_size = resolve_mft_layout(handle, boot)
        nrec = usable // rec_size if rec_size else 0

        if workers is not None:
            mft_workers = max(1, int(workers))
        else:
            mft_workers = choose_mft_procs(nrec)

        if mft_workers > 1:
            parse_pool, _ = begin_parse_pool(nrec)
            if parse_pool is None:
                mft_workers = 1
        else:
            parse_pool = None

        if cancel and cancel():
            raise MftIoError("cancelled")

        if usable <= 0:
            close_parse_pool(parse_pool)
            parse_pool = None
            return MftParseResult(
                table=CompactMftTable(n_records=0, meta=b"", names=[]),
                rec_size=rec_size,
                n_records=0,
                workers=1,
                shm_size=0,
            )

        shm = shared_memory.SharedMemory(create=True, size=usable)
        bps = boot.bytes_per_sector

        # 多进程：边读边派
        if parse_pool is not None and mft_workers > 1:
            streamer = StreamingCompactCollector(
                shm,
                usable,
                rec_size,
                workers=mft_workers,
                pool=parse_pool,
                bytes_per_sector=bps,
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
                    apply_usa_now=False,
                )
            finally:
                dest.release()
    except Exception:
        close_parse_pool(parse_pool, terminate=True)
        parse_pool = None
        streamer = None
        if shm is not None:
            try:
                shm.close()
                shm.unlink()
            except Exception:  # noqa: BLE001
                pass
            shm = None
        raise
    finally:
        if _span_end:
            _span_end("mft_read")

    if _span_start:
        _span_start("mft_parse")
    try:
        if cancel and cancel():
            raise InterruptedError("cancelled")

        assert shm is not None
        bps = boot.bytes_per_sector
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
                    )
                finally:
                    mv.release()
            mft_workers = 1

        return MftParseResult(
            table=table,
            rec_size=rec_size,
            n_records=nrec,
            workers=mft_workers,
            shm_size=usable,
        )
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
        if _span_end:
            _span_end("mft_parse")
