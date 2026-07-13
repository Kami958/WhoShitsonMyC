"""打开卷、读 BPB、按 runlist 流式读取 $MFT 记录。"""

from __future__ import annotations

import ctypes
import os
import struct
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from ctypes import wintypes
from dataclasses import dataclass

# Win32
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
FILE_BEGIN = 0
# 可选：绕过系统缓存（需对齐）；默认不用，兼容性优先
FILE_FLAG_NO_BUFFERING = 0x20000000

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.ReadFile.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
kernel32.SetFilePointerEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int64,
    ctypes.POINTER(ctypes.c_int64),
    wintypes.DWORD,
]

# 大 run 切段并发读：NVMe 吃队列深度
_READ_SEGMENT = 16 * 1024 * 1024  # 16 MiB
_READ_THREADS = 4


class MftIoError(OSError):
    """卷打开/读取失败。"""


@dataclass(slots=True)
class BootInfo:
    bytes_per_sector: int
    sectors_per_cluster: int
    cluster_size: int
    mft_lcn: int
    record_size: int  # 通常 1024


def is_ntfs_volume(drive_root: str) -> bool:
    """``drive_root`` 形如 ``C:\\``。"""
    root = drive_root
    if not root.endswith("\\"):
        root = root + "\\"
    fs_name = ctypes.create_unicode_buffer(32)
    ok = kernel32.GetVolumeInformationW(
        root,
        None,
        0,
        None,
        None,
        None,
        fs_name,
        32,
    )
    return bool(ok) and fs_name.value.upper() == "NTFS"


def open_volume(drive_letter: str, *, no_buffering: bool = False) -> wintypes.HANDLE:
    """打开 ``\\\\.\\C:``。需要管理员权限才能读 MFT。"""
    path = f"\\\\.\\{drive_letter}:"
    flags = FILE_FLAG_NO_BUFFERING if no_buffering else 0
    handle = kernel32.CreateFileW(
        path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        flags,
        None,
    )
    if handle == INVALID_HANDLE_VALUE or handle is None:
        err = ctypes.get_last_error()
        raise MftIoError(f"CreateFileW({path}) failed, winerr={err}")
    return handle


def close_handle(handle: wintypes.HANDLE) -> None:
    if handle and handle != INVALID_HANDLE_VALUE:
        kernel32.CloseHandle(handle)


def _seek(handle: wintypes.HANDLE, offset: int) -> None:
    new_pos = ctypes.c_int64(0)
    ok = kernel32.SetFilePointerEx(
        handle, ctypes.c_int64(offset), ctypes.byref(new_pos), FILE_BEGIN
    )
    if not ok:
        raise MftIoError(f"SetFilePointerEx({offset}) failed")


def _read_exact_into(
    handle: wintypes.HANDLE,
    dest: bytearray | memoryview,
    dest_off: int,
    size: int,
) -> None:
    """把 ``size`` 字节读入 ``dest[dest_off:dest_off+size]``，零额外 ``bytes`` 拷贝。"""
    if size <= 0:
        return
    if dest_off < 0 or dest_off + size > len(dest):
        raise MftIoError("read destination out of range")
    # from_buffer 直接指向目标；ReadFile 落地即就位
    if isinstance(dest, memoryview):
        # memoryview 需可写 contiguous
        raw = dest[dest_off : dest_off + size]
        buf = (ctypes.c_char * size).from_buffer(raw)
    else:
        buf = (ctypes.c_char * size).from_buffer(dest, dest_off)
    done = 0
    while done < size:
        n = wintypes.DWORD(0)
        ok = kernel32.ReadFile(
            handle,
            ctypes.byref(buf, done),
            size - done,
            ctypes.byref(n),
            None,
        )
        if not ok or n.value == 0:
            err = ctypes.get_last_error()
            raise MftIoError(f"ReadFile failed at +{done}, winerr={err}")
        done += n.value


def _read_exact(handle: wintypes.HANDLE, size: int) -> bytes:
    buf = bytearray(size)
    _read_exact_into(handle, buf, 0, size)
    return bytes(buf)


def read_at(handle: wintypes.HANDLE, offset: int, size: int) -> bytes:
    _seek(handle, offset)
    return _read_exact(handle, size)


def read_at_into(
    handle: wintypes.HANDLE,
    offset: int,
    dest: bytearray | memoryview,
    dest_off: int,
    size: int,
) -> None:
    """读 ``offset`` 处 ``size`` 字节写入 ``dest[dest_off:]``。"""
    _seek(handle, offset)
    _read_exact_into(handle, dest, dest_off, size)


def parse_boot(boot: bytes) -> BootInfo:
    if len(boot) < 0x50:
        raise MftIoError("boot sector too short")
    # OEM ID "NTFS    " at 0x03
    oem = boot[3:11]
    if oem != b"NTFS    ":
        raise MftIoError(f"not NTFS boot (oem={oem!r})")
    bps = struct.unpack_from("<H", boot, 0x0B)[0]
    spc = boot[0x0D]
    if bps == 0 or spc == 0:
        raise MftIoError("invalid BPB")
    mft_lcn = struct.unpack_from("<q", boot, 0x30)[0]
    raw_cps = struct.unpack_from("<b", boot, 0x40)[0]
    if raw_cps < 0:
        record_size = 1 << (-raw_cps)
    else:
        record_size = raw_cps * spc * bps
    if record_size < 512 or record_size > 4096:
        # 常见 1024；放宽一点
        if record_size <= 0:
            raise MftIoError(f"bad record size raw={raw_cps}")
    return BootInfo(
        bytes_per_sector=bps,
        sectors_per_cluster=spc,
        cluster_size=bps * spc,
        mft_lcn=mft_lcn,
        record_size=record_size,
    )


def decode_runlist(data: bytes, cluster_size: int) -> list[tuple[int, int]]:
    """解码 non-resident runlist → ``[(lcn, byte_length), ...]``（稀疏 lcn=-1）。"""
    runs: list[tuple[int, int]] = []
    i = 0
    prev_lcn = 0
    n = len(data)
    while i < n:
        header = data[i]
        i += 1
        if header == 0:
            break
        len_size = header & 0x0F
        off_size = header >> 4
        if len_size == 0 or i + len_size + off_size > n:
            break
        run_len = int.from_bytes(data[i : i + len_size], "little")
        i += len_size
        if off_size == 0:
            # 稀疏
            runs.append((-1, run_len * cluster_size))
            continue
        off_bytes = data[i : i + off_size]
        i += off_size
        # 有符号 little-endian
        off = int.from_bytes(off_bytes, "little", signed=True)
        prev_lcn = prev_lcn + off
        runs.append((prev_lcn, run_len * cluster_size))
    return runs


def apply_usa(
    record: bytearray | memoryview,
    bytes_per_sector: int,
    *,
    start: int = 0,
    length: int | None = None,
) -> None:
    """应用 Update Sequence Array，修复各扇区末尾。

    ``start``/``length`` 可对大块缓冲中的单条记录就地修复；
    支持 ``bytearray`` 与可写 ``memoryview``（含 SharedMemory）。
    """
    n = len(record) if length is None else length
    if n < 8 or start < 0 or start + n > len(record):
        return
    usa_off = struct.unpack_from("<H", record, start + 0x04)[0]
    usa_count = struct.unpack_from("<H", record, start + 0x06)[0]
    if usa_off == 0 or usa_count < 2:
        return
    usa_end = usa_off + usa_count * 2
    if usa_end > n:
        return
    # usa[0] = USN；usa[1..] = 各扇区末尾原始字
    for s in range(usa_count - 1):
        sector_end = (s + 1) * bytes_per_sector
        if sector_end > n:
            break
        src = start + usa_off + 2 + s * 2
        dst = start + sector_end - 2
        record[dst : dst + 2] = record[src : src + 2]


def _find_nonresident_data_runs(record: bytes, cluster_size: int) -> list[tuple[int, int]] | None:
    """从 $MFT 基记录里取出未命名 $DATA 的 runlist。"""
    from .parse import iter_attributes  # 局部导入避免环

    for attr in iter_attributes(record):
        if attr.type_code != 0x80:  # DATA
            continue
        if attr.name:
            continue  # 只要默认数据流
        if attr.non_resident and attr.runlist is not None:
            return decode_runlist(attr.runlist, cluster_size)
    return None


def _plan_read_jobs(
    runs: list[tuple[int, int]],
    cluster_size: int,
    *,
    segment: int = _READ_SEGMENT,
) -> list[tuple[int, int, int]]:
    """展开为 ``(blob_off, disk_off, length)`` 任务列表（稀疏不进表）。"""
    jobs: list[tuple[int, int, int]] = []
    blob_off = 0
    for lcn, length in runs:
        if length <= 0:
            continue
        if lcn < 0:
            blob_off += length
            continue
        disk_base = lcn * cluster_size
        remaining = length
        local = 0
        while remaining > 0:
            chunk = min(segment, remaining)
            jobs.append((blob_off + local, disk_base + local, chunk))
            local += chunk
            remaining -= chunk
        blob_off += length
    return jobs


def _split_jobs_contiguous(
    jobs: list[tuple[int, int, int]], n_threads: int
) -> list[list[tuple[int, int, int]]]:
    """按 blob 偏移连续切段分给各线程（每线程一条顺序流，利于预取）。

    旧 round-robin 会让单线程在 0/64/128MiB 间跳，HDD/预取不友好。
    """
    if n_threads <= 1 or len(jobs) <= 1:
        return [jobs]
    n_threads = min(n_threads, len(jobs))
    # 按总字节均分，边界落在 job 边界上
    total = sum(ln for _bo, _do, ln in jobs)
    target = max(1, total // n_threads)
    batches: list[list[tuple[int, int, int]]] = []
    cur: list[tuple[int, int, int]] = []
    acc = 0
    for job in jobs:
        cur.append(job)
        acc += job[2]
        if len(batches) < n_threads - 1 and acc >= target:
            batches.append(cur)
            cur = []
            acc = 0
    if cur:
        batches.append(cur)
    return batches


def _read_jobs_concurrent(
    drive_letter: str,
    dest: bytearray | memoryview,
    jobs: list[tuple[int, int, int]],
    *,
    on_bytes: Callable[[int, int], None] | None = None,
    on_range: Callable[[int, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    total_est: int = 0,
    threads: int = _READ_THREADS,
) -> None:
    """多句柄并发把 jobs 读进 ``dest``（bytearray 或可写 memoryview）。

    ``on_range(blob_off, length)`` 在每段读完后回调（可读线程触发），
    供 pipeline 按就绪区段派发解析。
    """
    if not jobs:
        return
    n_threads = max(1, min(threads, len(jobs)))
    lock = threading.Lock()
    done_bytes = [0]
    err_box: list[BaseException] = []

    def worker(batch: list[tuple[int, int, int]]) -> None:
        if err_box:
            return
        h = None
        try:
            h = open_volume(drive_letter)
            for blob_off, disk_off, length in batch:
                if err_box:
                    return
                if cancel and cancel():
                    raise MftIoError("cancelled")
                read_at_into(h, disk_off, dest, blob_off, length)
                if on_range is not None:
                    on_range(blob_off, length)
                if on_bytes is not None:
                    with lock:
                        done_bytes[0] += length
                        on_bytes(done_bytes[0], total_est)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                if not err_box:
                    err_box.append(exc)
        finally:
            if h is not None:
                close_handle(h)

    batches = _split_jobs_contiguous(jobs, n_threads)

    if len(batches) == 1:
        worker(batches[0])
    else:
        with ThreadPoolExecutor(max_workers=len(batches)) as ex:
            futs = [ex.submit(worker, b) for b in batches if b]
            for f in as_completed(futs):
                f.result()  # 传播线程内未捕获异常
    if err_box:
        raise err_box[0]


def resolve_mft_layout(
    handle: wintypes.HANDLE, boot: BootInfo
) -> tuple[list[tuple[int, int]], int, int]:
    """读 $MFT 基记录 → ``(runs, usable_bytes, record_size)``。

    ``usable_bytes`` 已按整记录对齐，供 SharedMemory / bytearray 定长分配。
    """
    rec_size = boot.record_size
    mft_offset = boot.mft_lcn * boot.cluster_size
    first = bytearray(rec_size)
    read_at_into(handle, mft_offset, first, 0, rec_size)
    apply_usa(first, boot.bytes_per_sector)
    if first[0:4] != b"FILE":
        raise MftIoError("MFT record 0 magic is not FILE")
    runs = _find_nonresident_data_runs(bytes(first), boot.cluster_size)
    if not runs:
        raise MftIoError("$MFT DATA runlist not found")
    total_est = sum(length for _lcn, length in runs if length > 0)
    usable = (total_est // rec_size) * rec_size
    return runs, usable, rec_size


def fill_mft_buffer(
    handle: wintypes.HANDLE,
    boot: BootInfo,
    dest: bytearray | memoryview,
    *,
    runs: list[tuple[int, int]] | None = None,
    usable: int | None = None,
    on_bytes: Callable[[int, int], None] | None = None,
    on_range: Callable[[int, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    drive_letter: str | None = None,
    apply_usa_now: bool = True,
) -> int:
    """把 $MFT 填进已有缓冲 ``dest``（须 ≥ 表长），返回 ``record_size``。

    供 pipeline 直接写入 SharedMemory，避免「bytearray → shm」二次拷贝。
    ``apply_usa_now=False`` 时留给 worker 分片就地 USA（与解析重叠）。
    ``on_range(blob_off, length)``：每段物理读完成后回调，用于读∥解析流水。
    """
    rec_size = boot.record_size
    if runs is None or usable is None:
        runs, usable, rec_size = resolve_mft_layout(handle, boot)

    if usable <= 0:
        return rec_size
    if len(dest) < usable:
        raise MftIoError("MFT destination buffer too small")

    jobs = _plan_read_jobs(runs, boot.cluster_size)
    jobs = [
        (bo, do, min(ln, usable - bo))
        for bo, do, ln in jobs
        if bo < usable and min(ln, usable - bo) > 0
    ]

    # HDD：多句柄并发会磁头颠簸，回退单线程顺序读
    read_threads = _READ_THREADS
    if drive_letter:
        try:
            from ..store import is_rotational_drive

            if is_rotational_drive(drive_letter) is True:
                read_threads = 1
        except Exception:  # noqa: BLE001
            pass

    if drive_letter and len(jobs) > 1 and read_threads > 1:
        _read_jobs_concurrent(
            drive_letter,
            dest,
            jobs,
            on_bytes=on_bytes,
            on_range=on_range,
            cancel=cancel,
            total_est=usable,
            threads=read_threads,
        )
    else:
        total = 0
        for lcn, length in runs:
            if cancel and cancel():
                raise MftIoError("cancelled")
            if length <= 0:
                continue
            write_len = min(length, usable - total)
            if write_len <= 0:
                break
            if lcn >= 0:
                read_at_into(
                    handle,
                    lcn * boot.cluster_size,
                    dest,
                    total,
                    write_len,
                )
            # 稀疏 run 也推进 blob 偏移（零填充已由 shm 初值保证）
            if on_range is not None and write_len > 0:
                on_range(total, write_len)
            total += write_len
            if on_bytes is not None:
                on_bytes(total, usable)

    if apply_usa_now:
        bps = boot.bytes_per_sector
        n = usable // rec_size
        for i in range(n):
            start = i * rec_size
            if dest[start : start + 4] == b"FILE":
                apply_usa(dest, bps, start=start, length=rec_size)
    return rec_size


def load_mft_blob(
    handle: wintypes.HANDLE,
    boot: BootInfo,
    *,
    on_bytes: Callable[[int, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    drive_letter: str | None = None,
) -> tuple[bytearray, int]:
    """读取全部 $MFT 到一块 ``bytearray``（USA 已就地修复），返回 ``(blob, record_size)``。

    - 大 run 切 16MiB 段，多句柄并发读（``drive_letter`` 有值时）
    - ``ReadFile`` 直接写入 ``blob`` 偏移，避免 ``bytes`` 中转拷贝
    """
    runs, usable, rec_size = resolve_mft_layout(handle, boot)
    blob = bytearray(usable)
    fill_mft_buffer(
        handle,
        boot,
        blob,
        runs=runs,
        usable=usable,
        on_bytes=on_bytes,
        cancel=cancel,
        drive_letter=drive_letter,
        apply_usa_now=True,
    )
    return blob, rec_size


def load_mft_records(
    handle: wintypes.HANDLE,
    boot: BootInfo,
    *,
    on_bytes: Callable[[int, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    drive_letter: str | None = None,
) -> tuple[list[memoryview], int]:
    """读取全部 MFT 文件记录，返回 ``(records, record_size)``。"""
    blob, rec_size = load_mft_blob(
        handle,
        boot,
        on_bytes=on_bytes,
        cancel=cancel,
        drive_letter=drive_letter,
    )
    n = len(blob) // rec_size
    mv_all = memoryview(blob)
    records = [mv_all[i * rec_size : (i + 1) * rec_size] for i in range(n)]
    return records, rec_size
