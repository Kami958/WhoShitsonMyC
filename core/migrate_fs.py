"""迁移用到的底层文件操作：长路径前缀、联接判断、复制、校验、卷文件系统查询。"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable, Iterator


class MigrateError(Exception):
    """可预期的迁移失败（message 为机器键，调用方负责 i18n）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _is_reparse(path: str) -> bool:
    """路径是否为重解析点（junction / 符号链接 / 其它）。"""
    try:
        st = os.lstat(_fs_path(path))
        return int(getattr(st, "st_reparse_tag", 0) or 0) != 0
    except OSError:
        return False


def _fs_path(path: str) -> str:
    """超长绝对路径转扩展长度形式（``\\\\?\\``），其余原样返回。

    Windows 没开 ``LongPathsEnabled`` 时文件 API 的路径上限是 260 个字符，
    ``\\\\?\\`` 前缀跳过这项检查。``os.walk`` / ``os.scandir`` 走目录枚举，
    不受上限影响，只有真正打开文件的调用需要转换。传入的可能是相对路径，
    所以先取绝对路径。末段（文件名）按原名保留：``normpath`` 会吃掉结尾的
    空格和点，那种名字只能靠 ``\\\\?\\`` 访问，还原后才能指向正确的文件。
    """
    if os.name != "nt" or not path or path.startswith(_LONG_PREFIX):
        return path
    try:
        p = os.path.normpath(os.path.abspath(path)).replace("/", "\\")
        tail = os.path.basename(path)
        if tail and tail not in (".", ".."):
            p = os.path.join(os.path.dirname(p), tail)
    except (OSError, ValueError):
        return path
    if len(p) < _LONG_AT:
        return path
    if p.startswith("\\\\"):
        return _LONG_PREFIX + "UNC" + p[1:]
    return _LONG_PREFIX + p


def _rmtree(path: str) -> None:
    """删整棵目录树，失败不抛。

    清理复制产物用：超长路径要带扩展前缀，只读项要先清属性（Windows 的
    删除 API 对只读项一律拒绝），清不掉的就留着，不阻断调用方。
    """
    if not path or not os.path.lexists(_fs_path(path)):
        return
    try:
        shutil.rmtree(_fs_path(path), onexc=_rmtree_retry)
    except OSError:
        pass


def _rmtree_retry(func, path: str, exc: BaseException) -> None:
    """被拒的项清掉只读属性重试一次；仍失败就放弃（清理是尽力而为）。"""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _drop_path(path: str) -> None:
    """删掉一个条目：链接只删链接本身，目录删整棵树，文件直接删。

    补齐时按变化清单删目标侧多出来的东西用。删不掉不抛：重试与只读属性
    的处理都在 ``_rmtree`` 里，这里只保证「链接不被当成目录树删掉」。
    """
    fs = _fs_path(path)
    if not os.path.lexists(fs):
        return
    if _is_reparse(path):
        try:
            os.unlink(fs)
        except OSError:
            try:
                os.rmdir(fs)
            except OSError:
                pass
        return
    if os.path.isdir(fs):
        _rmtree(path)
        return
    try:
        os.chmod(fs, stat.S_IWRITE)
    except OSError:
        pass
    try:
        os.unlink(fs)
    except OSError:
        pass


def _link_target(path: str) -> str:
    """重解析点的链接目标；读不出目标的（非链接类重解析点）返回空串。"""
    try:
        return os.readlink(_fs_path(path))
    except OSError:
        return ""


def _is_link(path: str) -> bool:
    """路径是否为读得出目标的链接（符号链接或目录联接）。"""
    return _is_reparse(path) and bool(_link_target(path))


def _rebuild_link(src: str, dst: str, target: str, *, is_dir: bool) -> bool:
    """在 ``dst`` 处按 ``src`` 的链接重建，成功返回真。

    目标不存在（悬空链接）也能建出链接。先试符号链接；目录链接在没权限
    （非管理员且没开开发者模式）时退回目录联接，建 junction 不需要提权。
    """
    try:
        os.symlink(target, _fs_path(dst), target_is_directory=is_dir)
    except OSError:
        pass
    else:
        try:
            shutil.copystat(_fs_path(src), _fs_path(dst), follow_symlinks=False)
        except (OSError, NotImplementedError):
            pass
        return True
    if not is_dir or os.name != "nt":
        return False
    abs_target = target
    if not os.path.isabs(abs_target):
        abs_target = os.path.join(os.path.dirname(src), abs_target)
    try:
        import _winapi

        _winapi.CreateJunction(os.path.abspath(abs_target), _fs_path(dst))
    except (OSError, ImportError):
        return False
    return True


def _same_path(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(
            os.path.normpath(b)
        )
    except OSError:
        return False


def _volume_fs(path: str) -> str:
    """路径所在卷的文件系统名（NTFS、FAT32、exFAT 等），取不到返回空串。

    迁移只允许同种文件系统之间进行。FAT32 与 exFAT 的修改时间精度（2 秒、
    10 毫秒）比 NTFS 粗，复制回去的时间会被取整，校验必然不一致；这两种
    文件系统也存不下目录联接与符号链接。
    """
    if os.name != "nt":
        return ""
    try:
        import ctypes

        drive = os.path.splitdrive(os.path.abspath(path))[0]
        if not drive:
            return ""
        root = drive if drive.endswith("\\") else drive + "\\"
        buf = ctypes.create_unicode_buffer(64)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), None, 0, None, None, None, buf, len(buf)
        )
    except (OSError, AttributeError, ValueError):
        return ""
    return buf.value.strip().upper() if ok else ""


def _unique_path(base: str) -> str:
    """生成不冲突的路径（``base`` 已存在则加序号）。"""
    candidate = base
    i = 1
    while os.path.exists(candidate):
        candidate = f"{base} ({i})"
        i += 1
    return candidate


def _walk_error(exc: OSError) -> None:
    """目录遍历被拒（无权限等）时中止，不让读不到的目录被当成空目录。"""
    raise MigrateError(f"unreadable:{exc.filename or ''}")


def _size_of(path: str) -> int:
    """单个文件的字节数，取不到算 0（进度用，不该为此中断迁移）。"""
    try:
        return int(os.path.getsize(_fs_path(path)))
    except OSError:
        return 0


def _walk_total(root: str, *, cancel: Callable[[], bool]) -> tuple[int, int]:
    """扫一遍源目录，统计文件数与总字节（用于进度百分比）。

    递归 ``os.scandir``，文件大小直接用目录项自带的（NTFS 上目录枚举就
    带回来了，不必为每个文件再查一次盘）。不跟随符号链接/重解析点子目录，
    避免死循环与重复计算；大小读不到的条目算进文件数但不计字节。
    """
    files = 0
    total = 0
    stack = [root]
    while stack:
        if cancel and cancel():
            raise MigrateError("cancelled")
        cur = stack.pop()
        try:
            with os.scandir(_fs_path(cur)) as it:
                entries = list(it)
        except OSError as exc:
            _walk_error(exc)
            continue
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                if not _is_reparse(entry.path):
                    stack.append(entry.path)
                continue
            try:
                total += entry.stat().st_size
            except OSError:
                pass
            files += 1
    return files, total


def _walk_stats(
    root: str, *, cancel: Callable[[], bool], skip: set[str] | None = None
) -> tuple[int, int, int]:
    """扫一遍目录树，返回 ``(目录数, 文件数, 总字节数)``。

    口径与 ``_walk_files`` 一致：重解析点目录不递归也不计入，``skip`` 里的
    条目不计入，大小读不到的算进文件数但不计字节。用目录项自带的大小，
    不为每个文件再查一次盘。10 万个文件两边各扫一遍约 0.5 秒。
    """
    skip = skip or set()
    dirs = 0
    files = 0
    total = 0
    stack: list[tuple[str, str]] = [("", root)]
    while stack:
        if cancel and cancel():
            raise MigrateError("cancelled")
        prefix, cur = stack.pop()
        try:
            with os.scandir(_fs_path(cur)) as it:
                entries = list(it)
        except OSError as exc:
            _walk_error(exc)
            continue
        for entry in entries:
            rel = prefix + entry.name
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                if _is_reparse(entry.path):
                    continue
                dirs += 1
                stack.append((rel + os.sep, entry.path))
                continue
            if rel in skip:
                continue
            try:
                total += entry.stat().st_size
            except OSError:
                pass
            files += 1
    return dirs, files, total


def _path_key(rel: str) -> str:
    """两侧比对用的排序键：分隔符换成 ``\0``，与 ``_walk_files`` 的顺序一致。"""
    return rel.replace(os.sep, "\0")


def _scandir_sorted(root: str, *, cancel: Callable[[], bool]) -> list[os.DirEntry]:
    """读一层目录，按名字排序返回目录项；链接目录（重解析点）不列进来。"""
    out: list[os.DirEntry] = []
    try:
        with os.scandir(_fs_path(root)) as it:
            for entry in it:
                if cancel and cancel():
                    raise MigrateError("cancelled")
                try:
                    if entry.is_dir() and _is_reparse(entry.path):
                        continue
                except OSError:
                    pass
                out.append(entry)
    except OSError as exc:
        _walk_error(exc)
    out.sort(key=lambda e: e.name)
    return out


def _walk_files(
    root: str, *, cancel: Callable[[], bool], skip: set[str] | None = None
) -> Iterator[tuple[str, int, float]]:
    """按固定顺序逐个产出 ``(相对路径, 字节数, 修改时间)``，用于两边比对。

    每层按名字排序，子目录的条目紧跟在它自己的位置，于是同一棵树的输出
    顺序就是按 ``_path_key`` 排的（分隔符当作 ``\0``，比文件名里任何合法
    字符都小）。两侧同时推进就是同一份顺序，可以边比边丢，不用把两棵树
    的文件清单都留在内存里。不跟随重解析点子目录，和复制阶段保持一致。

    ``skip`` 里的相对路径直接跳过，两侧传同一份，行序不会错位。
    """
    skip = skip or set()
    stack: list[tuple[str, list[os.DirEntry], int]] = [
        ("", _scandir_sorted(root, cancel=cancel), 0)
    ]
    while stack:
        prefix, entries, idx = stack.pop()
        if idx >= len(entries):
            continue
        entry = entries[idx]
        stack.append((prefix, entries, idx + 1))
        rel = prefix + entry.name
        try:
            is_dir = entry.is_dir()
        except OSError:
            is_dir = False
        if is_dir:
            stack.append((rel + os.sep, _scandir_sorted(entry.path, cancel=cancel), 0))
            continue
        if rel in skip:
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        yield (rel, st.st_size, st.st_mtime)


def _copy_file(
    src: str,
    dst: str,
    *,
    cancel: Callable[[], bool],
    buf_size: int = 1024 * 1024,
    on_chunk: Callable[[int], None] | None = None,
) -> None:
    """逐 chunk 复制单个文件，每 chunk 检查取消；末尾用 ``copystat`` 保留元数据。

    代替 ``shutil.copy2``：``copy2`` 一次系统调用把整个文件拷完，没法在
    大文件中途打断。分块读写后取消（含空目录、大文件）都能真正中断。

    ``on_chunk`` 每写完一块就拿到这一块的字节数，交给调用方累计；单个
    大文件要写几十秒，靠它把进度在途中报出去。
    """
    try:
        with open(_fs_path(src), "rb") as fin, open(_fs_path(dst), "wb") as fout:
            while True:
                if cancel and cancel():
                    raise MigrateError("cancelled")
                chunk = fin.read(buf_size)
                if not chunk:
                    break
                fout.write(chunk)
                if on_chunk:
                    on_chunk(len(chunk))
        shutil.copystat(_fs_path(src), _fs_path(dst))
    except OSError as exc:
        raise MigrateError(f"copy:{os.path.basename(src)}:{exc}") from exc


# 只有 Windows 有 CopyFileW。
_SYSCALL_COPY = os.name == "nt"

# 走系统调用整份复制的上限：再大就分块复制，保住中途取消。
_SYSCALL_COPY_MAX = 128 * 1024 * 1024


def _copy_file_win(src: str, dst: str) -> None:
    """``CopyFileW`` 整份复制（内核态循环，比 Python 分块快得多）。

    只给小于 ``_SYSCALL_COPY_MAX`` 的文件用：一次调用拷完，中途没法取消。
    第三个参数 False 表示目标已存在就覆盖，与 ``open(dst, "wb")`` 的行为
    一致（传 True 会在目标存在时直接失败）。修改时间与只读属性由系统调用
    一并带过去，不用再调 ``copystat``（每文件要多花 0.31 毫秒）。
    """
    detail = ""
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        k32.CopyFileW.restype = ctypes.c_int
        k32.CopyFileW.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int)
        if not k32.CopyFileW(_fs_path(src), _fs_path(dst), False):
            code = int(k32.GetLastError())
            detail = ctypes.FormatError(code).strip() or f"error {code}"
    except (OSError, AttributeError, ValueError) as exc:
        detail = str(exc)
    if detail:
        raise MigrateError(f"copy:{os.path.basename(src)}:{detail}")


def _copy_one(
    src: str,
    dst: str,
    size: int,
    *,
    cancel: Callable[[], bool],
    on_chunk: Callable[[int], None] | None = None,
) -> None:
    """复制单个文件：小文件走系统调用，失败或大小不明就回退分块复制。

    ``size`` 是复制前统计到的字节数；统计失败得 0，也按分块处理，宁可慢
    一点也要保住中途取消。走系统调用时没有块级回报，这类文件本来就小，
    完成时一次计入总量即可。
    """
    if _SYSCALL_COPY and 0 < size < _SYSCALL_COPY_MAX:
        try:
            _copy_file_win(src, dst)
            return
        except MigrateError:
            pass
    _copy_file(src, dst, cancel=cancel, on_chunk=on_chunk)


# 目标卷是 SSD 时的并行复制线数。机械盘上并发写会让磁头来回寻道，反而更慢，
# 探测不出卷类型时也按机械盘处理。
_COPY_THREADS_SSD = 4
_COPY_THREADS_HDD = 1

# 每个卷适合几条线，一次迁移只探测一次。
_COPY_WORKERS_CACHE: dict[str, int] = {}

_IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
_STORAGE_DEVICE_SEEK_PENALTY_PROPERTY = 7
_OPEN_EXISTING = 3
_FILE_SHARE_ALL = 7


def _device_path(path: str) -> str:
    r"""卷设备路径（``\\.\C:``）；不是盘符挂载的（UNC 等）返回空串。"""
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if len(drive) != 2 or drive[1] != ":":
        return ""
    return "\\\\.\\" + drive


def _incurs_seek_penalty(device: str) -> bool:
    """设备写数据是否有寻道代价：机械盘为真，SSD 为假，探测不出也按真。

    问 ``IOCTL_STORAGE_QUERY_PROPERTY`` 的 ``StorageDeviceSeekPenaltyProperty``。
    按卷打开并把 ``dwDesiredAccess`` 传 0，这样不需要管理员权限；问不到
    答案的一律按机械盘处理，宁可只用一条线，也不在机械盘上并发写。
    """
    try:
        import ctypes

        class _Query(ctypes.Structure):
            """``STORAGE_PROPERTY_QUERY``：问哪个属性。"""

            _fields_ = (
                ("PropertyId", ctypes.c_uint32),
                ("QueryType", ctypes.c_uint32),
                ("AdditionalParameters", ctypes.c_ubyte * 1),
            )

        class _Desc(ctypes.Structure):
            """``DEVICE_SEEK_PENALTY_DESCRIPTOR``：写数据要不要寻道。"""

            _fields_ = (
                ("Version", ctypes.c_uint32),
                ("Size", ctypes.c_uint32),
                ("IncursSeekPenalty", ctypes.c_ubyte),
            )

        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        )
        k32.DeviceIoControl.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        )
        k32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = k32.CreateFileW(
            device, 0, _FILE_SHARE_ALL, None, _OPEN_EXISTING, 0, None
        )
        if not handle or handle == ctypes.c_void_p(-1).value:
            return True
        try:
            query = _Query()
            query.PropertyId = _STORAGE_DEVICE_SEEK_PENALTY_PROPERTY
            query.QueryType = 0
            desc = _Desc()
            returned = ctypes.c_uint32(0)
            ok = k32.DeviceIoControl(
                handle,
                _IOCTL_STORAGE_QUERY_PROPERTY,
                ctypes.byref(query),
                ctypes.sizeof(query),
                ctypes.byref(desc),
                ctypes.sizeof(desc),
                ctypes.byref(returned),
                None,
            )
        finally:
            k32.CloseHandle(handle)
    except (OSError, AttributeError, TypeError, ValueError):
        return True
    if not ok or not returned.value:
        return True
    return bool(desc.IncursSeekPenalty)


def _copy_workers(dest: str) -> int:
    """复制并行线数：目标卷是 SSD 用 4 条，机械盘或探测不出用 1 条。

    看目标卷而不是源卷：瓶颈在写入侧。
    """
    if os.name != "nt":
        return _COPY_THREADS_HDD
    device = _device_path(dest)
    if not device:
        return _COPY_THREADS_HDD
    workers = _COPY_WORKERS_CACHE.get(device)
    if workers is None:
        workers = (
            _COPY_THREADS_HDD if _incurs_seek_penalty(device) else _COPY_THREADS_SSD
        )
        _COPY_WORKERS_CACHE[device] = workers
    return workers


_LONG_PREFIX = "\\\\?\\"


# 到这个长度就换扩展长度形式，给后面还可能拼上的名字留余量。
_LONG_AT = 240
