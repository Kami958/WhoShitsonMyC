"""应用日志：stdlib ``logging`` + 产品侧内存缓冲。

**边界（必须遵守）**

- 运行时业务 / 模块 / 扫描计时 → **只** 调本模块 ``debug/info/warn/error/exception``
- 不要 ``print`` / ``traceback.print_exc`` / 裸 ``logging.getLogger`` 记运行日志
- CLI 工具（``build.py``、``dev/bench_*``）面向终端的进度输出可以 ``print``，那不是应用日志
- 默认不落盘（仅内存环形缓冲）；设置页读缓冲；用户可手动导出
- 路径脱敏默认开启；写入时处理，关闭后只影响之后的新日志

环境：

- ``WSMC_LOG_LEVEL`` = DEBUG|INFO|WARN|ERROR
- ``WSMC_DEBUG=1`` → 未设前者时门槛 DEBUG
- ``WSMC_LOG_SANITIZE`` = 0/1（false/true）：仅当设置项未写明时生效；设置项优先
- 源码 + 门槛 ≤ DEBUG → 同步 mirror stderr（exe 不 mirror）
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import Sequence
from typing import Any

# 保留条数上限（内存）；满则丢最旧
_MAX_ENTRIES = 1024
_MAX_MSG_LEN = 4000
_MAX_TRACE_LEN = 12000

_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")
_LEVEL_RANK = {name: i for i, name in enumerate(_LEVELS)}

_TO_LOGGING = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
}
_FROM_LOGGING = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARN",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "ERROR",
}

_WIN_PATH_RE = re.compile(r"(?i)(?:[a-z]:\\|\\\\)[^\s\"'<>|]+")
_POSIX_PATH_RE = re.compile(
    r"(?<![a-zA-Z0-9_])/(?:Users|home|tmp|var|private)[^\s\"'<>|]*"
)
_FILE_URL_RE = re.compile(r"(?i)file:///[^\s\"'<>]+")
_USER_PROFILE_RE = re.compile(
    r"(?i)((?:Users|Documents and Settings))\\[^\\/\s\"'<>|]+"
)

_lock = threading.RLock()
_entries: deque[dict[str, Any]] = deque(maxlen=_MAX_ENTRIES)
_seq = 0
_env_summary: str = ""
_min_level: str | None = None
_min_level_explicit: bool = False
_configured = False
# 路径脱敏：默认开。由设置项同步；设置未写明时可读环境变量。
_sanitize_enabled: bool = True

LOGGER_NAME = "wsmc"
_logger = logging.getLogger(LOGGER_NAME)


class _LevelNameFilter(logging.Filter):
    """对外等级名用 WARN，不用 WARNING。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.wsmc_level = _FROM_LOGGING.get(record.levelno, "INFO")  # type: ignore[attr-defined]
        return True


class _SanitizeFilter(logging.Filter):
    """message / 异常栈截断；按开关决定是否路径脱敏。写入 ``exc_text`` 供各 Handler 共用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(getattr(record, "msg", "") or "")
        text = str(msg).strip() or "(empty)"
        if get_sanitize_enabled():
            text = sanitize(text)
        record.msg = _clip(text, _MAX_MSG_LEN)
        record.args = ()

        tb = ""
        if record.exc_info:
            try:
                if (
                    isinstance(record.exc_info, tuple)
                    and record.exc_info[0] is not None
                ):
                    tb = "".join(traceback.format_exception(*record.exc_info))
            except Exception:  # noqa: BLE001
                tb = ""
        if tb:
            if get_sanitize_enabled():
                tb = sanitize(tb)
            tb = _clip(tb, _MAX_TRACE_LEN)
            record.exc_text = tb
            # 避免 StreamHandler 再格式化出未脱敏栈
            record.exc_info = None
        record.wsmc_traceback = tb  # type: ignore[attr-defined]
        return True


class _MemoryHandler(logging.Handler):
    """写入进程内环形缓冲（设置页 / 导出）。"""

    def emit(self, record: logging.LogRecord) -> None:
        global _seq
        try:
            level = getattr(record, "wsmc_level", None) or _FROM_LOGGING.get(
                record.levelno, "INFO"
            )
            msg = record.getMessage()
            tb = getattr(record, "wsmc_traceback", None) or (record.exc_text or "")
            with _lock:
                _seq += 1
                _entries.append(
                    {
                        "id": _seq,
                        "ts": float(record.created or time.time()),
                        "level": level,
                        "message": msg,
                        "traceback": tb,
                    }
                )
        except Exception:  # noqa: BLE001
            self.handleError(record)


class _WsmcFormatter(logging.Formatter):
    """stderr 行格式；等级显示为 WARN 而非 WARNING。"""

    def format(self, record: logging.LogRecord) -> str:
        level = getattr(record, "wsmc_level", None) or _FROM_LOGGING.get(
            record.levelno, "INFO"
        )
        record.levelname = level
        return super().format(record)


_memory_handler = _MemoryHandler(level=logging.DEBUG)
_memory_handler.addFilter(_LevelNameFilter())
_memory_handler.addFilter(_SanitizeFilter())

_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setLevel(logging.DEBUG)
_stderr_handler.addFilter(_LevelNameFilter())
_stderr_handler.addFilter(_SanitizeFilter())
_stderr_handler.setFormatter(
    _WsmcFormatter("[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _parse_level(raw: str | None, default: str = "INFO") -> str:
    s = (raw or "").strip().upper()
    if s == "WARNING":
        s = "WARN"
    if s in _LEVEL_RANK:
        return s
    return default


def _resolve_min_level_from_env() -> str:
    raw = os.environ.get("WSMC_LOG_LEVEL", "").strip()
    if raw:
        return _parse_level(raw, "INFO")
    if _env_truthy("WSMC_DEBUG"):
        return "DEBUG"
    return "INFO"


def _should_mirror_stderr() -> bool:
    if getattr(sys, "frozen", False):
        return False
    return _LEVEL_RANK[get_min_level()] <= _LEVEL_RANK["DEBUG"]


def _ensure_configured() -> None:
    global _configured, _min_level
    with _lock:
        if _min_level is None:
            _min_level = _resolve_min_level_from_env()
        if not _configured:
            _logger.handlers.clear()
            _logger.propagate = False
            _logger.addHandler(_memory_handler)
            _configured = True
        _apply_level_locked()


def _apply_level_locked() -> None:
    level_name = _min_level or "INFO"
    py_level = _TO_LOGGING.get(level_name, logging.INFO)
    _logger.setLevel(py_level)
    _memory_handler.setLevel(py_level)
    has_stderr = _stderr_handler in _logger.handlers
    want_stderr = (not getattr(sys, "frozen", False)) and (
        _LEVEL_RANK[level_name] <= _LEVEL_RANK["DEBUG"]
    )
    if want_stderr and not has_stderr:
        _logger.addHandler(_stderr_handler)
    elif not want_stderr and has_stderr:
        _logger.removeHandler(_stderr_handler)
    if want_stderr:
        _stderr_handler.setLevel(py_level)


def get_min_level() -> str:
    _ensure_configured()
    with _lock:
        return _min_level or "INFO"


def set_min_level(level: str | None = None) -> str:
    """设置最低记录等级；``None`` 表示重新读环境。"""
    global _min_level, _min_level_explicit
    _ensure_configured()
    with _lock:
        if level is None:
            _min_level = _resolve_min_level_from_env()
            _min_level_explicit = False
        else:
            _min_level = _parse_level(str(level), "INFO")
            _min_level_explicit = True
        _apply_level_locked()
        return _min_level


def env_log_sanitize() -> bool | None:
    """读 ``WSMC_LOG_SANITIZE``。未设置返回 ``None``；无法识别也返回 ``None``。"""
    raw = os.environ.get("WSMC_LOG_SANITIZE", "").strip().lower()
    if not raw:
        return None
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return None


def get_sanitize_enabled() -> bool:
    """是否在写入日志时做路径脱敏（默认 True）。"""
    with _lock:
        return bool(_sanitize_enabled)


def set_sanitize_enabled(enabled: bool) -> bool:
    """设置路径脱敏开关；只影响之后新写入的日志。"""
    global _sanitize_enabled
    with _lock:
        _sanitize_enabled = bool(enabled)
        return _sanitize_enabled


def is_enabled(level: str) -> bool:
    lv = _parse_level(level, "INFO")
    return _LEVEL_RANK[lv] >= _LEVEL_RANK[get_min_level()]


def get_logger() -> logging.Logger:
    """底层 ``logging.Logger``。业务优先用模块级 ``info/warn/...``。"""
    _ensure_configured()
    return _logger


def collect_env_summary() -> str:
    """采集 CPU 核数、物理内存等非隐私调试信息（不含用户路径）。"""
    parts: list[str] = []
    try:
        cpu = os.cpu_count() or 0
        parts.append(f"cpu_logical={cpu}")
    except Exception:  # noqa: BLE001
        pass
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
    try:
        if os.name == "nt":
            ver = sys.getwindowsversion()
            parts.append(f"win={ver.major}.{ver.minor}.{ver.build}")
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


def log(
    level: str,
    message: str,
    *,
    exc_info: BaseException | bool | None = None,
) -> None:
    """写一条日志。低于门槛则丢弃。"""
    _ensure_configured()
    lv = _parse_level(level, "INFO")
    py_level = _TO_LOGGING.get(lv, logging.INFO)
    if not _logger.isEnabledFor(py_level):
        return

    ei: Any = None
    if exc_info is True:
        ei = True
    elif isinstance(exc_info, BaseException):
        ei = (type(exc_info), exc_info, exc_info.__traceback__)
    elif exc_info:
        ei = True

    _logger.log(py_level, message or "", exc_info=ei)


def debug(message: str) -> None:
    log("DEBUG", message)


def info(message: str) -> None:
    log("INFO", message)


def log_settings_changed(
    scope: str,
    changes: list[str] | Sequence[str] | None,
    *,
    level: str = "DEBUG",
) -> None:
    """记录设置项变更（统一出口）。

    - 仅在有变更片段时写入
    - 默认 **DEBUG**（日常 INFO 不刷屏；开 DEBUG 可审计）
    - ``changes`` 每项已是 ``key: old -> new``；**路径请传原文**，
      是否脱敏由写入管线的 sanitize 开关决定（开→替换，关→保留明文）

    示例::

        applog.log_settings_changed(
            "settings",
            ["scan_workers: 4 -> 8", "snapshot_dir: C:\\\\Data -> D:\\\\Snaps"],
        )
        # sanitize on  → snapshot_dir: <path> -> <path>
        # sanitize off → 保留绝对路径
    """
    parts = [str(x).strip() for x in (changes or []) if str(x or "").strip()]
    if not parts:
        return
    name = (scope or "settings").strip() or "settings"
    log(level, f"{name} changed | " + " | ".join(parts))


def log_settings_event(scope: str, event: str, *, level: str = "DEBUG") -> None:
    """设置相关非 diff 事件（如恢复默认、目录迁移摘要）。路径仍走 sanitize 管线。"""
    name = (scope or "settings").strip() or "settings"
    body = str(event or "").strip()
    if not body:
        return
    log(level, f"{name} | {body}")


def warn(message: str) -> None:
    log("WARN", message)


def error(message: str, *, exc: BaseException | bool | None = None) -> None:
    log("ERROR", message, exc_info=exc if exc is not None else False)


def exception(message: str, exc: BaseException | None = None) -> None:
    """错误 + 栈。优先用传入的 ``exc``。"""
    if exc is not None:
        log("ERROR", message, exc_info=exc)
    else:
        log("ERROR", message, exc_info=True)


def get_entries(limit: int | None = None) -> list[dict[str, Any]]:
    """日志副本（旧→新）。``limit`` 为正时只取最近 N 条。"""
    _ensure_configured()
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
    if get_sanitize_enabled():
        privacy = (
            "Privacy: path sanitization ON for new entries; "
            "absolute paths are redacted when written."
        )
    else:
        privacy = (
            "Privacy: path sanitization OFF; "
            "log may contain absolute paths and usernames."
        )
    lines: list[str] = [
        "WhoShitsOnMyC application log",
        privacy,
        f"Env: {env}",
        f"min_level: {get_min_level()}  sanitize: "
        f"{'on' if get_sanitize_enabled() else 'off'}  "
        f"buffer_cap: {_MAX_ENTRIES}  entries: {len(rows)}",
        f"backend: logging logger={LOGGER_NAME}",
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
    """启动：刷新门槛、环境摘要、一条 INFO。"""
    global _env_summary
    with _lock:
        explicit = _min_level_explicit
    if not explicit:
        set_min_level(None)
    else:
        _ensure_configured()
    _env_summary = collect_env_summary()
    ver = (version or "").strip()
    bits = f"v{ver} " if ver else ""
    info(
        f"App started {bits}| {_env_summary} "
        f"| log=memory+logging cap={_MAX_ENTRIES} min_level={get_min_level()}"
        f" sanitize={'on' if get_sanitize_enabled() else 'off'}"
    )
    debug(
        "applog debug enabled"
        f" | mirror_stderr={_should_mirror_stderr()}"
        f" | WSMC_LOG_LEVEL={os.environ.get('WSMC_LOG_LEVEL', '')!r}"
        f" | WSMC_DEBUG={os.environ.get('WSMC_DEBUG', '')!r}"
        f" | WSMC_LOG_SANITIZE={os.environ.get('WSMC_LOG_SANITIZE', '')!r}"
        f" | WSMC_SCAN_TIMING={os.environ.get('WSMC_SCAN_TIMING', '')!r}"
    )
