"""路径与卷：盘符是否机械盘、默认扫描线程数、路径比较、快照目录迁移。"""

from __future__ import annotations

from collections.abc import Callable
import os

from .compress import drop_cache_for, is_snapshot_filename
from .snapshots_store import sanitize_folder_name


def is_rotational_drive(drive_letter: str | None = None) -> bool | None:
    """粗测盘符是否为机械盘（Windows SeekPenalty）。

    Args:
        drive_letter: ``'C'`` / ``'C:'`` / ``None``。``None`` 时查系统盘。

    Returns:
        True: 旋转介质（HDD）
        False: 非旋转（SSD/NVMe 等）
        None: 无法判断（非 Windows / 查询失败）
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        if drive_letter:
            letter = drive_letter.strip().rstrip(":\\/").upper()
            if len(letter) != 1 or not ("A" <= letter <= "Z"):
                return None
            root = letter + ":"
        else:
            sys_dir = ctypes.create_unicode_buffer(260)
            if not kernel32.GetSystemDirectoryW(sys_dir, 260):
                return None
            root = os.path.splitdrive(sys_dir.value)[0]
            if not root:
                return None
        volume_path = "\\\\.\\" + root.rstrip("\\/")

        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_EXISTING = 3
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        handle = kernel32.CreateFileW(
            volume_path,
            0,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle == INVALID_HANDLE_VALUE or handle is None:
            handle = kernel32.CreateFileW(
                volume_path,
                GENERIC_READ,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                0,
                None,
            )
        if handle == INVALID_HANDLE_VALUE or handle is None:
            return None

        try:
            # IOCTL_STORAGE_QUERY_PROPERTY + StorageDeviceSeekPenaltyProperty
            IOCTL = 0x002D1400

            class STORAGE_PROPERTY_QUERY(ctypes.Structure):
                _fields_ = [
                    ("PropertyId", wintypes.DWORD),
                    ("QueryType", wintypes.DWORD),
                    ("AdditionalParameters", ctypes.c_byte * 1),
                ]

            class DEVICE_SEEK_PENALTY_DESCRIPTOR(ctypes.Structure):
                _fields_ = [
                    ("Version", wintypes.DWORD),
                    ("Size", wintypes.DWORD),
                    ("IncursSeekPenalty", wintypes.BOOLEAN),
                ]

            query = STORAGE_PROPERTY_QUERY()
            query.PropertyId = 7  # StorageDeviceSeekPenaltyProperty
            query.QueryType = 0
            seek = DEVICE_SEEK_PENALTY_DESCRIPTOR()
            returned = wintypes.DWORD(0)
            ok = kernel32.DeviceIoControl(
                handle,
                IOCTL,
                ctypes.byref(query),
                ctypes.sizeof(query),
                ctypes.byref(seek),
                ctypes.sizeof(seek),
                ctypes.byref(returned),
                None,
            )
            if not ok:
                return None
            return bool(seek.IncursSeekPenalty)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - 启发式失败则退回默认
        return None


def _is_rotational_system_drive() -> bool | None:
    """系统盘是否机械盘（:func:`default_scan_workers` 用）。"""
    return is_rotational_drive(None)


def default_scan_workers() -> int:
    """扫描线程数默认值。

    - 探测到**机械盘**（寻道惩罚）→ ``1``（多线程易抖磁头）
    - SSD / 无法判断 → ``max(1, CPU 核数)``，不人为封顶
      （设置页仍可手动改；上限见 :func:`set_scan_workers`）
    """
    cpu = max(1, os.cpu_count() or 1)
    if _is_rotational_system_drive() is True:
        return 1
    return cpu


def _same_dir(a: str, b: str) -> bool:
    """两路径是否同一目录（规范化后比较）。"""
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(
            os.path.abspath(b)
        )
    except OSError:
        return False


def migrate_snapshots(
    src_dir: str,
    dst_dir: str,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """把 ``src_dir`` 下的快照文件迁到 ``dst_dir``。

    移动 ``.db`` / ``.dbz``，含快照根下一层归纳子目录中的文件
    （保留相对一层文件夹名）；目标已有同名文件则跳过（不覆盖）。
    返回 ``{moved, skipped, failed, errors, total}``。

    ``progress`` 可选，每处理一个文件回调一次，参数为::

        {done, total, name, status, moved, skipped, failed}
        status ∈ moved | skipped | failed
    """
    result: dict = {
        "moved": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
        "total": 0,
    }
    if not src_dir or not dst_dir or _same_dir(src_dir, dst_dir):
        return result
    if not os.path.isdir(src_dir):
        return result
    try:
        os.makedirs(dst_dir, exist_ok=True)
    except OSError as exc:
        result["failed"] += 1
        result["errors"].append(str(exc))
        return result

    try:
        names = os.listdir(src_dir)
    except OSError as exc:
        result["failed"] += 1
        result["errors"].append(str(exc))
        return result

    # (显示名, src, dst)；dst 含一层子目录时需先 makedirs
    jobs: list[tuple[str, str, str]] = []
    for name in names:
        src = os.path.join(src_dir, name)
        try:
            is_file = os.path.isfile(src)
            is_dir = os.path.isdir(src) and not os.path.islink(src)
        except OSError:
            continue
        if is_file and is_snapshot_filename(name):
            jobs.append((name, src, os.path.join(dst_dir, name)))
            continue
        if not is_dir:
            continue
        try:
            folder = sanitize_folder_name(name)
        except ValueError:
            continue
        if not folder or folder != name:
            continue
        try:
            children = os.listdir(src)
        except OSError:
            continue
        for child in children:
            if not is_snapshot_filename(child):
                continue
            child_src = os.path.join(src, child)
            if not os.path.isfile(child_src):
                continue
            jobs.append(
                (
                    f"{folder}/{child}",
                    child_src,
                    os.path.join(dst_dir, folder, child),
                )
            )
    total = len(jobs)
    result["total"] = total
    if progress and total:
        progress(
            {
                "done": 0,
                "total": total,
                "name": "",
                "status": "start",
                "moved": 0,
                "skipped": 0,
                "failed": 0,
            }
        )

    for i, (name, src, dst) in enumerate(jobs, start=1):
        status = "moved"
        if os.path.exists(dst):
            result["skipped"] += 1
            status = "skipped"
        else:
            try:
                parent = os.path.dirname(dst)
                if parent and not os.path.isdir(parent):
                    os.makedirs(parent, exist_ok=True)
                # 先尝试 rename（同卷快）；跨卷失败再 copy+remove
                try:
                    os.replace(src, dst)
                except OSError:
                    import shutil

                    shutil.copy2(src, dst)
                    try:
                        drop_cache_for(src)
                    except Exception:  # noqa: BLE001
                        pass
                    os.remove(src)
                else:
                    try:
                        drop_cache_for(src)
                    except Exception:  # noqa: BLE001
                        pass
                result["moved"] += 1
                status = "moved"
                # 源在一层子目录且已空 → 删空夹
                try:
                    src_parent = os.path.dirname(src)
                    if (
                        not _same_dir(src_parent, src_dir)
                        and _same_dir(os.path.dirname(src_parent), src_dir)
                        and os.path.isdir(src_parent)
                        and not os.listdir(src_parent)
                    ):
                        os.rmdir(src_parent)
                except OSError:
                    pass
            except OSError as exc:
                result["failed"] += 1
                result["errors"].append(f"{name}: {exc}")
                status = "failed"
        if progress:
            progress(
                {
                    "done": i,
                    "total": total,
                    "name": name,
                    "status": status,
                    "moved": result["moved"],
                    "skipped": result["skipped"],
                    "failed": result["failed"],
                }
            )
    return result
