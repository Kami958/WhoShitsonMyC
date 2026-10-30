"""对比树目标路径的安全删除（回收站 / 永久）与黑名单匹配。

不依赖 AI / httpx；Windows 回收站走 shell32.SHFileOperationW。
"""

from __future__ import annotations

import os
import re
import stat
import time
from typing import Any

from . import applog

# 黑名单条数 / 单条 path 长度上限
_MAX_BLACKLIST = 200
_MAX_PATH_LEN = 512
_MODES = frozenset({"exact", "prefix", "regex"})

# 逐项进度两次上报之间的最小间隔（秒）
_PROGRESS_MIN_GAP_S = 0.12

# 固定盘 3 上的删除会进回收站；未知 0、可移动 2、远程 4、光驱 5、内存盘 6 上
# 系统会直接永久删除且不报错，不能当作回收站可用。
_RECYCLE_BLOCKED_DRIVE_TYPES = frozenset({0, 2, 4, 5, 6})

# 组策略禁止把删除放进回收站的值：1 表示永久删除。
_RECYCLE_POLICY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"
_RECYCLE_POLICY_VALUE = "NoRecycleFiles"

# 每个卷的回收站设置：NukeOnDelete 为 1 时该卷的删除直接永久删除。
_RECYCLE_BUCKET_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume"
)
_RECYCLE_NUKE_VALUE = "NukeOnDelete"

# Windows 扩展长度前缀：加上它后路径不再受 260 字符上限约束。
_WIN32_EXTENDED_PREFIX = "\\\\?\\"


class DeleteError(Exception):
    """可预期的删除失败（文案已可直接给前端）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def normalize_abs(path: str | None) -> str:
    """绝对路径规范化（expanduser + abspath）。"""
    if path is None:
        return ""
    s = str(path).strip()
    if not s:
        return ""
    try:
        return os.path.abspath(os.path.expanduser(s))
    except OSError:
        return s


def path_for_match(path: str) -> str:
    """用于黑名单比较的路径键（Windows 下 normcase）。"""
    abs_p = normalize_abs(path)
    if not abs_p:
        return ""
    if os.name == "nt":
        return os.path.normcase(abs_p)
    return abs_p


def is_drive_root(path: str | None) -> bool:
    """是否为盘符根（``C:\\`` / ``D:/`` 等）。"""
    abs_p = normalize_abs(path)
    if not abs_p:
        return False
    try:
        parent = os.path.dirname(abs_p.rstrip("\\/"))
        # 盘符根：dirname 去掉尾部分隔后为空或等于自身
        if os.name == "nt":
            drive, tail = os.path.splitdrive(abs_p)
            if not drive:
                return False
            rest = tail.replace("/", "\\").strip("\\")
            return rest == ""
        # POSIX：仅 /
        return abs_p == os.path.sep
    except OSError:
        return False


def is_under_root(root: str | None, target: str | None) -> bool:
    """target 是否位于 root 之下或与 root 相同（规范化后）。"""
    r = path_for_match(root)
    t = path_for_match(target)
    if not r or not t:
        return False
    if r == t:
        return True
    # 保证 root 以分隔符结尾再做前缀，避免 C:\Win 匹配 C:\Windows
    sep = "\\" if os.name == "nt" else os.sep
    root_pref = r if r.endswith(("/", "\\")) else r + sep
    return t.startswith(root_pref)


def resolve_compare_target(root: str, rel_path: str | None) -> str:
    """扫描根 + 相对路径 → 绝对路径。

    ``rel_path`` 为空表示根自身。禁止用绝对 rel 跳出（最终仍靠 is_under_root）。
    """
    root_abs = normalize_abs(root)
    if not root_abs:
        raise DeleteError("invalid root")
    rel = (rel_path or "").strip()
    if not rel:
        return root_abs
    # 拒绝绝对相对段
    if os.path.isabs(rel):
        # 仍 join 会忽略 root；直接当完整路径再校验 under
        return normalize_abs(rel)
    # 规范化 .. 段
    joined = os.path.normpath(os.path.join(root_abs, rel))
    return normalize_abs(joined)


def normalize_delete_blacklist(raw: Any) -> list[dict[str, str]]:
    """规范化黑名单：``[{path, mode}, ...]``。

    - 接受 list[dict] / list[str]（str → prefix）/ JSON 可解析字符串
    - mode: exact|prefix|regex，默认 prefix
    - 去空、截断长度、去重、上限 200
    - regex 模式：编译失败的条目丢弃（写入前应由 UI/API 再校验一次）
    """
    items: list = []
    if raw is None:
        items = []
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            items = []
        else:
            try:
                import json

                parsed = json.loads(text)
                if isinstance(parsed, list):
                    items = parsed
                else:
                    items = []
            except (ValueError, TypeError):
                items = []
    elif isinstance(raw, list):
        items = raw
    else:
        items = []

    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if isinstance(item, str):
            path = item.strip()
            mode = "prefix"
        elif isinstance(item, dict):
            path = str(item.get("path") or "").strip()
            mode = str(item.get("mode") or "prefix").strip().lower()
        else:
            continue
        if not path or len(path) > _MAX_PATH_LEN:
            continue
        if mode not in _MODES:
            mode = "prefix"
        if mode == "regex":
            try:
                re.compile(path)
            except re.error:
                continue
        key = (path_for_match(path) if mode != "regex" else path, mode)
        if key in seen:
            continue
        seen.add(key)
        out.append({"path": path, "mode": mode})
        if len(out) >= _MAX_BLACKLIST:
            break
    return out


def blacklist_entry_valid(path: str, mode: str) -> str | None:
    """校验单条；通过返回 None，否则返回错误说明（英文短句，调用方 i18n）。"""
    p = (path or "").strip()
    m = (mode or "prefix").strip().lower()
    if not p:
        return "empty"
    if len(p) > _MAX_PATH_LEN:
        return "too_long"
    if m not in _MODES:
        return "bad_mode"
    if m == "regex":
        try:
            re.compile(p)
        except re.error:
            return "bad_regex"
    return None


def _compile_entry(entry: dict[str, str]) -> re.Pattern[str] | None:
    path = (entry.get("path") or "").strip()
    mode = (entry.get("mode") or "prefix").strip().lower()
    if not path:
        return None
    flags = re.IGNORECASE if os.name == "nt" else 0
    try:
        if mode == "exact":
            key = path_for_match(path)
            return re.compile("^" + re.escape(key) + "$", flags)
        if mode == "prefix":
            base = path_for_match(path).rstrip("\\/")
            if not base:
                return None
            # 自身或子路径
            return re.compile(
                "^" + re.escape(base) + r"(?:[\\/]|$)",
                flags,
            )
        # regex：对匹配键做 search
        return re.compile(path, flags)
    except re.error:
        return None


def path_matches_blacklist(abs_path: str, entries: list[dict[str, str]] | None) -> bool:
    """绝对路径是否命中黑名单。"""
    if not entries:
        return False
    subject = path_for_match(abs_path)
    if not subject:
        return False
    for entry in entries:
        pat = _compile_entry(entry)
        if pat is None:
            continue
        mode = (entry.get("mode") or "prefix").strip().lower()
        if mode == "regex":
            if pat.search(subject):
                return True
        else:
            if pat.match(subject):
                return True
    return False


def evaluate_pending_candidate(
    root: str,
    rel_path: str | None,
    blacklist: list[dict[str, str]] | None,
    *,
    require_exists: bool = False,
) -> dict[str, Any]:
    """评估路径是否允许进入待删除列表（**不**执行删除）。

    用于 AI 提议 / 人工确认前的白名单与结构校验。默认不因「当前不存在」拒绝入队
    （执行删除时仍会再校验）；``require_exists=True`` 时与真删前一致。

    Returns:
        ``ok`` / ``code`` / ``path`` / ``root`` / ``rel``。
        ``code`` 为机器键：``ok`` | ``invalid`` | ``root`` | ``drive_root`` |
        ``outside`` | ``blacklist`` | ``missing``。
    """
    root_abs = normalize_abs(root)
    rel = (rel_path or "").strip()
    base: dict[str, Any] = {
        "ok": False,
        "code": "invalid",
        "path": "",
        "root": root_abs,
        "rel": rel,
    }
    if not root_abs:
        return base

    try:
        full = resolve_compare_target(root_abs, rel)
    except DeleteError as exc:
        base["code"] = str(exc.message or "invalid")
        return base
    except OSError:
        return base

    if not full:
        return base

    base["path"] = full

    if path_for_match(full) == path_for_match(root_abs):
        base["code"] = "root"
        return base

    if is_drive_root(full):
        base["code"] = "drive_root"
        return base

    if not is_under_root(root_abs, full):
        base["code"] = "outside"
        return base

    if path_matches_blacklist(full, blacklist or []):
        base["code"] = "blacklist"
        return base

    exists = os.path.lexists(full) if hasattr(os.path, "lexists") else os.path.exists(full)
    if require_exists and not exists:
        base["code"] = "missing"
        return base

    base["ok"] = True
    base["code"] = "ok"
    base["exists"] = bool(exists)
    return base


def assert_deletable(
    root: str,
    rel_path: str | None,
    blacklist: list[dict[str, str]] | None,
    *,
    zh_en: tuple[str, str] | None = None,
) -> str:
    """校验可删并返回绝对路径；失败抛 :class:`DeleteError`（消息已是最终文案需由调用方包装时可用 message）。

    本函数抛出的 message 为**机器键**风格短英文，``app.py`` 负责 i18n。
    为方便单测，也可直接抛出人类可读键：
    ``root`` / ``drive_root`` / ``outside`` / ``blacklist`` / ``missing`` / ``invalid``
    """
    result = evaluate_pending_candidate(
        root, rel_path, blacklist, require_exists=True
    )
    if not result.get("ok"):
        raise DeleteError(str(result.get("code") or "invalid"))
    return str(result.get("path") or "")


def _recycle_is_unc(abs_path: str) -> bool:
    """是否为 UNC 路径；扩展长度前缀的 UNC 写法同样算。

    带扩展长度前缀的本地盘写法不算 UNC。
    """
    p = str(abs_path).replace("/", "\\")
    if p[:8].upper() == "\\\\?\\UNC\\":
        return True
    if p.startswith("\\\\?\\"):
        # 带扩展长度前缀的本地盘路径不以网络共享开头
        return False
    return p.startswith("\\\\")


def _recycle_drive_type(abs_path: str) -> int:
    """GetDriveTypeW 探测盘类型；探测本身失败时抛出。"""
    import ctypes

    drive, _ = os.path.splitdrive(abs_path)
    root = drive + "\\" if drive else abs_path
    return int(ctypes.windll.kernel32.GetDriveTypeW(root))


def _recycle_policy_blocks() -> bool:
    """HKCU 或 HKLM 的 NoRecycleFiles 为 1 时，系统会永久删除而不进回收站。"""
    import winreg

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, _RECYCLE_POLICY_KEY) as key:
                value, _ = winreg.QueryValueEx(key, _RECYCLE_POLICY_VALUE)
        except OSError:
            continue
        try:
            blocked = int(value) == 1
        except (TypeError, ValueError):
            continue
        if blocked:
            return True
    return False


def _recycle_volume_nukes(abs_path: str) -> bool:
    """该卷是否设置了删除时不进回收站而直接永久删除。

    读取当前用户在每个卷上的回收站属性 ``NukeOnDelete``。查询不到卷名、
    打不开注册表项或读取失败时抛出，由调用方按不可用处理，不能假定可用。
    """
    import ctypes
    import winreg

    drive, _ = os.path.splitdrive(abs_path)
    root = drive + "\\" if drive else abs_path
    buf = ctypes.create_unicode_buffer(1024)
    kernel32 = ctypes.windll.kernel32
    get_volume = kernel32.GetVolumeNameForVolumeMountPointW
    get_volume.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    get_volume.restype = ctypes.c_bool
    if not get_volume(root, buf, len(buf)):
        raise OSError("volume name unavailable")
    volume = (buf.value or "").rstrip("\\")
    marker = "Volume"
    index = volume.find(marker)
    if index < 0:
        raise OSError("volume name malformed")
    guid = volume[index + len(marker):]
    key_path = _RECYCLE_BUCKET_KEY + "\\" + guid
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _ = winreg.QueryValueEx(key, _RECYCLE_NUKE_VALUE)
    except FileNotFoundError:
        return False
    try:
        return int(value) == 1
    except (TypeError, ValueError):
        return False


def _recycle_unavailable_reason(abs_path: str) -> str:
    """该位置能否真的用回收站；返回空串表示可用，否则返回内部短因。

    共享盘、可移动盘、光驱、内存盘和策略禁止回收站的位置上，系统会直接永久
    删除且不报错，因此这些位置必须先拒绝，不能交给 SHFileOperation 后按成功
    返回。探测本身出错时同样按不可用处理，无法确认就不执行删除。
    """
    try:
        if _recycle_is_unc(abs_path):
            return "unc"
        try:
            drive_type = _recycle_drive_type(abs_path)
        except (OSError, AttributeError, ValueError, TypeError):
            return "drive_type"
        if drive_type in _RECYCLE_BLOCKED_DRIVE_TYPES:
            return f"drive_type:{drive_type}"
        if _recycle_policy_blocks():
            return "policy"
        if _recycle_volume_nukes(abs_path):
            return "nuke_on_delete"
    except Exception:  # noqa: BLE001 - 探测失败时不能假定回收站可用
        return "unknown"
    return ""


def delete_to_recycle(abs_path: str) -> None:
    """移到回收站（Windows SHFileOperation）；失败抛 DeleteError，不降级永久删。

    调用 Shell 之前先确认该位置真的会把文件放进回收站。放在 SHFileOperation
    里没法得知结果：这些位置上传回值仍是成功，文件却已经永久删掉。
    """
    path = normalize_abs(abs_path)
    if not path:
        raise DeleteError("invalid")
    if os.name != "nt":
        # 非 Windows：无系统回收站封装时明确失败，避免静默永久删
        raise DeleteError("recycle_unsupported")
    if _recycle_unavailable_reason(path):
        raise DeleteError("recycle_unavailable")
    _shfile_delete(path, allow_undo=True)


def _extended_path(path: str) -> str:
    """加 Windows 扩展长度前缀，绕开 260 字符路径上限；其它平台原样返回。

    只用于面向文件系统的调用。Shell 的 SHFileOperation 不接受带前缀的路径，
    回收站一律用普通路径。
    """
    if os.name != "nt":
        return path
    p = str(path)
    if p.startswith(_WIN32_EXTENDED_PREFIX):
        return p
    p = p.replace("/", "\\")
    if p.startswith("\\\\"):
        # UNC：\\server\share → \\?\UNC\server\share
        return "\\\\?\\UNC\\" + p[2:]
    if not os.path.isabs(p):
        p = os.path.abspath(p)
    return _WIN32_EXTENDED_PREFIX + p


def _clear_readonly(path: str) -> bool:
    """清掉只读属性，返回是否真的改动过。

    符号链接、交叉点、占位文件跳过：Windows 上 ``os.chmod`` 会落到它们指向的
    目标上，删链接不该改动目标。
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if getattr(st, "st_reparse_tag", 0):
        return False
    if st.st_mode & stat.S_IWRITE:
        return False
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        return False
    return True


def _remove_file_or_link(path: str) -> None:
    """删单个文件、文件符号链接，或目录链接本身。

    目录链接（交叉点、目录符号链接）在 Windows 上只能用删空目录的方式去掉
    链接本身，普通文件与文件符号链接要用删文件的方式。按对象类型选一种，
    不在失败后换另一种：换用另一种只会把真实原因（文件被占用、没有权限）
    替换成“目录名称无效”，对定位没有任何帮助。
    """
    if os.path.isdir(path) and _is_reparse_path(path):
        os.rmdir(path)
        return
    os.unlink(path)


def _unlink_permanent(path: str) -> None:
    """删单个文件、链接或交叉点；被拒且带只读属性时清掉属性重试一次。

    两条路删的都是链接本身，不动它指向的目标。
    """
    try:
        _remove_file_or_link(path)
    except OSError:
        if not _clear_readonly(path):
            raise
        _remove_file_or_link(path)


def _remove_dir(path: str) -> None:
    """删掉已经空了的目录；被拒且带只读属性时清掉属性重试一次。"""
    try:
        os.rmdir(path)
    except OSError:
        if not _clear_readonly(path):
            raise
        os.rmdir(path)


def _bump(counter: dict, on_progress: Any) -> None:
    """记一项删完，按时间节流地报进度。

    每项都回一次会把进度推送（写进网页）变成删除本身的瓶颈，所以限最小间隔；
    第一项立即报，好让界面马上有东西可显示。
    """
    counter["items"] += 1
    if on_progress is None:
        return
    now = time.monotonic()
    if counter["items"] == 1 or now - counter["last"] >= _PROGRESS_MIN_GAP_S:
        counter["last"] = now
        on_progress({"items": counter["items"]})


def _is_reparse_entry(entry: os.DirEntry) -> bool:
    """这个目录项是不是重解析点（交叉点、目录符号链接等链接类目录）。

    交叉点在 Windows 上 ``is_dir(follow_symlinks=False)`` 照样返回真，只看它就会
    往链接指向的目录里递归，把别处的数据一起删掉，所以要单独看重解析点属性。
    """
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    attrs = int(getattr(st, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return bool(attrs & reparse_flag)


def _is_reparse_path(path: str) -> bool:
    """路径本身是不是重解析点，检查的是链接本身而不是它指向的目标。

    Windows 交叉点和卷挂载点的 ``os.path.islink`` 为假、``os.path.isdir`` 为真，
    只按这两个判断会把链接当成普通目录，递归进目标后删掉别处的数据。
    用 ``lstat`` 读链接自身的属性，交叉点、目录符号链接、文件符号链接与卷挂载点
    都会带重解析点标记；非 Windows 上该属性不存在时视为普通目录项。
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(getattr(st, "st_mode", 0)):
        return True
    attrs = int(getattr(st, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if attrs & reparse_flag:
        return True
    return bool(int(getattr(st, "st_reparse_tag", 0) or 0))


def _delete_tree(path: str, counter: dict, on_progress: Any) -> None:
    """递归删掉目录内容与目录本身，每删掉一项记一笔。

    先把一层列完再删（``shutil.rmtree`` 同款做法），边枚举边删在 Windows 上会漏项。
    链接类目录（交叉点、目录符号链接）不往里进，删链接本身，不动它指向的目标。
    """
    for entry in list(os.scandir(path)):
        if entry.is_dir(follow_symlinks=False) and not _is_reparse_entry(entry):
            _delete_tree(entry.path, counter, on_progress)
        else:
            _unlink_permanent(entry.path)
            _bump(counter, on_progress)
    _remove_dir(path)
    _bump(counter, on_progress)


def _os_error_code(exc: OSError) -> str:
    """把系统删除失败归类成可读机器码，供界面给出可处理的提示。

    Windows 错误 32、33 表示文件正被其他程序占用，5、19 表示没有权限。
    两类都带上出错的文件名，其余原因只给通用标记，不把系统原文透给用户。
    """
    winerror = int(getattr(exc, "winerror", 0) or 0)
    raw = str(getattr(exc, "filename", "") or "").rstrip("\\/")
    name = os.path.basename(raw.replace("/", "\\")) if raw else ""
    if winerror in (32, 33):
        return f"busy:{name}" if name else "busy"
    if winerror == 5:
        return f"denied:{name}" if name else "denied"
    if winerror == 19:
        return f"writeprotect:{name}" if name else "writeprotect"
    return "os"


def delete_permanent(abs_path: str, *, on_progress: Any = None) -> None:
    """彻底删除文件或目录树。

    只读项被系统拒绝（WinError 5）时清掉只读属性重试一次；不提升权限、
    不改 ACL，其余失败原因照原样抛出。
    根路径与整棵树的遍历、删除都用扩展长度路径，超过 260 字符的深层目录也能
    一次删完，不会删到一半停下。
    ``on_progress`` 在删掉若干项后收到 ``{"items": 已删项数}``，按时间节流，
    最后一项不保证再报一次；回收站路径交给系统一次调用，拿不到逐项进度。
    """
    path = normalize_abs(abs_path)
    if not path:
        raise DeleteError("invalid")
    long_path = _extended_path(path)
    if not (
        os.path.lexists(long_path)
        if hasattr(os.path, "lexists")
        else os.path.exists(long_path)
    ):
        raise DeleteError("missing")
    try:
        # 根节点是重解析点时只删链接本身，绝不进入它指向的目标。
        # 交叉点与卷挂载点的 islink 为假、isdir 为真，不能只靠这两个判断。
        if os.path.isdir(long_path) and not _is_reparse_path(long_path):
            _delete_tree(long_path, {"items": 0, "last": 0.0}, on_progress)
        else:
            _unlink_permanent(long_path)
            if on_progress is not None:
                on_progress({"items": 1})
    except OSError as exc:
        winerror = int(getattr(exc, "winerror", 0) or 0)
        applog.warn(
            "delete_permanent failed"
            f" | path={path}"
            f" | reason={type(exc).__name__}"
            f" | winerror={winerror}"
        )
        raise DeleteError(_os_error_code(exc)) from exc


def delete_path(abs_path: str, *, permanent: bool = False, on_progress: Any = None) -> None:
    """执行删除。"""
    if permanent:
        delete_permanent(abs_path, on_progress=on_progress)
    else:
        delete_to_recycle(abs_path)


def _shfile_delete(path: str, *, allow_undo: bool) -> None:
    """Windows SHFileOperationW FO_DELETE。"""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", wintypes.LPVOID),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 3
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040
    FOF_NOERRORUI = 0x0400
    FOF_WANTNUKEWARNING = 0x4000

    flags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI
    if allow_undo:
        # 回收站不可用时 Shell 仍可能直接永久删除；要求它在真正永久删除前
        # 给出警告，不能带着“静默且不确认”的标志直接返回成功。
        flags |= FOF_ALLOWUNDO | FOF_WANTNUKEWARNING
    else:
        flags |= FOF_WANTNUKEWARNING

    # 双 NUL 结尾路径列表（缓冲区多留一个 NUL，value 只写 path）
    from_buf = ctypes.create_unicode_buffer(len(path) + 2)
    from_buf.value = path

    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(from_buf, wintypes.LPCWSTR)
    op.pTo = None
    op.fFlags = flags
    op.fAnyOperationsAborted = False
    op.hNameMappings = None
    op.lpszProgressTitle = None

    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    ret = int(shell32.SHFileOperationW(ctypes.byref(op)))
    if ret != 0 or op.fAnyOperationsAborted:
        raise DeleteError(f"recycle:{ret}")
