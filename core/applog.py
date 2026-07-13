"""进程内应用日志 —— 默认不写文件，仅内存环形缓冲。

用于设置页「日志」查看与手动导出。刻意**不**记录扫描中的具体路径、
用户选择的目录全文等隐私量大数据；错误栈会写入，但对绝对路径做脱敏。
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
import traceback
from collections import deque
from typing import Any

# 保留条数上限（内存）；满则丢最旧
_MAX_ENTRIES = 1024
# 单条消息截断，防止异常文本爆炸
_MAX_MSG_LEN = 4000
_MAX_TRACE_LEN = 12000

_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")

# Windows / Unix 风格绝对路径（尽量匹配，再替换为 <path>）
_WIN_PATH_RE = re.compile(
    r"(?i)(?:[a-z]:\\|\\\\)[^\s\"'<>|]+"
)
_POSIX_PATH_RE = re.compile(r"(?<![a-zA-Z0-9_])/(?:Users|home|tmp|var|private)[^\s\"'<>|]*")
# file:// URL
_FILE_URL_RE = re.compile(r"(?i)file:///[^\s\"'<>]+")
# 用户目录片段（Windows 常见；路径未整段命中时兜底）
_USER_PROFILE_RE = re.compile(
    r"(?i)((?:Users|Documents and Settings))\\[^\\/\s\"'<>|]+"
)

_lock = threading.RLock()
_entries: deque[dict[str, Any]] = deque(maxlen=_MAX_ENTRIES)
_seq = 0
# 启动时采集的非隐私环境摘要（CPU / 内存等）
_env_summary: str = ""


def collect_env_summary() -> str:
    """采集 CPU 核数、物理内存等非隐私调试信息（不含用户路径）。"""
    parts: list[str] = []
    try:
        cpu = os.cpu_count() or 0
        parts.append(f"cpu_logical={cpu}")
    except Exception:  # noqa: BLE001
        pass
    # 物理内存（Windows GlobalMemoryStatusEx / POSIX sysconf）
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_uint64),
                    ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64),
                    ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64),
                    ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                total_gb = stat.ullTotalPhys / (1024 ** 3)
                avail_gb = stat.ullAvailPhys / (1024 ** 3)
                parts.append(f"ram_total_gb={total_gb:.1f}")
                parts.append(f"ram_avail_gb={avail_gb:.1f}")
                parts.append(f"ram_load_pct={stat.dwMemoryLoad}")
        else:
            page = os.sysconf("SC_PAGE_SIZE")
            phys = os.sysconf("SC_PHYS_PAGES")
            if page > 0 and phys > 0:
                total_gb = (page * phys) / (1024 ** 3)
                parts.append(f"ram_total_gb={total_gb:.1f}")
    except Exception:  # noqa: BLE001
        pass
    try:
        parts.append(f"python={sys.version.split()[0]}")
        parts.append(f"platform={sys.platform}")
        parts.append(f"arch={getattr(sys, 'maxsize', 0).bit_length()}bit")
    except Exception:  # noqa: BLE001
        pass
    try:
        parts.append(f"pid={os.getpid()}")
    except Exception:  # noqa: BLE001
        pass
    try:
        parts.append(f"frozen={bool(getattr(sys, 'frozen', False))}")
    except Exception:  # noqa: BLE001
        pass
    # Windows 版本号（无用户信息）
    try:
        if os.name == "nt":
            ver = sys.getwindowsversion()
            parts.append(
                f"win={ver.major}.{ver.minor}.{ver.build}"
            )
    except Exception:  # noqa: BLE001
        pass
    return " ".join(parts) if parts else "env=unknown"


def get_env_summary() -> str:
    return _env_summary or ""


def sanitize(text: str) -> str:
    """去掉/替换日志里的绝对路径与用户名目录，降低隐私泄露。"""
    if not text:
        return ""
    s = str(text)
    # 先替换已知 home
    try:
        home = os.path.expanduser("~")
        if home and len(home) > 2 and home in s:
            s = s.replace(home, "<home>")
    except Exception:  # noqa: BLE001
        pass
    try:
        local = os.environ.get("LOCALAPPDATA") or ""
        if local and local in s:
            s = s.replace(local, "<localappdata>")
        appdata = os.environ.get("APPDATA") or ""
        if appdata and appdata in s:
            s = s.replace(appdata, "<appdata>")
    except Exception:  # noqa: BLE001
        pass
    s = _FILE_URL_RE.sub("<file-url>", s)
    s = _WIN_PATH_RE.sub("<path>", s)
    s = _POSIX_PATH_RE.sub("<path>", s)
    s = _USER_PROFILE_RE.sub(r"\1\\<user>", s)
    return s


def _clip(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 3] + "..."


def log(level: str, message: str, *, exc_info: BaseException | bool | None = None) -> None:
    """追加一条日志。``exc_info`` 为 True 时取当前异常；也可直接传异常对象。"""
    global _seq
    lv = (level or "INFO").upper()
    if lv not in _LEVELS:
        lv = "INFO"
    msg = _clip(sanitize((message or "").strip() or "(empty)"), _MAX_MSG_LEN)
    tb_text = ""
    if exc_info is True:
        tb_text = traceback.format_exc()
    elif isinstance(exc_info, BaseException):
        tb_text = "".join(
            traceback.format_exception(type(exc_info), exc_info, exc_info.__traceback__)
        )
    elif exc_info:
        tb_text = traceback.format_exc()
    if tb_text:
        tb_text = _clip(sanitize(tb_text), _MAX_TRACE_LEN)

    with _lock:
        _seq += 1
        _entries.append(
            {
                "id": _seq,
                "ts": time.time(),
                "level": lv,
                "message": msg,
                "traceback": tb_text,
            }
        )


def debug(message: str) -> None:
    log("DEBUG", message)


def info(message: str) -> None:
    log("INFO", message)


def warn(message: str) -> None:
    log("WARN", message)


def error(message: str, *, exc: BaseException | bool | None = None) -> None:
    log("ERROR", message, exc_info=exc if exc is not None else False)


def exception(message: str, exc: BaseException | None = None) -> None:
    """记录错误 + 栈。优先用传入的 ``exc``，否则取当前异常上下文。"""
    if exc is not None:
        log("ERROR", message, exc_info=exc)
    else:
        log("ERROR", message, exc_info=True)


def get_entries(limit: int | None = None) -> list[dict[str, Any]]:
    """返回日志副本（旧→新）。``limit`` 为正时只取最近 N 条。"""
    with _lock:
        items = list(_entries)
    if limit is not None and limit > 0 and len(items) > limit:
        items = items[-int(limit) :]
    return items


def clear() -> int:
    """清空缓冲，返回清除条数。"""
    with _lock:
        n = len(_entries)
        _entries.clear()
    return n


def count() -> int:
    with _lock:
        return len(_entries)


def format_export(entries: list[dict[str, Any]] | None = None) -> str:
    """导出为纯文本。"""
    rows = entries if entries is not None else get_entries()
    env = _env_summary or collect_env_summary()
    lines: list[str] = [
        "WhoShitsOnMyC application log",
        "Privacy: absolute paths redacted; scan paths are not recorded.",
        f"Env: {env}",
        f"buffer_cap: {_MAX_ENTRIES}  entries: {len(rows)}",
        "-" * 60,
    ]
    for e in rows:
        ts = float(e.get("ts") or 0)
        try:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        except (OverflowError, OSError, ValueError):
            stamp = str(ts)
        lines.append(f"[{stamp}] {e.get('level') or 'INFO'}: {e.get('message') or ''}")
        tb = (e.get("traceback") or "").rstrip()
        if tb:
            lines.append(tb)
            lines.append("-" * 40)
    lines.append("")
    return "\n".join(lines)


def note_startup(version: str = "") -> None:
    """进程启动时记环境摘要 + 一条启动信息（无用户路径）。"""
    global _env_summary
    _env_summary = collect_env_summary()
    ver = (version or "").strip()
    bits = f"v{ver} " if ver else ""
    info(
        f"App started {bits}| {_env_summary} "
        f"| log=memory-only cap={_MAX_ENTRIES}"
    )
