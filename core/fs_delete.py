"""对比树目标路径的安全删除（回收站 / 永久）与黑名单匹配。

不依赖 AI / httpx；Windows 回收站走 shell32.SHFileOperationW。
"""

from __future__ import annotations

import os
import re
import shutil
from typing import Any

# 黑名单条数 / 单条 path 长度上限
_MAX_BLACKLIST = 200
_MAX_PATH_LEN = 512
_MODES = frozenset({"exact", "prefix", "regex"})


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


def delete_to_recycle(abs_path: str) -> None:
    """移到回收站（Windows SHFileOperation）；失败抛 DeleteError，不降级永久删。"""
    path = normalize_abs(abs_path)
    if not path:
        raise DeleteError("invalid")
    if os.name != "nt":
        # 非 Windows：无系统回收站封装时明确失败，避免静默永久删
        raise DeleteError("recycle_unsupported")
    _shfile_delete(path, allow_undo=True)


def delete_permanent(abs_path: str) -> None:
    """永久删除文件或目录树。"""
    path = normalize_abs(abs_path)
    if not path:
        raise DeleteError("invalid")
    if not (os.path.lexists(path) if hasattr(os.path, "lexists") else os.path.exists(path)):
        raise DeleteError("missing")
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    except OSError as exc:
        raise DeleteError(f"os:{exc}") from exc


def delete_path(abs_path: str, *, permanent: bool = False) -> None:
    """执行删除。"""
    if permanent:
        delete_permanent(abs_path)
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
        flags |= FOF_ALLOWUNDO
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
