"""路径与卷：盘符是否机械盘、默认扫描线程数、路径比较、快照目录移动。"""

from __future__ import annotations

from collections.abc import Callable
import errno
import os
import stat as _stat
import uuid as _uuid

from .compress import drop_cache_for, is_snapshot_filename
from .snapshots_store import sanitize_folder_name


def _probe_reason(exc: OSError) -> str:
    """把目录探测失败的原因收成机器码，由界面层翻译。"""
    if isinstance(exc, FileExistsError):
        return "already_file"
    if isinstance(exc, NotADirectoryError):
        return "not_dir"
    if isinstance(exc, FileNotFoundError):
        return "missing_parent"
    if isinstance(exc, PermissionError):
        return "permission"
    if getattr(exc, "errno", None) == errno.ENOSPC:
        return "no_space"
    if getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM):
        return "permission"
    return "unknown"


def _write_probe_file(path: str) -> None:
    """在目录里写一个临时文件再删除；失败抛出 OSError。

    只看 ``makedirs`` 不足以判断当前账户能否写入：目录可能已存在但只读，
    也可能被 ACL 限制。临时文件名带随机后缀，避免与其他实例撞名。
    """
    probe = os.path.join(
        path, f".wsmc-write-test-{os.getpid()}-{_uuid.uuid4().hex}.tmp"
    )
    try:
        with open(probe, "wb") as fh:
            fh.write(b"wsmc")
    finally:
        try:
            os.remove(probe)
        except FileNotFoundError:
            pass


def probe_writable_dir(path: str) -> str:
    """验证目录当前账户可写，返回空串表示可用，否则返回失败原因。

    目录不存在时先创建；创建成功仍要写入临时文件并删除，删除失败同样
    视为不可写。
    """
    if not path:
        return "missing_parent"
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        return _probe_reason(exc)
    try:
        if not os.path.isdir(path):
            return "not_dir"
    except OSError:
        return "not_dir"
    try:
        _write_probe_file(path)
    except OSError as exc:
        return _probe_reason(exc)
    return ""


def migrate_data_root(
    src_dir: str,
    dst_dir: str,
    file_names: tuple[str, ...],
) -> dict:
    """把旧数据目录里的设置、登记、历史和快照移到新目录。

    目标已有同名文件时保留目标文件并计入跳过，源文件不覆盖目标。
    快照复用 :func:`migrate_snapshots`，失败的文件保留在源目录。
    """
    result: dict = {
        "moved": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
        "snapshots": {},
    }
    if not src_dir or not dst_dir or _same_dir(src_dir, dst_dir):
        return result
    reason = probe_writable_dir(dst_dir)
    if reason:
        result["failed"] += 1
        result["error"] = "destination_unwritable"
        result["reason"] = reason
        result["errors"].append(reason)
        return result

    for name in file_names:
        src = os.path.join(src_dir, name)
        try:
            src_stat = os.stat(src)
        except FileNotFoundError:
            continue
        except OSError as exc:
            # 源存在但当前账户读不到时必须算失败，不能像不存在一样跳过，
            # 否则界面会误报移动完成。
            result["failed"] += 1
            result["errors"].append(f"{name}: {type(exc).__name__}")
            continue
        if not _stat.S_ISREG(src_stat.st_mode):
            continue
        dst = os.path.join(dst_dir, name)
        if os.path.exists(dst):
            result["skipped"] += 1
            continue
        try:
            _move_file_verified(src, dst, int(src_stat.st_size))
            result["moved"] += 1
        except OSError as exc:
            result["failed"] += 1
            result["errors"].append(f"{name}: {type(exc).__name__}")

    snap_src = os.path.join(src_dir, "snapshots")
    snap_dst = os.path.join(dst_dir, "snapshots")
    try:
        snap_stat = os.stat(snap_src)
    except FileNotFoundError:
        snap_stat = None
    except OSError as exc:
        result["failed"] += 1
        result["errors"].append(f"snapshots: {type(exc).__name__}")
        snap_stat = None
    if snap_stat is not None and _stat.S_ISDIR(snap_stat.st_mode):
        result["snapshots"] = migrate_snapshots(snap_src, snap_dst)
        snap_result = result["snapshots"]
        result["moved"] += int(snap_result.get("moved") or 0)
        result["skipped"] += int(snap_result.get("skipped") or 0)
        result["failed"] += int(snap_result.get("failed") or 0)
        result["errors"].extend(list(snap_result.get("errors") or []))
    return result


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


def _file_size(path: str) -> int:
    """文件字节数；读不到返回 -1。"""
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def _copy_file_verified(src: str, dst: str, src_size: int) -> None:
    """复制到临时文件，校验大小后再替换目标。

    目标已有同名文件时也不会先把它破坏：临时文件校验通过后才替换。校验
    不一致时删除临时文件并抛出 OSError，调用方据此保留源文件。
    """
    import shutil

    tmp = f"{dst}.wsmc-{_uuid.uuid4().hex}.tmp"
    try:
        shutil.copy2(src, tmp)
        if _file_size(tmp) != src_size:
            raise OSError("size_mismatch")
        os.replace(tmp, dst)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _unique_conflict_destination(dst: str) -> str:
    """目标已被另一份内容占用时，找一个不冲突的名字，两份都保留。"""
    base, ext = os.path.splitext(dst)
    index = 2
    while index <= 999:
        candidate = f"{base} ({index}){ext}"
        try:
            os.stat(candidate)
        except FileNotFoundError:
            return candidate
        except OSError:
            # 读不到就当已占用，继续试下一个名字
            pass
        index += 1
    raise OSError("target_conflict_too_many")


def _move_file_verified(src: str, dst: str, src_size: int) -> None:
    """把一个文件搬到目标并校验目标大小。

    同卷优先直接改名，跨卷失败后退回复制；无论哪条路径，目标大小与源
    不一致都视为失败。源已不在时尽量把它移回去，移不回去则保留目标，
    避免两份数据都被删掉。
    """
    parent = os.path.dirname(dst)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    try:
        os.replace(src, dst)
        renamed = True
    except OSError:
        _copy_file_verified(src, dst, src_size)
        renamed = False

    if _file_size(dst) != src_size:
        if renamed and not os.path.exists(src):
            try:
                os.replace(dst, src)
            except OSError:
                pass
        elif os.path.exists(src):
            try:
                os.remove(dst)
            except OSError:
                pass
        raise OSError("size_mismatch")

    if not renamed and os.path.exists(src):
        os.remove(src)


def migrate_snapshots(
    src_dir: str,
    dst_dir: str,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """把 ``src_dir`` 下的快照文件迁到 ``dst_dir``。

    移动 ``.db`` / ``.dbz``，含快照根下一层归纳子目录中的文件
    （保留相对一层文件夹名）。搬文件前先验证目标目录当前账户可写；
    每个文件复制后校验目标大小与源一致。目标已有同名文件时比较大小：
    一致则跳过，不一致则重新复制并校验。失败不会删除源文件。

    返回 ``{moved, skipped, failed, errors, total}``；目标目录不可写时另带
    ``error='destination_unwritable'`` 与 ``reason``。

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
    try:
        src_stat = os.stat(src_dir)
    except FileNotFoundError:
        return result
    except OSError as exc:
        # 目录存在但当前账户读不到：必须上报，不能当成没有快照。
        result["failed"] += 1
        result["errors"].append(f"snapshots: {type(exc).__name__}")
        return result
    if not _stat.S_ISDIR(src_stat.st_mode):
        return result
    reason = probe_writable_dir(dst_dir)
    if reason:
        result["failed"] += 1
        result["error"] = "destination_unwritable"
        result["reason"] = reason
        result["errors"].append(reason)
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
            entry = os.stat(src)
        except FileNotFoundError:
            continue
        except OSError as exc:
            result["failed"] += 1
            result["errors"].append(f"{name}: {type(exc).__name__}")
            continue
        is_file = _stat.S_ISREG(entry.st_mode)
        is_dir = _stat.S_ISDIR(entry.st_mode) and not os.path.islink(src)
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
        except OSError as exc:
            result["failed"] += 1
            result["errors"].append(f"{folder}: {type(exc).__name__}")
            continue
        for child in children:
            if not is_snapshot_filename(child):
                continue
            child_src = os.path.join(src, child)
            try:
                child_stat = os.stat(child_src)
            except FileNotFoundError:
                continue
            except OSError as exc:
                result["failed"] += 1
                result["errors"].append(
                    f"{folder}/{child}: {type(exc).__name__}"
                )
                continue
            if not _stat.S_ISREG(child_stat.st_mode):
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

        def _report(current: str) -> None:
            if not progress:
                return
            progress(
                {
                    "done": i,
                    "total": total,
                    "name": name,
                    "status": current,
                    "moved": result["moved"],
                    "skipped": result["skipped"],
                    "failed": result["failed"],
                }
            )

        try:
            src_size = _file_size(src)
            if src_size < 0:
                raise OSError("source_missing")
            if os.path.exists(dst):
                if not os.path.isfile(dst):
                    raise OSError("target_is_not_file")
                same_size = _file_size(dst) == src_size
                if same_size:
                    result["skipped"] += 1
                    status = "skipped"
                    _report(status)
                    continue
                # 大小不一致说明目标里的同名文件是另一份内容：改名后并存，
                # 不用旧目录的文件覆盖它，两份快照都保留。
                new_dst = _unique_conflict_destination(dst)
                _move_file_verified(src, new_dst, src_size)
                result.setdefault("renamed", []).append(
                    f"{name}: {os.path.basename(new_dst)}"
                )
                dst = new_dst
            else:
                _move_file_verified(src, dst, src_size)

            if _file_size(dst) != src_size:
                raise OSError("size_mismatch")
            try:
                drop_cache_for(src)
            except Exception:  # noqa: BLE001
                pass
            result["moved"] += 1
            status = "moved"
            # 源在一层子目录且已空 → 删空夹。
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
        _report(status)
    return result
