"""子进程侧的工作单元与进程池：单段解析、串行解析、池的创建与关闭。"""

from __future__ import annotations

from collections.abc import Callable
from multiprocessing import Pool, shared_memory
from .pack import (
    CompactMftTable,
    _F_IN_USE,
    _F_IS_DIR,
    _F_NONE,
    _F_REPARSE,
    _META,
    _META_SIZE,
    _pack_chunk,
)
from .parse import ParsedRecord, parse_record
from .procs import _RECORDS_PER_PROC, choose_mft_procs


def _worker_parse_range(
    args: tuple[str, int, int, int, int, int],
) -> tuple[int, bytes, list[tuple[int, int, int, int, int, str, int]], list[tuple[int, str]]]:
    """子进程入口：从 SharedMemory 解析 [start, end) 条记录。

    必须是模块顶层函数，Windows spawn 才能 pickle。
    返回 ``(start, meta_bytes, names, link_targets)``。边解析边打包，避免整段 ParsedRecord 峰值。

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
    link_targets: list[tuple[int, str]] = []
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
                            out_buf, rel * _META_SIZE, _F_NONE, 0, 0, 0, 0, 0, 0
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
                            out_buf, rel * _META_SIZE, _F_NONE, 0, 0, 0, 0, 0, 0
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
                        rec.reparse_tag & 0xFFFFFFFF,
                    )
                    if rec.link_target:
                        link_targets.append((i, rec.link_target))
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
        return start, bytes(out_buf), names, link_targets
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
    meta, names, link_targets = _pack_chunk(parsed)
    # _pack_chunk 用相对下标 0..n-1，已是全局
    return CompactMftTable(
        n_records=len(parsed), meta=meta, names=names, link_targets=link_targets
    )


Progress = Callable[[int, int], None]  # (done, total)


Cancel = Callable[[], bool]
