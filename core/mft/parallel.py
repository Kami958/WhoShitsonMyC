"""MFT 记录多进程解析。

纯 Python ``parse_record`` 受 GIL 限制，多线程几乎不加速；多进程才有效。
原始 $MFT 经 SharedMemory 共享，避免把数百 MB blob pickle 给每个 worker。
worker 返回紧凑二进制 meta + 文件名表，避免 pickle 海量 ``ParsedRecord``。

进程数**不**复用设置页「扫描线程数」（那是目录遍历语义，HDD 常为 1，
会废掉 MFT 纯 CPU 路径）。默认按核数 + 记录量推导；开发可用
``WSMC_MFT_WORKERS`` / ``WSMC_MFT_PROCS`` 强制覆盖。
"""

from __future__ import annotations

from multiprocessing import Pool, shared_memory

from .parse import ParsedRecord

# 下面这些名字的实现已拆到 pack / procs / worker / collect，
# 这里再导入一遍，旧的 core.mft.parallel.xxx 引用路径继续可用。
from .pack import (
    CompactMftTable,
    F_IN_USE,
    F_IS_DIR,
    F_NONE,
    F_REPARSE,
    META,
    META_SIZE,
    NameRow,
    _CHUNK_RECORDS,
    _F_IN_USE,
    _F_IS_DIR,
    _F_NONE,
    _F_REPARSE,
    _META,
    _META_SIZE,
    _chunk_ranges,
    _pack_chunk,
    _unpack_chunk,
)

from .procs import (
    _MAX_PROCS,
    _RECORDS_PER_PROC,
    _env_mft_procs,
    choose_mft_procs,
    default_mft_workers,
)

from .worker import (
    Cancel,
    Progress,
    _serial_to_compact,
    _warmup_pool,
    _worker_parse_range,
    begin_parse_pool,
    close_parse_pool,
    parse_records_serial,
)

from .collect import (
    StreamingCompactCollector,
    _ByteCoverage,
    collect_compact_from_shared_memory,
)


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
        return _unpack_chunk(0, table.meta, names_rel, table.link_targets)
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
