"""应用数据目录、设置与快照存放位置。

数据目录按当前运行账户解析并验证访问。默认快照目录在数据目录下的
snapshots，用户可以改到其他文件夹并把已有快照迁过去。
应用设置默认不落盘：无 settings.yaml 时用内置默认。用户改过设置后自动写入
该文件，恢复默认会删除文件并把内存值重置为内置默认。
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import threading
import uuid
from collections.abc import Callable

from .compress import (
    drop_cache_for,
    is_snapshot_filename,
)
from . import applog
from . import user_dirs
from . import wingeom
from . import store_paths
from .corrupt_backup import backup_corrupt_file

from .store_paths import (
    _is_rotational_system_drive,
    _same_dir,
    default_scan_workers,
    is_rotational_drive,
    migrate_data_root,
    migrate_snapshots,
    probe_writable_dir,
)

from .store_values import (
    _AI_CLEANUP_DEPTH_MAX,
    _AI_CLEANUP_DEPTH_MIN,
    _AI_DEFAULTS,
    _WORKERS_MAX,
    _WORKERS_MIN,
    _as_bool,
    _as_int,
    _as_str,
    _clamp_workers,
    _default_enabled_tools,
    _delete_blacklist_to_yaml,
    _enabled_tools_to_yaml,
    _model_options_to_yaml,
    _normalize_cleanup_max_depth,
    _normalize_delete_blacklist,
    _normalize_enabled_tools,
    _normalize_lang,
    _normalize_log_level,
    _normalize_model_options,
    _normalize_pinned_snapshots,
    _normalize_snapshot_dir,
    _normalize_theme,
    _normalize_ui_hover_group_min,
    _normalize_ui_toast_seconds,
    _normalize_ui_tool_panel_width,
    _normalize_window_size,
    _parse_bool,
    _pinned_snapshots_to_yaml,
    _put_raw_pair,
    _yaml_quote,
    _yaml_unquote,
    resolve_theme,
)

from .store_yaml import (
    _common_view,
    _load_settings_yaml,
    _read_settings_yaml,
    _ui_view,
    _write_settings_yaml,
)

_APP_DIR_NAME = "WhoShitsOnMyC"
_SETTINGS_FILE = "settings.yaml"


class StoreError(Exception):
    """快照存储管理相关错误。"""


class SnapshotDirUnavailable(OSError):
    """当前生效的快照目录无法创建或当前账户不能写入。

    ``reason`` 是机器码，由界面层翻译成完整文案；``path`` 是出问题的目录。
    """

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"snapshot dir unavailable: {path or '(empty)'} ({reason})")
        self.path = path
        self.reason = reason or "unknown"


class DataDirUnavailable(OSError):
    """默认或用户选择的文件夹不能作为应用数据根使用。

    ``reason`` 是机器码，由界面层翻译成完整文案；``path`` 是出问题的文件夹。
    """

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"data folder unavailable: {path or '(empty)'} ({reason})")
        self.path = path
        self.reason = reason or "unknown"


# 设置文件读取失败时的会话状态：失败原因、损坏副本与对应文件。
_settings_recovered_path = ""
_settings_recovered_reason = ""
_settings_recovered_backup = ""


# 卸载 wipe 后置位：禁止再 makedirs 把 WhoShitsOnMyC 建回来（直到进程退出）
_data_wiped = False


# 数据目录只在启动初始化时确定，解析对象是当前运行账户。
_app_data_root = ""
_storage_initialized = False
_storage_ok = False
_storage_state: dict = {}
_current_user: user_dirs.CurrentUser | None = None
_DATA_DIR_POINTER_SUFFIX = ".data-dir"
_storage_lock = threading.RLock()


def _pointer_path() -> str:
    """用户主动选过的数据目录记录，位于当前运行账户的本地数据目录下。"""
    user = _current_user
    if user is None or not user.local_app_data:
        return ""
    return os.path.join(
        user.local_app_data, _APP_DIR_NAME + _DATA_DIR_POINTER_SUFFIX
    )


def _read_data_dir_pointer() -> tuple[str, bool]:
    """读取用户主动选择过的数据目录。

    返回 ``(path, invalid)``。没有记录时是 ``("", False)``；记录存在但
    读不出或内容不是绝对路径时是 ``("", True)``，调用方据此提示用户。
    """
    path = _pointer_path()
    if not path or not os.path.isfile(path):
        return "", False
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read(4096).strip()
    except OSError as exc:
        applog.warn(
            f"data folder pointer unreadable | reason={type(exc).__name__}"
        )
        return "", True
    if not value or not os.path.isabs(value):
        applog.warn("data folder pointer invalid | reason=not_absolute")
        return "", True
    return os.path.normpath(value), False


def _write_data_dir_pointer(target: str) -> bool:
    """记住用户主动选择的数据目录，下次启动继续使用。"""
    path = _pointer_path()
    if not path:
        return False
    temp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(temp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(target + "\n")
        os.replace(temp, path)
        return True
    except Exception as exc:  # noqa: BLE001 - 记住失败只能退回本次会话
        applog.warn(
            "data folder pointer write failed"
            f" | reason={type(exc).__name__}"
        )
        try:
            if os.path.exists(temp):
                os.remove(temp)
        except OSError:
            pass
        return False


def _probe_writable_dir(path: str) -> str:
    """验证当前运行账户能否写入目录。"""
    return store_paths.probe_writable_dir(path)


def _path_within(path: str, root: str) -> bool:
    """路径是否位于 root 之内（同一路径也算）。"""
    if not path or not root:
        return False
    try:
        p = os.path.normcase(os.path.abspath(path))
        r = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([p, r]) == r
    except (OSError, ValueError):
        return False


def initialize_storage() -> dict:
    """确定当前运行账户的数据目录并读取设置。

    数据目录跟随当前运行账户，不跨账户复制数据。
    解析或验证失败时不创建目录、不写入设置，调用方据此暂停扫描与快照。
    """
    with _storage_lock:
        return _initialize_storage_locked()


def _initialize_storage_locked() -> dict:
    """在锁内执行一次启动存储初始化。"""
    global _storage_initialized, _storage_ok, _app_data_root, _storage_state
    global _current_user
    if _storage_initialized:
        return dict(_storage_state)
    _storage_initialized = True

    try:
        user = user_dirs.open_current_user()
    except user_dirs.UserLookupError as exc:
        _storage_ok = False
        _storage_state = {
            "ok": False,
            "app_data_dir": "",
            "path": "",
            "reason": exc.reason,
            "account": user_dirs.current_account_name(),
            "pointer_invalid": False,
        }
        applog.warn(f"current user lookup failed | reason={exc.reason}")
        return dict(_storage_state)
    except Exception as exc:  # noqa: BLE001
        _storage_ok = False
        _storage_state = {
            "ok": False,
            "app_data_dir": "",
            "path": "",
            "reason": "identity_unavailable",
            "account": user_dirs.current_account_name(),
            "pointer_invalid": False,
        }
        applog.exception("current user lookup failed", exc)
        return dict(_storage_state)

    _current_user = user
    stored, pointer_invalid = _read_data_dir_pointer()
    root = (
        _resolve_data_root(stored)
        if stored
        else os.path.join(user.local_app_data, _APP_DIR_NAME)
    )

    reason = _probe_writable_dir(root)
    if reason:
        _storage_ok = False
        _storage_state = {
            "ok": False,
            "app_data_dir": root,
            "path": root,
            "reason": reason,
            "account": user.account,
            "pointer_invalid": pointer_invalid,
        }
        applog.warn(
            "data folder unavailable for current user"
            f" | reason={reason}"
        )
        return dict(_storage_state)

    _app_data_root = root
    _clear_settings_recovery()
    try:
        _apply_loaded(_recover_settings_document(settings_path()))
    except OSError as exc:
        applog.exception("read settings after storage init failed", exc)

    _storage_ok = True
    _storage_state = {
        "ok": True,
        "app_data_dir": root,
        "account": user.account,
        "pointer_invalid": pointer_invalid,
    }
    return dict(_storage_state)


def _app_base_dir_path() -> str:
    """应用数据根路径（**不**创建目录）。

    ``initialize_storage`` 已经按当前运行账户验证过；未初始化时先初始化。
    失败时抛 :class:`DataDirUnavailable`，不把数据写进未经验证的位置。
    """
    if _app_data_root:
        return _app_data_root
    state = initialize_storage()
    if not state.get("ok"):
        raise DataDirUnavailable(
            str(state.get("path") or ""),
            str(state.get("reason") or "unknown"),
        )
    return _app_data_root


def _safe_app_base_path() -> str:
    """尽量取应用数据根路径，只用于错误提示，取不到返回空串。"""
    try:
        return _app_base_dir_path()
    except OSError:
        return ""


def storage_ready() -> bool:
    """数据目录是否已经按当前运行账户验证可用。"""
    return bool(initialize_storage().get("ok"))


def preferred_pick_dir() -> str:
    """原生对话框的起始目录，必须是当前运行账户能访问的位置。"""
    user = _current_user
    if user is None:
        initialize_storage()
        user = _current_user
    if user is None:
        return ""
    for candidate in (user.home_dir, user.public_dir, user.local_app_data):
        if user.accessible_dir(candidate):
            return candidate
    return ""


def _storage_reason(exc: OSError) -> str:
    """把创建目录失败的原因收成机器键，由界面层翻译成完整文案。"""
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
    if not _safe_app_base_path():
        return "no_base"
    return "unknown"


def storage_error_payload(exc: OSError) -> dict:
    """目录创建失败的结构化结果，供 API 转成用户可读文案。"""
    filename = getattr(exc, "filename", "") or ""
    path = str(getattr(exc, "path", "") or filename or _safe_app_base_path())
    reason = str(getattr(exc, "reason", "") or _storage_reason(exc))
    return {
        "error": "storage_unavailable",
        "path": path,
        "reason": reason,
    }


def storage_status() -> dict:
    """检查应用数据根与当前生效的快照目录。

    数据目录不可用时返回 ``ok`` 为假，并带路径、原因与当前运行账户；
    自定义快照目录不可写时 ``ok`` 仍为 True，另带不可用标记。
    """
    state = initialize_storage()
    if not state.get("ok"):
        return dict(state)
    base = _app_data_root
    account = str(state.get("account") or "")
    if _snapshot_dir:
        reason = _probe_writable_dir(_snapshot_dir)
        info = {
            "ok": True,
            "app_data_dir": base,
            "account": account,
            "snapshot_dir": _snapshot_dir,
            "snapshot_dir_is_custom": True,
        }
        if reason:
            info.update(
                {
                    "snapshot_dir_unavailable": True,
                    "path": _snapshot_dir,
                    "reason": reason,
                }
            )
        return info

    try:
        snap = builtin_snapshot_dir()
    except OSError as exc:
        info = storage_error_payload(exc)
        info["app_data_dir"] = base
        info["account"] = account
        info["path"] = info.get("path") or os.path.join(base, "snapshots")
        info["ok"] = False
        info["snapshot_dir_unavailable"] = True
        return info
    reason = _probe_writable_dir(snap)
    if reason:
        return {
            "ok": False,
            "app_data_dir": base,
            "account": account,
            "path": snap,
            "reason": reason,
            "snapshot_dir_unavailable": True,
        }
    return {
        "ok": True,
        "app_data_dir": base,
        "account": account,
        "snapshot_dir": snap,
    }


_LEGACY_IMPORT_FILES = (_SETTINGS_FILE, "links.json", "migration_history.json")


def _data_folder_is_empty() -> bool:
    """当前数据文件夹是否还没有设置文件和快照。"""
    root = _app_data_root
    if not root:
        return True
    if os.path.isfile(os.path.join(root, _SETTINGS_FILE)):
        return False
    snap_dir = os.path.join(root, "snapshots")
    try:
        with os.scandir(snap_dir) as it:
            for item in it:
                try:
                    if item.is_file() and is_snapshot_filename(item.name):
                        return False
                except OSError:
                    continue
    except OSError:
        return True
    return True


def _legacy_data_candidate_paths() -> list[dict]:
    """列出其他 Windows 账户下可能存在的旧版本数据文件夹。

    只读检测，不复制、不修改。当前账户的数据文件夹不列入；不可读的
    位置会保留在结果中，供界面提示用户确认。
    """
    user = _current_user
    if user is None or not user.local_app_data or not user.home_dir:
        return []
    current_local = os.path.normcase(os.path.abspath(user.local_app_data))
    users_root = os.path.dirname(os.path.abspath(user.home_dir))
    try:
        entries = list(os.scandir(users_root))
    except OSError:
        return []
    found = []
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        local = os.path.join(entry.path, "AppData", "Local", _APP_DIR_NAME)
        try:
            if os.path.normcase(os.path.abspath(local)) == current_local:
                continue
        except OSError:
            continue
        try:
            with os.scandir(local) as it:
                local_entries = list(it)
        except FileNotFoundError:
            continue
        except PermissionError:
            # 目录可能存在但当前账户无权读取，仍然提示用户去确认。
            found.append({
                "path": local,
                "account": entry.name,
                "has_settings": False,
                "snapshots": 0,
                "readable": False,
                "unverified": True,
            })
            continue
        except OSError:
            continue
        has_settings = any(
            item.name == _SETTINGS_FILE and item.is_file()
            for item in local_entries
        )
        snap_dir = os.path.join(local, "snapshots")
        snapshots = 0
        readable = True
        if os.path.isdir(snap_dir):
            try:
                with os.scandir(snap_dir) as it:
                    for item in it:
                        try:
                            if item.is_file() and is_snapshot_filename(item.name):
                                snapshots += 1
                        except OSError:
                            readable = False
            except PermissionError:
                readable = False
            except OSError:
                readable = False
        elif not has_settings:
            continue
        if not has_settings and snapshots == 0:
            continue
        found.append({
            "path": local,
            "account": entry.name,
            "has_settings": has_settings,
            "snapshots": snapshots,
            "readable": readable,
        })
    found.sort(key=lambda item: (item["account"].lower(), item["path"].lower()))
    return found


def _legacy_data_roots() -> list[dict]:
    """当前数据文件夹为空时，返回可供提示的旧数据文件夹。"""
    if not _data_folder_is_empty():
        return []
    return _legacy_data_candidate_paths()


def legacy_data_candidates() -> list[dict]:
    """返回可以导入的旧数据文件夹（只读检测）。"""
    with _storage_lock:
        state = initialize_storage()
        if not state.get("ok"):
            return []
        return _legacy_data_roots()


def _copy_legacy_file(src: str, dst: str) -> None:
    """复制文件到目标位置，先写临时文件再替换。"""
    tmp = f"{dst}.wsmc-{uuid.uuid4().hex}.tmp"
    try:
        shutil.copy2(src, tmp)
        if os.path.getsize(tmp) != os.path.getsize(src):
            raise OSError("size_mismatch")
        os.replace(tmp, dst)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _same_file_size(a: str, b: str) -> bool:
    try:
        return os.path.getsize(a) == os.path.getsize(b)
    except OSError:
        return False


def _same_file_content(a: str, b: str) -> bool:
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                ca = fa.read(65536)
                cb = fb.read(65536)
                if ca != cb:
                    return False
                if not ca:
                    return True
    except OSError:
        return False


def _conflict_entry(
    name: str, relative: str, legacy_path: str, current_path: str
) -> dict:
    def describe(path: str) -> dict:
        try:
            st = os.stat(path)
        except OSError:
            return {"size": 0, "mtime": 0}
        return {"size": st.st_size, "mtime": int(st.st_mtime)}

    return {
        "name": name,
        "relative": relative,
        "current": describe(current_path),
        "legacy": describe(legacy_path),
    }


def _legacy_conflicts(source: str, dest: str) -> dict:
    """找出导入时会与当前数据文件夹同名的文件。"""
    result = {"settings": [], "snapshots": []}
    for name in _LEGACY_IMPORT_FILES:
        src = os.path.join(source, name)
        dst = os.path.join(dest, name)
        if not os.path.isfile(src) or not os.path.isfile(dst):
            continue
        if _same_file_content(src, dst):
            continue
        result["settings"].append(_conflict_entry(name, name, src, dst))
    snap_src = os.path.join(source, "snapshots")
    snap_dst = os.path.join(dest, "snapshots")
    if os.path.isdir(snap_src) and os.path.isdir(snap_dst):
        try:
            with os.scandir(snap_src) as it:
                for item in it:
                    try:
                        if not item.is_file() or not is_snapshot_filename(item.name):
                            continue
                    except OSError:
                        continue
                    target = os.path.join(snap_dst, item.name)
                    if not os.path.isfile(target):
                        continue
                    if _same_file_size(item.path, target):
                        continue
                    result["snapshots"].append(
                        _conflict_entry(
                            item.name, f"snapshots/{item.name}", item.path, target
                        )
                    )
        except OSError:
            pass
    result["snapshots"].sort(key=lambda item: item["name"].lower())
    return result


def _legacy_source_or_raise(path: str) -> str:
    source = os.path.abspath(path or "")
    candidates = {
        os.path.normcase(os.path.abspath(item["path"]))
        for item in _legacy_data_candidate_paths()
    }
    if os.path.normcase(source) not in candidates:
        raise DataDirUnavailable(source, "not_legacy")
    return source


def scan_legacy_conflicts(path: str) -> dict:
    """扫描导入旧数据时会遇到的同名文件，供用户选择保留哪一份。"""
    with _storage_lock:
        state = initialize_storage()
        if not state.get("ok"):
            raise DataDirUnavailable(
                "", str(state.get("reason") or "storage_unavailable")
            )
        source = _legacy_source_or_raise(path)
        return _legacy_conflicts(source, _app_base_dir_path())


def import_legacy_data(path: str, choices: dict | None = None) -> dict:
    """把其他账户下的旧数据复制到当前数据文件夹。

    没有同名的文件直接复制。同名文件按 choices 选择保留哪一份：
    键是相对路径，值是 current 或 legacy。没有选择时保留当前版本。
    旧位置保持原样，不做移动或删除。
    """
    with _storage_lock:
        state = initialize_storage()
        if not state.get("ok"):
            raise DataDirUnavailable(
                "", str(state.get("reason") or "storage_unavailable")
            )
        source = _legacy_source_or_raise(path)
        dest = _app_base_dir_path()
        decisions = choices or {}
        result = {
            "copied": 0,
            "overwritten": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],
        }

        def import_file(src: str, dst: str, relative: str, compare) -> None:
            try:
                if not os.path.isfile(src):
                    return
            except OSError as exc:
                result["failed"] += 1
                result["errors"].append(f"{relative}: {type(exc).__name__}")
                return
            if os.path.isfile(dst):
                if compare(src, dst):
                    result["skipped"] += 1
                    return
                if decisions.get(relative) != "legacy":
                    result["skipped"] += 1
                    return
                try:
                    _copy_legacy_file(src, dst)
                    result["overwritten"] += 1
                except OSError as exc:
                    result["failed"] += 1
                    result["errors"].append(f"{relative}: {type(exc).__name__}")
                return
            try:
                _copy_legacy_file(src, dst)
                result["copied"] += 1
            except OSError as exc:
                result["failed"] += 1
                result["errors"].append(f"{relative}: {type(exc).__name__}")

        for name in _LEGACY_IMPORT_FILES:
            import_file(
                os.path.join(source, name),
                os.path.join(dest, name),
                name,
                _same_file_content,
            )
        snap_src = os.path.join(source, "snapshots")
        snap_dst = os.path.join(dest, "snapshots")
        if os.path.isdir(snap_src):
            try:
                os.makedirs(snap_dst, exist_ok=True)
            except OSError as exc:
                result["failed"] += 1
                result["errors"].append(f"snapshots: {type(exc).__name__}")
            else:
                try:
                    with os.scandir(snap_src) as it:
                        for item in it:
                            try:
                                if not item.is_file() or not is_snapshot_filename(
                                    item.name
                                ):
                                    continue
                            except OSError:
                                result["failed"] += 1
                                continue
                            import_file(
                                item.path,
                                os.path.join(snap_dst, item.name),
                                f"snapshots/{item.name}",
                                _same_file_size,
                            )
                except OSError as exc:
                    result["failed"] += 1
                    result["errors"].append(f"snapshots: {type(exc).__name__}")
        if result["copied"] or result["overwritten"]:
            try:
                _apply_loaded(_recover_settings_document(settings_path()))
            except OSError as exc:
                applog.exception("read settings after legacy import failed", exc)
        return result


def _ensure_dir(path: str) -> None:
    """创建目录。"""
    os.makedirs(path, exist_ok=True)


def _app_base_dir() -> str:
    """返回应用数据根目录（必要时创建）。默认快照目录就在这。

    目录已经按当前运行账户验证过；卸载 wipe 后本会话内不再创建。
    """
    path = _app_base_dir_path()
    if not _data_wiped:
        _ensure_dir(path)
    return path


def builtin_snapshot_dir() -> str:
    """内置快照目录（应用数据根下的 ``snapshots``，必要时创建）。"""
    path = os.path.join(_app_base_dir(), "snapshots")
    if not _data_wiped:
        _ensure_dir(path)
    return path


def default_snapshot_dir() -> str:
    """当前生效的快照存放目录（自定义或内置；必要时创建）。"""
    return get_snapshot_dir()


def settings_path() -> str:
    """``settings.yaml`` 绝对路径（位于应用数据根）。

    只解析路径，不创建目录；读取设置本身不产生目录副作用。
    """
    return os.path.join(_app_base_dir_path(), _SETTINGS_FILE)


def _recover_settings_document(path: str) -> dict:
    """读取设置文件；读取失败时先备份损坏副本，再返回空配置。"""
    global _settings_recovered_path, _settings_recovered_reason
    global _settings_recovered_backup
    data, reason = _read_settings_yaml(path)
    if not reason:
        return data
    if (
        _settings_recovered_path == path
        and _settings_recovered_reason
        and _settings_recovered_backup
        and os.path.isfile(_settings_recovered_backup)
    ):
        # 本次会话已经备份过同一份损坏文件，不再反复生成副本。
        return {}
    backup = backup_corrupt_file(path)
    _settings_recovered_path = path
    _settings_recovered_reason = reason
    _settings_recovered_backup = backup
    applog.warn(
        f"settings file unreadable | reason={reason} "
        f"backup={backup or '(none)'}"
    )
    return {}


def load_settings_document() -> dict:
    """读取当前 ``settings.yaml``，读取失败时使用默认设置并保留损坏副本。"""
    return _recover_settings_document(settings_path())


def settings_recovered() -> bool:
    """当前设置文件本次会话是否读取失败过；供设置页提示。"""
    if not _settings_recovered_reason:
        return False
    try:
        return _settings_recovered_path == settings_path()
    except OSError:
        return False


def _ensure_corrupt_settings_backup() -> None:
    """写入设置前确认损坏副本存在；做不出副本时拒绝覆盖唯一原件。"""
    global _settings_recovered_backup
    if not _settings_recovered_reason:
        return
    path = settings_path()
    if _settings_recovered_path != path:
        return
    if _settings_recovered_backup and os.path.isfile(_settings_recovered_backup):
        return
    if not os.path.isfile(path):
        return
    backup = backup_corrupt_file(path)
    if backup:
        _settings_recovered_backup = backup
        return
    raise OSError("corrupt settings backup unavailable")


def _delete_settings_yaml() -> None:
    # 删除前也要保证损坏副本存在：恢复默认不能把唯一原件直接丢掉。
    _ensure_corrupt_settings_backup()
    path = settings_path()
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


# 当前会话值。import 时先落默认，再若默认路径存在 settings.yaml 则按键覆盖。
_scan_workers = default_scan_workers()
_compress_snapshots = True
_use_mft = True  # 默认开：盘符根 + NTFS 优先 MFT，失败回退 scandir
_search_memory_index = True  # 默认开：打开搜索时预热内存索引
_temp_cleanup = True  # 默认开：退出删除+启动清扫解压临时文件
_auto_check_updates = True  # 默认开：启动后台检查更新，有新版在标题栏提示
_remember_window_size = True  # 默认开：记住窗口大小
_window_width = 0
_window_height = 0
_log_sanitize = True  # 默认开：写入应用日志时脱敏绝对路径
# None = YAML 未写明该键（可用环境变量兜底）；True/False = 用户/文件显式设定
_log_sanitize_explicit: bool | None = None
# 日志最低等级：DEBUG|INFO|WARN|ERROR；默认 INFO
_log_level = "INFO"
# None = YAML 未写明（可用 WSMC_LOG_LEVEL / WSMC_DEBUG 兜底）
_log_level_explicit: bool | None = None
_lang = "en"  # 启动时 app 会按系统语言再设；YAML 优先覆盖
_theme = "light"
# 自定义快照目录；空串 = 使用 builtin_snapshot_dir()
_snapshot_dir = ""
# UI 设置（settings.yaml 的 ui: 节，设置页「界面」页签）
# 对比树父级行方向色背景；默认关
_ui_parent_row_bg = False
# 对比树父级行淡化；默认关
_ui_parent_row_dim = False
# 对比树悬停分组框（描边当前与同级目录）；默认关
_ui_hover_group_outline = False
# 悬停分组框：块内行数（自己+兄弟）超过该值才显示；0-50，默认 10
_ui_hover_group_min = 10
# 对比树层级引导线：子级块左侧竖线；默认开
_ui_tree_guide = True
# 展开父级下方间隔线：父行底边画分隔线；默认关
_ui_parent_sep = False
# 对比树多选：把选中项加入待删除后自动退出多选；默认开
_ui_multiselect_exit_after_add = True
# 对比树多选：连按两下 Esc 退出多选；默认开
_ui_multiselect_double_esc = True
# 提示停留时间（秒）：普通提示起始 2 秒、上限 20 秒；警告提示起始 5 秒、上限 30 秒
_ui_toast_info_base = 2
_ui_toast_info_max = 20
_ui_toast_warn_base = 5
_ui_toast_warn_max = 30
# 右侧工具侧栏是否展开；默认收起
_ui_tool_panel_open = False
# None = YAML 未写明该键（老配置，可用 localStorage 上报值迁移）；True/False = 已存过
_ui_tool_panel_open_explicit: bool | None = None
# 右侧工具侧栏宽度（像素，240-640）
_ui_tool_panel_width = wingeom.PANEL_W_DEFAULT
# AI 设置（与通用设置同文件 settings.yaml 的 ai: 节）
_ai_enabled = False
_ai_base_url = str(_AI_DEFAULTS["base_url"])
_ai_model = ""
_ai_api_key = ""
_ai_extra_prompt = ""
_ai_consented = False
_ai_model_options: list[str] = []
_ai_cleanup_max_depth = int(_AI_DEFAULTS["cleanup_max_depth"])
_ai_enabled_tools: list[str] = list(_default_enabled_tools())
# 删除黑名单：[{path, mode}]，mode=exact|prefix|regex
_delete_blacklist: list[dict[str, str]] = []
# 置顶快照的路径列表（按置顶顺序；只在所属文件夹内排到最前）
_pinned_snapshots: list[str] = []


def _ai_payload() -> dict:
    """当前 AI 设置（写 YAML / 模块共用）。"""
    base = (_ai_base_url or "").strip() or _AI_DEFAULTS["base_url"]
    return {
        "enabled": bool(_ai_enabled),
        "base_url": base,
        "model": (_ai_model or "").strip(),
        "api_key": _ai_api_key or "",
        "extra_prompt": _ai_extra_prompt or "",
        "consented": bool(_ai_consented),
        "model_options": list(_ai_model_options or []),
        "cleanup_max_depth": int(_ai_cleanup_max_depth),
        "enabled_tools": list(_ai_enabled_tools or []),
    }


def _apply_ai_loaded(raw: dict | None) -> None:
    """用 YAML 里的 ai 节覆盖内存 AI 设置（类型转换在此完成）。"""
    global _ai_enabled, _ai_base_url, _ai_model, _ai_api_key
    global _ai_extra_prompt, _ai_consented, _ai_model_options
    global _ai_cleanup_max_depth, _ai_enabled_tools
    if not isinstance(raw, dict) or not raw:
        return
    if "enabled" in raw:
        b = _as_bool(raw.get("enabled"))
        if b is not None:
            _ai_enabled = b
    if "base_url" in raw:
        base = _as_str(raw.get("base_url")).strip()
        _ai_base_url = base or _AI_DEFAULTS["base_url"]
    if "model" in raw:
        _ai_model = _as_str(raw.get("model")).strip()
    if "api_key" in raw:
        _ai_api_key = _as_str(raw.get("api_key"))
    if "extra_prompt" in raw:
        _ai_extra_prompt = _as_str(raw.get("extra_prompt"))
    if "consented" in raw:
        b = _as_bool(raw.get("consented"))
        if b is not None:
            _ai_consented = b
    if "model_options" in raw:
        _ai_model_options = _normalize_model_options(raw.get("model_options"))
    if "cleanup_max_depth" in raw:
        _ai_cleanup_max_depth = _normalize_cleanup_max_depth(
            raw.get("cleanup_max_depth")
        )
    # enabled_tools 优先；旧 tools_enabled=false 映射为空列表
    if "enabled_tools" in raw:
        _ai_enabled_tools = _normalize_enabled_tools(raw.get("enabled_tools"))
    elif "tools_enabled" in raw:
        legacy = _as_bool(raw.get("tools_enabled"))
        if legacy is False:
            _ai_enabled_tools = []
        elif legacy is True:
            _ai_enabled_tools = list(_default_enabled_tools())


def _apply_loaded(data: dict) -> None:
    """用 YAML 字典覆盖内存设置。

    - 文件不存在 / 空 dict：全部保持调用前的默认值。
    - 通用字段读 ``common`` 节；旧扁平顶层键同样生效。
    - AI 字段读 ``ai`` 节。
    - 只消费程序认识的键；未知键/节保留在解析结果里但不使用。
    - 旧文件中的 ``persist`` 键忽略（已不再使用手动持久化开关）。
    """
    global _scan_workers, _compress_snapshots, _use_mft, _search_memory_index
    global _remember_window_size, _window_width, _window_height
    global _log_sanitize, _log_sanitize_explicit
    global _log_level, _log_level_explicit
    global _lang, _theme, _snapshot_dir, _delete_blacklist, _pinned_snapshots
    global _ui_parent_row_bg, _ui_parent_row_dim
    global _ui_hover_group_outline, _ui_hover_group_min, _ui_tree_guide, _ui_parent_sep
    global _ui_tool_panel_open, _ui_tool_panel_open_explicit, _ui_tool_panel_width
    global _ui_multiselect_exit_after_add, _ui_multiselect_double_esc
    global _ui_toast_info_base, _ui_toast_info_max, _ui_toast_warn_base, _ui_toast_warn_max
    global _temp_cleanup, _auto_check_updates
    if not data:
        return
    common = _common_view(data)
    ui = _ui_view(data)
    if "scan_workers" in common:
        n = _as_int(common.get("scan_workers"))
        if n is not None:
            _scan_workers = _clamp_workers(n)
    if "compress_snapshots" in common:
        b = _as_bool(common.get("compress_snapshots"))
        if b is not None:
            _compress_snapshots = b
    if "use_mft" in common:
        b = _as_bool(common.get("use_mft"))
        if b is not None:
            _use_mft = b
    if "search_memory_index" in common:
        b = _as_bool(common.get("search_memory_index"))
        if b is not None:
            _search_memory_index = b
    if "temp_cleanup" in common:
        b = _as_bool(common.get("temp_cleanup"))
        if b is not None:
            _temp_cleanup = b
    if "auto_check_updates" in common:
        b = _as_bool(common.get("auto_check_updates"))
        if b is not None:
            _auto_check_updates = b
    if "remember_window_size" in common:
        b = _as_bool(common.get("remember_window_size"))
        if b is not None:
            _remember_window_size = b
    if "window_width" in common or "window_height" in common:
        w = _as_int(common.get("window_width"), 0) or 0
        h = _as_int(common.get("window_height"), 0) or 0
        if w >= 820 and h >= 560:
            _window_width, _window_height = _normalize_window_size(w, h)
    if "log_sanitize" in common:
        b = _as_bool(common.get("log_sanitize"))
        if b is not None:
            _log_sanitize = b
            _log_sanitize_explicit = b
    if "log_level" in common:
        _log_level = _normalize_log_level(common.get("log_level"))
        _log_level_explicit = True
    if "lang" in common:
        _lang = _normalize_lang(_as_str(common.get("lang"), "en"))
    if "theme" in common:
        _theme = _normalize_theme(_as_str(common.get("theme"), "light"))
    if "snapshot_dir" in common:
        _snapshot_dir = _normalize_snapshot_dir(_as_str(common.get("snapshot_dir"), ""))
    if "ui_parent_row_bg" in ui:
        b = _as_bool(ui.get("ui_parent_row_bg"))
        if b is not None:
            _ui_parent_row_bg = b
    if "ui_parent_row_dim" in ui:
        b = _as_bool(ui.get("ui_parent_row_dim"))
        if b is not None:
            _ui_parent_row_dim = b
    if "ui_hover_group_outline" in ui:
        b = _as_bool(ui.get("ui_hover_group_outline"))
        if b is not None:
            _ui_hover_group_outline = b
    if "ui_hover_group_min" in ui:
        _ui_hover_group_min = _normalize_ui_hover_group_min(ui.get("ui_hover_group_min"))
    if "ui_tree_guide" in ui:
        b = _as_bool(ui.get("ui_tree_guide"))
        if b is not None:
            _ui_tree_guide = b
    if "ui_parent_sep" in ui:
        b = _as_bool(ui.get("ui_parent_sep"))
        if b is not None:
            _ui_parent_sep = b
    if "ui_multiselect_exit_after_add" in ui:
        b = _as_bool(ui.get("ui_multiselect_exit_after_add"))
        if b is not None:
            _ui_multiselect_exit_after_add = b
    if "ui_multiselect_double_esc" in ui:
        b = _as_bool(ui.get("ui_multiselect_double_esc"))
        if b is not None:
            _ui_multiselect_double_esc = b
    if "ui_toast_info_base" in ui:
        _ui_toast_info_base = _normalize_ui_toast_seconds(
            ui.get("ui_toast_info_base"), 2, 1, 20
        )
    if "ui_toast_info_max" in ui:
        _ui_toast_info_max = _normalize_ui_toast_seconds(
            ui.get("ui_toast_info_max"), 20, 1, 60
        )
    if "ui_toast_warn_base" in ui:
        _ui_toast_warn_base = _normalize_ui_toast_seconds(
            ui.get("ui_toast_warn_base"), 5, 1, 30
        )
    if "ui_toast_warn_max" in ui:
        _ui_toast_warn_max = _normalize_ui_toast_seconds(
            ui.get("ui_toast_warn_max"), 30, 1, 120
        )
    if _ui_toast_info_max < _ui_toast_info_base:
        _ui_toast_info_max = _ui_toast_info_base
    if _ui_toast_warn_max < _ui_toast_warn_base:
        _ui_toast_warn_max = _ui_toast_warn_base
    if "ui_tool_panel_open" in ui:
        b = _as_bool(ui.get("ui_tool_panel_open"))
        if b is not None:
            _ui_tool_panel_open = b
            _ui_tool_panel_open_explicit = b
    if "ui_tool_panel_width" in ui:
        _ui_tool_panel_width = _normalize_ui_tool_panel_width(
            ui.get("ui_tool_panel_width")
        )
    if "delete_blacklist" in common:
        # _put_raw_pair 已去引号，此处多为 JSON 字符串
        _delete_blacklist = _normalize_delete_blacklist(common.get("delete_blacklist"))
    if "pinned_snapshots" in common:
        _pinned_snapshots = _normalize_pinned_snapshots(common.get("pinned_snapshots"))
    if isinstance(data.get("ai"), dict):
        _apply_ai_loaded(data.get("ai"))

def _settings_payload() -> dict:
    """当前内存设置（写 YAML / API 共用）。"""
    return {
        "scan_workers": _scan_workers,
        "compress_snapshots": _compress_snapshots,
        "use_mft": _use_mft,
        "search_memory_index": _search_memory_index,
        "temp_cleanup": _temp_cleanup,
        "auto_check_updates": bool(_auto_check_updates),
        "remember_window_size": bool(_remember_window_size),
        "window_width": int(_window_width),
        "window_height": int(_window_height),
        "log_sanitize": _log_sanitize,
        "log_level": _normalize_log_level(_log_level),
        "lang": _lang,
        "theme": _theme,
        "snapshot_dir": _snapshot_dir,
        "delete_blacklist": list(_delete_blacklist or []),
        "pinned_snapshots": list(_pinned_snapshots or []),
        "ui_parent_row_bg": _ui_parent_row_bg,
        "ui_parent_row_dim": _ui_parent_row_dim,
        "ui_hover_group_outline": _ui_hover_group_outline,
        "ui_hover_group_min": _ui_hover_group_min,
        "ui_tree_guide": _ui_tree_guide,
        "ui_parent_sep": _ui_parent_sep,
        "ui_multiselect_exit_after_add": _ui_multiselect_exit_after_add,
        "ui_multiselect_double_esc": _ui_multiselect_double_esc,
        "ui_toast_info_base": _ui_toast_info_base,
        "ui_toast_info_max": _ui_toast_info_max,
        "ui_toast_warn_base": _ui_toast_warn_base,
        "ui_toast_warn_max": _ui_toast_warn_max,
        "ui_tool_panel_open": _ui_tool_panel_open,
        "ui_tool_panel_width": _ui_tool_panel_width,
        "ai": _ai_payload(),
    }


def _persist() -> None:
    """把当前内存全部设置写回 YAML（改设置后自动调用）。

    若本次会话曾从损坏的 settings.yaml 恢复，写之前先确认损坏副本仍在；
    做不出副本时抛出 OSError，宁可写入失败也不覆盖唯一原件。
    """
    _ensure_corrupt_settings_backup()
    _write_settings_yaml(settings_path(), _settings_payload())


def reload_settings_from_disk() -> None:
    """从默认路径的 ``settings.yaml`` 重新加载。

    无文件则不改动内存；有文件则按键覆盖，缺项保留当前/默认值。
    读取失败时先备份损坏副本，再按空配置继续。
    """
    data = _recover_settings_document(settings_path())
    _apply_loaded(data)


# 模块加载时不读设置、不创建目录。启动入口先确定当前运行账户的数据目录并
# 验证访问，再由 initialize_storage 读取设置，避免把数据写进未经验证的位置。


def get_scan_workers() -> int:
    """返回当前生效的扫描线程数（默认 :func:`default_scan_workers`）。"""
    return _scan_workers


def set_scan_workers(n: int) -> int:
    """设置扫描线程数（越界收拢），返回生效值；值变化时写 YAML。"""
    global _scan_workers
    new = _clamp_workers(n)
    if new != _scan_workers:
        _scan_workers = new
        _persist()
    return _scan_workers


def get_compress_snapshots() -> bool:
    """返回是否在扫描完成后压缩快照（默认 True）。"""
    return _compress_snapshots


def set_compress_snapshots(enabled: bool) -> bool:
    """设置是否压缩快照；值变化时写 YAML。"""
    global _compress_snapshots
    new = bool(enabled)
    if new != _compress_snapshots:
        _compress_snapshots = new
        _persist()
    return _compress_snapshots


def get_use_mft() -> bool:
    """是否对盘符根 NTFS 尝试 MFT 快路径（默认 True）。"""
    return _use_mft


def set_use_mft(enabled: bool) -> bool:
    """设置是否尝试 MFT；值变化时写 YAML。"""
    global _use_mft
    new = bool(enabled)
    if new != _use_mft:
        _use_mft = new
        _persist()
    return _use_mft


def get_search_memory_index() -> bool:
    """是否在打开搜索时预热内存索引（默认 True）。"""
    return _search_memory_index


def set_search_memory_index(enabled: bool) -> bool:
    """设置是否使用搜索内存索引；值变化时写 YAML。"""
    global _search_memory_index
    new = bool(enabled)
    if new != _search_memory_index:
        _search_memory_index = new
        _persist()
    return _search_memory_index


def get_temp_cleanup() -> bool:
    """是否自动清理对比解压产生的临时文件（默认 True）。"""
    return _temp_cleanup


def set_temp_cleanup(enabled: bool) -> bool:
    """设置是否自动清理临时解压文件；值变化时写 YAML。"""
    global _temp_cleanup
    new = bool(enabled)
    if new != _temp_cleanup:
        _temp_cleanup = new
        _persist()
    return _temp_cleanup


def get_auto_check_updates() -> bool:
    """是否在启动时后台检查更新（默认 True）。"""
    return bool(_auto_check_updates)


def set_auto_check_updates(enabled: bool) -> bool:
    """设置是否自动检查更新；值变化时写 YAML。"""
    global _auto_check_updates
    new = bool(enabled)
    if new != _auto_check_updates:
        _auto_check_updates = new
        _persist()
    return _auto_check_updates


def get_remember_window_size() -> bool:
    """是否记住窗口大小（写入 settings.yaml）。"""
    return bool(_remember_window_size)


def set_remember_window_size(enabled: bool) -> bool:
    """设置是否记住窗口大小；值变化时写 YAML。"""
    global _remember_window_size
    val = bool(enabled)
    if val == bool(_remember_window_size):
        return val
    _remember_window_size = val
    _persist()
    return val


def get_window_size() -> tuple[int, int]:
    """当前记住的窗口宽高（逻辑像素）；未记住过时可能为 (0, 0)。"""
    return int(_window_width), int(_window_height)


def has_saved_window_size() -> bool:
    """是否已有可用的记住尺寸（关闭「记住」后启动不用它）。"""
    w, h = int(_window_width), int(_window_height)
    return w >= 820 and h >= 560


def set_window_size(width: int, height: int, *, persist: bool = True) -> tuple[int, int]:
    """更新记住的窗口尺寸；``persist=True`` 且开启记住时写 YAML。

    防抖由调用方负责（尺寸稳定后再调本函数）。
    """
    global _window_width, _window_height
    w, h = _normalize_window_size(width, height)
    changed = w != int(_window_width) or h != int(_window_height)
    _window_width, _window_height = w, h
    if persist and _remember_window_size and changed:
        _persist()
    elif persist and _remember_window_size and not changed:
        # 尺寸相同不写盘
        pass
    return w, h


def get_tool_panel_open() -> bool:
    """右侧工具侧栏是否展开。"""
    return bool(_ui_tool_panel_open)


def set_tool_panel_open(enabled: bool) -> bool:
    """设置侧栏展开状态；值变化时写 YAML。"""
    global _ui_tool_panel_open, _ui_tool_panel_open_explicit
    val = bool(enabled)
    # explicit 为 None 时也要写一次，把老配置升级成显式记录
    if _ui_tool_panel_open_explicit is None or val != bool(_ui_tool_panel_open):
        _ui_tool_panel_open = val
        _ui_tool_panel_open_explicit = val
        _persist()
    return val


def get_tool_panel_width() -> int:
    """右侧工具侧栏宽度（像素，240-640）。"""
    return int(_ui_tool_panel_width)


def set_tool_panel_width(px: int) -> int:
    """设置侧栏宽度；夹到 240-640，值变化时写 YAML。"""
    global _ui_tool_panel_width
    val = _normalize_ui_tool_panel_width(px)
    if val != int(_ui_tool_panel_width):
        _ui_tool_panel_width = val
        _persist()
    return val


def has_explicit_tool_panel_open() -> bool:
    """YAML 里是否写过侧栏展开状态；为假说明是升级前的老配置。"""
    return _ui_tool_panel_open_explicit is not None


def migrate_tool_panel_state(open: bool, width: int) -> None:
    """把老版本 localStorage 里的侧栏状态搬进 YAML，只做一次。

    仅当 ``_ui_tool_panel_open_explicit`` 为 None（YAML 没写过该键）时执行。
    同时把 ``window_width`` 从老语义（当前实际宽度）折算成新语义
    （侧栏展开时的宽度），避免老用户升级后窗口宽度跳变。
    """
    global _ui_tool_panel_open, _ui_tool_panel_open_explicit, _ui_tool_panel_width
    global _window_width, _window_height
    if _ui_tool_panel_open_explicit is not None:
        return
    _ui_tool_panel_open = bool(open)
    _ui_tool_panel_open_explicit = _ui_tool_panel_open
    _ui_tool_panel_width = _normalize_ui_tool_panel_width(width)
    if has_saved_window_size():
        _window_width, _window_height = _normalize_window_size(
            wingeom.to_stored_width(
                _window_width,
                panel_open=_ui_tool_panel_open,
                panel_width=_ui_tool_panel_width,
            ),
            _window_height,
        )
    _persist()


def get_delete_blacklist() -> list[dict[str, str]]:
    """删除黑名单副本：``[{path, mode}, ...]``。"""
    return [dict(x) for x in (_delete_blacklist or [])]


def set_delete_blacklist(entries) -> list[dict[str, str]]:
    """规范化并写入删除黑名单；值变化时落盘。"""
    global _delete_blacklist
    new = _normalize_delete_blacklist(entries)
    if new != list(_delete_blacklist or []):
        _delete_blacklist = new
        _persist()
    return get_delete_blacklist()


def get_pinned_snapshots() -> list[str]:
    """置顶快照路径副本，按置顶先后的顺序。"""
    return list(_pinned_snapshots or [])


def set_pinned_snapshots(paths) -> list[str]:
    """整体替换置顶列表；值变化时落盘。"""
    global _pinned_snapshots
    new = _normalize_pinned_snapshots(paths)
    if new != list(_pinned_snapshots or []):
        _pinned_snapshots = new
        _persist()
    return get_pinned_snapshots()


def get_log_sanitize() -> bool:
    """写入应用日志时是否脱敏绝对路径（默认 True）。"""
    return bool(_log_sanitize)


def is_log_sanitize_explicit() -> bool:
    """``settings.yaml`` / 设置页是否显式写过 ``log_sanitize``。

    False 时启动可用环境变量 ``WSMC_LOG_SANITIZE`` 兜底。
    """
    return _log_sanitize_explicit is not None


def set_log_sanitize(enabled: bool) -> bool:
    """设置日志路径脱敏；值变化时写 YAML（此后视为显式设定）。"""
    global _log_sanitize, _log_sanitize_explicit
    new = bool(enabled)
    if new != _log_sanitize or _log_sanitize_explicit is None:
        _log_sanitize = new
        _log_sanitize_explicit = new
        _persist()
    return _log_sanitize


def get_log_level() -> str:
    """应用日志最低等级（DEBUG|INFO|WARN|ERROR），默认 INFO。"""
    return _normalize_log_level(_log_level)


def is_log_level_explicit() -> bool:
    """``settings.yaml`` / 设置页是否显式写过 ``log_level``。

    False 时启动可用 ``WSMC_LOG_LEVEL`` / ``WSMC_DEBUG`` 兜底。
    """
    return _log_level_explicit is not None


def set_log_level(level: str) -> str:
    """设置日志最低等级；值变化或首次显式设定时写 YAML。"""
    global _log_level, _log_level_explicit
    new = _normalize_log_level(level)
    if new != _normalize_log_level(_log_level) or _log_level_explicit is None:
        _log_level = new
        _log_level_explicit = True
        _persist()
    return _log_level


def get_lang() -> str:
    """界面语言（``zh`` / ``en``），供 YAML 与启动恢复。"""
    return _lang


def set_lang(lang: str) -> str:
    """设置界面语言；值变化时写 YAML。"""
    global _lang
    new = _normalize_lang(lang)
    if new != _lang:
        _lang = new
        _persist()
    return _lang


def get_theme() -> str:
    """界面主题（``dark`` / ``light``）。"""
    return _theme


def set_theme(theme: str) -> str:
    """设置主题；值变化时写 YAML。"""
    global _theme
    new = _normalize_theme(theme)
    if new != _theme:
        _theme = new
        _persist()
    return _theme


def get_snapshot_dir() -> str:
    """当前生效的快照目录（自定义或内置；必要时创建并验证可写）。

    自定义目录不可用时不再回退内置目录：回退会让界面显示自定义路径、
    快照却写进内置目录，用户无法发现两边不一致。此时抛出
    :class:`SnapshotDirUnavailable`，由调用方给出可读错误。
    """
    if _snapshot_dir:
        reason = _probe_writable_dir(_snapshot_dir)
        if reason:
            raise SnapshotDirUnavailable(_snapshot_dir, reason)
        return _snapshot_dir
    path = builtin_snapshot_dir()
    reason = _probe_writable_dir(path)
    if reason:
        raise SnapshotDirUnavailable(path, reason)
    return path


def snapshot_dir_writable(path: str) -> str:
    """按当前运行账户验证指定目录可写；返回空串或失败原因。"""
    return _probe_writable_dir(path)


def get_snapshot_dir_configured() -> str:
    """用户配置的自定义路径；空串表示使用内置目录。"""
    return _snapshot_dir


def set_snapshot_dir(path: str | None) -> str:
    """设置快照存放目录。

    空 / None 恢复内置目录；非空路径会 abspath 并验证当前账户可写。
    验证失败或写入设置失败时抛出 OSError，内存中的目录回到原来值，
    界面不会显示成已生效。
    """
    global _snapshot_dir
    old = _snapshot_dir
    raw = _normalize_snapshot_dir(path)
    if not raw:
        builtin = builtin_snapshot_dir()
        reason = _probe_writable_dir(builtin)
        if reason:
            raise SnapshotDirUnavailable(builtin, reason)
        if old:
            _snapshot_dir = ""
            try:
                _persist()
            except OSError:
                _snapshot_dir = old
                raise
        return builtin
    reason = _probe_writable_dir(raw)
    if reason:
        raise SnapshotDirUnavailable(raw, reason)
    if raw != old:
        _snapshot_dir = raw
        try:
            _persist()
        except OSError:
            _snapshot_dir = old
            raise
    return _snapshot_dir


_APPLY_SETTINGS_STATE_KEYS = (
    "_scan_workers",
    "_compress_snapshots",
    "_use_mft",
    "_search_memory_index",
    "_temp_cleanup",
    "_auto_check_updates",
    "_remember_window_size",
    "_window_width",
    "_window_height",
    "_log_sanitize",
    "_log_sanitize_explicit",
    "_log_level",
    "_log_level_explicit",
    "_snapshot_dir",
    "_delete_blacklist",
    "_pinned_snapshots",
    "_ui_parent_row_bg",
    "_ui_parent_row_dim",
    "_ui_hover_group_outline",
    "_ui_hover_group_min",
    "_ui_tree_guide",
    "_ui_parent_sep",
    "_ui_multiselect_exit_after_add",
    "_ui_multiselect_double_esc",
    "_ui_toast_info_base",
    "_ui_toast_info_max",
    "_ui_toast_warn_base",
    "_ui_toast_warn_max",
    "_ui_tool_panel_open",
    "_ui_tool_panel_open_explicit",
    "_ui_tool_panel_width",
)


def _capture_apply_settings_state() -> dict:
    """保存批量设置会改动的内存值，供写入失败时整体回滚。"""
    g = globals()
    return {name: g[name] for name in _APPLY_SETTINGS_STATE_KEYS}


def _restore_apply_settings_state(state: dict) -> None:
    """恢复批量设置前的内存值。"""
    globals().update(state)


def apply_settings(
    payload: dict,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """一次性应用多条设置（供设置页点「完成」时统一提交）。

    先更新内存中的各项，再写 ``settings.yaml``（设置页提交即自动持久化）。
    忽略旧版 ``persist_settings`` 键。

    若 ``snapshot_dir`` 变更，会把**原目录**中的 ``.db``/``.dbz`` 迁到新目录。
    ``progress`` 会在迁移过程中被调用（见 :func:`migrate_snapshots`）。

    可识别键：``scan_workers``、``compress_snapshots``、``use_mft``、
    ``search_memory_index``、``temp_cleanup``、``remember_window_size``、
    ``window_width``、``window_height``、``log_sanitize``、``log_level``、
    ``snapshot_dir``（空串=内置目录）、``delete_blacklist``、
    ``pinned_snapshots``（置顶快照路径列表）、
    ``ui_parent_row_bg``、``ui_parent_row_dim``、``ui_hover_group_outline``、
    ``ui_hover_group_min``、``ui_tree_guide``、``ui_parent_sep``、
    ``ui_multiselect_exit_after_add``、``ui_multiselect_double_esc``、
    ``ui_toast_info_base``、``ui_toast_info_max``、``ui_toast_warn_base``、
    ``ui_toast_warn_max``、``ui_tool_panel_open``、``ui_tool_panel_width``。
    缺省键保持当前值。
    """
    global _scan_workers, _compress_snapshots, _use_mft, _search_memory_index
    global _remember_window_size, _window_width, _window_height
    global _log_sanitize, _log_sanitize_explicit
    global _log_level, _log_level_explicit
    global _snapshot_dir, _delete_blacklist
    global _pinned_snapshots
    global _ui_parent_row_bg, _ui_parent_row_dim
    global _ui_hover_group_outline, _ui_hover_group_min, _ui_tree_guide, _ui_parent_sep
    global _ui_tool_panel_open, _ui_tool_panel_open_explicit, _ui_tool_panel_width
    global _ui_multiselect_exit_after_add, _ui_multiselect_double_esc
    global _ui_toast_info_base, _ui_toast_info_max, _ui_toast_warn_base, _ui_toast_warn_max
    global _temp_cleanup, _auto_check_updates

    if not isinstance(payload, dict):
        payload = {}

    saved_state = _capture_apply_settings_state()

    # 先把新快照目录验证完再改内存：不可写时直接拒绝，不搬任何文件。
    prepared_snapshot_dir: str | None = None
    if "snapshot_dir" in payload:
        prepared_snapshot_dir = _normalize_snapshot_dir(payload.get("snapshot_dir"))
        target = prepared_snapshot_dir or builtin_snapshot_dir()
        reason = _probe_writable_dir(target)
        if reason:
            raise SnapshotDirUnavailable(target, reason)

    old_configured_snapshot_dir = _snapshot_dir
    try:
        old_snap_dir = get_snapshot_dir()
    except OSError:
        # 原快照目录不可用不妨碍保存一个已验证可写的新目录；
        # 没有可迁移的来源时按空目录处理。
        old_snap_dir = ""
    dir_changed = False

    if "scan_workers" in payload:
        _scan_workers = _clamp_workers(payload["scan_workers"])
    if "compress_snapshots" in payload:
        _compress_snapshots = bool(payload["compress_snapshots"])
    if "use_mft" in payload:
        _use_mft = bool(payload["use_mft"])
    if "search_memory_index" in payload:
        _search_memory_index = bool(payload["search_memory_index"])
    if "temp_cleanup" in payload:
        _temp_cleanup = bool(payload["temp_cleanup"])
    if "auto_check_updates" in payload:
        _auto_check_updates = bool(payload["auto_check_updates"])
    if "remember_window_size" in payload:
        _remember_window_size = bool(payload["remember_window_size"])
    if "window_width" in payload or "window_height" in payload:
        w = payload.get("window_width", _window_width)
        h = payload.get("window_height", _window_height)
        _window_width, _window_height = _normalize_window_size(w, h)
    if "log_sanitize" in payload:
        _log_sanitize = bool(payload["log_sanitize"])
        _log_sanitize_explicit = _log_sanitize
    if "log_level" in payload:
        _log_level = _normalize_log_level(payload.get("log_level"))
        _log_level_explicit = True
    if "delete_blacklist" in payload:
        _delete_blacklist = _normalize_delete_blacklist(payload.get("delete_blacklist"))
    if "pinned_snapshots" in payload:
        _pinned_snapshots = _normalize_pinned_snapshots(payload.get("pinned_snapshots"))
    if "ui_parent_row_bg" in payload:
        _ui_parent_row_bg = bool(payload["ui_parent_row_bg"])
    if "ui_parent_row_dim" in payload:
        _ui_parent_row_dim = bool(payload["ui_parent_row_dim"])
    if "ui_hover_group_outline" in payload:
        _ui_hover_group_outline = bool(payload["ui_hover_group_outline"])
    if "ui_hover_group_min" in payload:
        _ui_hover_group_min = _normalize_ui_hover_group_min(payload.get("ui_hover_group_min"))
    if "ui_tree_guide" in payload:
        _ui_tree_guide = bool(payload["ui_tree_guide"])
    if "ui_parent_sep" in payload:
        _ui_parent_sep = bool(payload["ui_parent_sep"])
    if "ui_multiselect_exit_after_add" in payload:
        _ui_multiselect_exit_after_add = bool(payload["ui_multiselect_exit_after_add"])
    if "ui_multiselect_double_esc" in payload:
        _ui_multiselect_double_esc = bool(payload["ui_multiselect_double_esc"])
    if "ui_toast_info_base" in payload:
        _ui_toast_info_base = _normalize_ui_toast_seconds(
            payload.get("ui_toast_info_base"), 2, 1, 20
        )
    if "ui_toast_info_max" in payload:
        _ui_toast_info_max = _normalize_ui_toast_seconds(
            payload.get("ui_toast_info_max"), 20, 1, 60
        )
    if "ui_toast_warn_base" in payload:
        _ui_toast_warn_base = _normalize_ui_toast_seconds(
            payload.get("ui_toast_warn_base"), 5, 1, 30
        )
    if "ui_toast_warn_max" in payload:
        _ui_toast_warn_max = _normalize_ui_toast_seconds(
            payload.get("ui_toast_warn_max"), 30, 1, 120
        )
    if _ui_toast_info_max < _ui_toast_info_base:
        _ui_toast_info_max = _ui_toast_info_base
    if _ui_toast_warn_max < _ui_toast_warn_base:
        _ui_toast_warn_max = _ui_toast_warn_base
    if "ui_tool_panel_open" in payload:
        _ui_tool_panel_open = bool(payload["ui_tool_panel_open"])
        _ui_tool_panel_open_explicit = _ui_tool_panel_open
    if "ui_tool_panel_width" in payload:
        _ui_tool_panel_width = _normalize_ui_tool_panel_width(
            payload.get("ui_tool_panel_width")
        )
    new_snap_dir = ""
    if prepared_snapshot_dir is not None:
        _snapshot_dir = prepared_snapshot_dir
        new_snap_dir = prepared_snapshot_dir or builtin_snapshot_dir()
        dir_changed = not _same_dir(old_snap_dir, new_snap_dir)

    migrate_info: dict = {
        "moved": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
        "total": 0,
    }
    if dir_changed:
        migrate_info = migrate_snapshots(old_snap_dir, new_snap_dir, progress=progress)

    try:
        _persist()
    except OSError as exc:
        # 设置没有写进去，所有已改的内存值都恢复原状，否则界面会显示成
        # 配置已生效，重启后又退回旧值。已经搬走的文件不回滚，由界面告知用户。
        _restore_apply_settings_state(saved_state)
        if dir_changed and int(migrate_info.get("moved") or 0) > 0:
            return {
                "error": "snapshot_dir_not_saved",
                "path": new_snap_dir,
                "reason": _storage_reason(exc),
                "migrate": migrate_info,
                "snapshot_dir_changed": False,
                "snapshot_dir_configured": old_configured_snapshot_dir,
                "snapshot_dir_is_custom": bool(old_configured_snapshot_dir),
            }
        return {
            "error": "settings_write_failed",
            "reason": _storage_reason(exc),
            "migrate": migrate_info,
            "snapshot_dir_changed": False,
        }
    except Exception:
        _restore_apply_settings_state(saved_state)
        raise
    out = settings_dict()
    out["snapshot_dir_changed"] = dir_changed
    out["migrate"] = migrate_info
    return out


def _clear_settings_recovery() -> None:
    """清空设置文件读取失败的会话状态（换数据文件夹时用）。"""
    global _settings_recovered_path, _settings_recovered_reason
    global _settings_recovered_backup
    _settings_recovered_path = ""
    _settings_recovered_reason = ""
    _settings_recovered_backup = ""


def _reset_settings_in_memory(*, lang: str | None = None) -> None:
    """把内存设置全部回到内置默认（**不**碰磁盘上的任何文件）。"""
    global _scan_workers, _compress_snapshots, _use_mft, _search_memory_index
    global _remember_window_size, _window_width, _window_height
    global _log_sanitize, _log_sanitize_explicit
    global _log_level, _log_level_explicit
    global _lang, _theme, _snapshot_dir, _delete_blacklist, _pinned_snapshots
    global _ai_enabled, _ai_base_url, _ai_model, _ai_api_key
    global _ai_extra_prompt, _ai_consented, _ai_model_options
    global _ai_cleanup_max_depth, _ai_enabled_tools
    global _ui_parent_row_bg, _ui_parent_row_dim
    global _ui_hover_group_outline, _ui_hover_group_min, _ui_tree_guide, _ui_parent_sep
    global _ui_multiselect_exit_after_add, _ui_multiselect_double_esc
    global _ui_tool_panel_open, _ui_tool_panel_open_explicit, _ui_tool_panel_width
    global _ui_toast_info_base, _ui_toast_info_max, _ui_toast_warn_base, _ui_toast_warn_max
    global _temp_cleanup, _auto_check_updates

    _scan_workers = default_scan_workers()
    _compress_snapshots = True
    _use_mft = True
    _search_memory_index = True
    _temp_cleanup = True
    _auto_check_updates = True
    _remember_window_size = True
    _window_width = 0
    _window_height = 0
    _log_sanitize = True
    _log_sanitize_explicit = None
    _log_level = "INFO"
    _log_level_explicit = None
    _theme = "light"
    _lang = _normalize_lang(lang) if lang is not None else "en"
    _snapshot_dir = ""
    _delete_blacklist = []
    _pinned_snapshots = []
    _ui_parent_row_bg = False
    _ui_parent_row_dim = False
    _ui_hover_group_outline = False
    _ui_hover_group_min = 10
    _ui_tree_guide = True
    _ui_parent_sep = False
    _ui_multiselect_exit_after_add = True
    _ui_multiselect_double_esc = True
    _ui_toast_info_base = 2
    _ui_toast_info_max = 20
    _ui_toast_warn_base = 5
    _ui_toast_warn_max = 30
    _ui_tool_panel_open = False
    _ui_tool_panel_open_explicit = None
    _ui_tool_panel_width = wingeom.PANEL_W_DEFAULT
    _ai_enabled = False
    _ai_base_url = str(_AI_DEFAULTS["base_url"])
    _ai_model = ""
    _ai_api_key = ""
    _ai_extra_prompt = ""
    _ai_consented = False
    _ai_model_options = []
    _ai_cleanup_max_depth = int(_AI_DEFAULTS["cleanup_max_depth"])
    _ai_enabled_tools = list(_default_enabled_tools())


def reset_settings_to_defaults(*, lang: str | None = None) -> dict:
    """恢复内置默认设置并删除 ``settings.yaml``。

    不删除快照文件，仅清空自定义快照目录配置（回到内置目录）。
    ``lang`` 由调用方传入冷启动默认语言（系统语言）；省略则 ``en``。
    """
    # 先确认损坏副本可做，再改内存与删文件，避免失败时界面看到默认值
    # 已经生效而原件仍处于不可读状态。
    _ensure_corrupt_settings_backup()

    _reset_settings_in_memory(lang=lang)
    _delete_settings_yaml()
    _clear_settings_recovery()
    return settings_dict()


def get_ai_settings() -> dict:
    """返回当前 AI 设置副本。"""
    return _ai_payload()


def set_ai_settings(payload: dict | None = None, **kwargs) -> dict:
    """合并写入 AI 设置并落盘；返回最新副本。

    - 缺省键保留原值
    - ``api_key`` 空串且未带 ``clear_key`` 时保留原 key
    - ``clear_key=True`` 且 key 为空时清空
    """
    global _ai_enabled, _ai_base_url, _ai_model, _ai_api_key
    global _ai_extra_prompt, _ai_consented, _ai_model_options
    global _ai_cleanup_max_depth, _ai_enabled_tools

    body = dict(payload or {})
    body.update(kwargs)

    if "enabled" in body:
        _ai_enabled = bool(body.get("enabled"))
    if "base_url" in body and body.get("base_url") is not None:
        base = str(body.get("base_url") or "").strip()
        _ai_base_url = base or _AI_DEFAULTS["base_url"]
    if "model" in body and body.get("model") is not None:
        _ai_model = str(body.get("model") or "").strip()
    if "extra_prompt" in body and body.get("extra_prompt") is not None:
        _ai_extra_prompt = str(body.get("extra_prompt") or "")
    if "consented" in body:
        _ai_consented = bool(body.get("consented"))
    if "model_options" in body and body.get("model_options") is not None:
        _ai_model_options = _normalize_model_options(body.get("model_options"))
    if "cleanup_max_depth" in body and body.get("cleanup_max_depth") is not None:
        _ai_cleanup_max_depth = _normalize_cleanup_max_depth(
            body.get("cleanup_max_depth"), default=_ai_cleanup_max_depth
        )
    if "enabled_tools" in body:
        _ai_enabled_tools = _normalize_enabled_tools(body.get("enabled_tools"))
    elif "tools_enabled" in body:
        # 兼容旧客户端布尔开关
        if bool(body.get("tools_enabled")):
            _ai_enabled_tools = list(_default_enabled_tools())
        else:
            _ai_enabled_tools = []

    if "api_key" in body:
        key = body.get("api_key")
        if key is None:
            pass
        else:
            key_s = str(key).strip()
            if key_s:
                _ai_api_key = key_s
            elif body.get("clear_key"):
                _ai_api_key = ""

    _persist()
    return _ai_payload()


def app_data_dir() -> str:
    """应用数据根（当前运行账户的本地数据目录下的应用文件夹，必要时创建）。"""
    return _app_base_dir()


def storage_state() -> dict:
    """当前数据目录状态；供启动恢复界面读取。"""
    state = initialize_storage()
    return dict(state)


def set_data_root_for_tests(path: str) -> None:
    """仅测试用：把数据目录固定到临时路径，跳过当前账户解析。"""
    global _app_data_root, _storage_initialized, _storage_ok, _storage_state
    _app_data_root = os.path.abspath(path)
    _storage_initialized = True
    _storage_ok = True
    _storage_state = {
        "ok": True,
        "app_data_dir": _app_data_root,
        "account": "",
    }
    _clear_settings_recovery()


def _resolve_data_root(target: str) -> str:
    """把用户选定的目录转成实际数据根：在其下统一保留一层应用子目录。

    数据文件会平铺在数据根里，直接使用用户选中的目录会让设置与快照和别的
    文件混在一起，清理时也容易误伤。统一往下建一层后，盘符根、系统目录与
    个人主目录都不需要单独拦截；用户重新选回已有数据根时不重复叠加。
    """
    base = os.path.abspath(os.path.expanduser(str(target or "")))
    if os.path.basename(base).lower() == _APP_DIR_NAME.lower():
        return base
    return os.path.join(base, _APP_DIR_NAME)



def _is_app_data_like_root(path: str) -> bool:
    """排除盘符根这类明显不是应用数据目录的迁移来源。"""
    _drive, tail = os.path.splitdrive(os.path.abspath(path))
    return bool(tail.strip("\\/"))



def switch_data_root(path: str) -> dict:
    """把数据目录换到用户主动选择的 ``path``。"""
    with _storage_lock:
        return _switch_data_root_locked(path)


def _switch_data_root_locked(path: str) -> dict:
    """在锁内把数据目录换到用户主动选择的位置。

    用户选的是一个存放位置，实际数据根是它下面的应用子目录。目标目录先
    验证当前运行账户可写，再把当前数据目录里的设置、链接登记、迁移历史
    与快照移动到新位置。用户主动选择会写入当前账户本地数据下的指针文件，
    下次启动继续使用；指针写不进去时只对本次运行有效，并在结果里说明。

    Raises:
        DataDirUnavailable: 路径为空，当前账户不能写入，或新位置在现
            有数据文件夹内部。
    """
    global _app_data_root, _storage_ok, _storage_state, _data_wiped
    global _snapshot_dir

    initialize_storage()
    account = str(_storage_state.get("account") or "")

    raw = str(path or "").strip()
    if not raw:
        raise DataDirUnavailable("", "missing_parent")
    target = _resolve_data_root(raw)
    previous_root = _app_data_root
    if (
        previous_root
        and not _same_dir(previous_root, target)
        and _path_within(target, previous_root)
    ):
        raise DataDirUnavailable(target, "inside_source")
    reason = _probe_writable_dir(target)
    if reason:
        raise DataDirUnavailable(target, reason)

    settings_file = os.path.join(target, _SETTINGS_FILE)
    settings_found = os.path.isfile(settings_file)
    target_had_config = settings_found or any(
        os.path.exists(os.path.join(target, name))
        for name in ("links.json", "migration_history.json", "snapshots")
    )

    source = previous_root
    if not source or _same_dir(source, target) or not _is_app_data_like_root(source):
        source = ""
    old_root = source

    migration: dict = {}
    if source:
        try:
            migration = store_paths.migrate_data_root(
                source,
                target,
                (_SETTINGS_FILE, "links.json", "migration_history.json"),
            )
        except Exception as exc:  # noqa: BLE001
            applog.exception("switch data folder move failed", exc)
            migration = {
                "moved": 0,
                "skipped": 0,
                "failed": 1,
                "errors": [type(exc).__name__],
            }
        # 数据文件已经移到新位置，旧的应用目录空了就一并删掉；
        # 里面若还有未移动的文件，删除失败，原目录保留。
        for empty in (os.path.join(source, "snapshots"), source):
            try:
                os.rmdir(empty)
            except OSError:
                pass

    _app_data_root = target
    _storage_ok = True
    _data_wiped = False
    _reset_settings_in_memory(lang=_lang)
    _clear_settings_recovery()
    from . import migrate_history, migrate_links

    migrate_links._clear_registry_recovery()  # noqa: SLF001 - 同一会话状态，需一并重置
    migrate_history._clear_history_recovery()  # noqa: SLF001
    _apply_loaded(_recover_settings_document(settings_file))
    # 目标位置已有设置时沿用目标设置；没有时用刚移入的设置。
    settings_found = settings_found or os.path.isfile(settings_file)
    snapshot_dir_reset = False
    old_builtin_snapshots = os.path.join(old_root, "snapshots") if old_root else ""
    if (
        _snapshot_dir
        and old_builtin_snapshots
        and _same_dir(_snapshot_dir, old_builtin_snapshots)
    ):
        # 内置快照目录已经随数据目录移到新位置，旧路径不再存在，改回新
        # 数据目录下的内置目录。旧根内其他自定义快照目录保留原配置。
        _snapshot_dir = ""
        snapshot_dir_reset = True
        try:
            _persist()
        except OSError as exc:
            applog.warn(
                "reset snapshot dir after data folder switch failed"
                f" | reason={type(exc).__name__}"
            )
    pointer_saved = _write_data_dir_pointer(target)
    try:
        snapshot_dir = builtin_snapshot_dir()
    except OSError:
        snapshot_dir = ""
    _storage_state = {
        "ok": True,
        "app_data_dir": target,
        "account": account,
        "pointer_invalid": False,
    }
    applog.info(
        "data folder switched"
        f" | target_had_config={target_had_config}"
        f" pointer_saved={pointer_saved}"
    )
    return {
        "app_data_dir": target,
        "settings_found": settings_found,
        "target_had_config": target_had_config,
        "snapshot_dir": snapshot_dir,
        "pointer_saved": pointer_saved,
        "snapshot_dir_reset": snapshot_dir_reset,
        "previous_root": (
            previous_root
            if previous_root and not _same_dir(previous_root, target)
            else ""
        ),
        "moved": int(migration.get("moved") or 0),
        "skipped": int(migration.get("skipped") or 0),
        "failed": int(migration.get("failed") or 0),
    }


def wipe_app_data(*, delete_data: bool = True) -> dict:
    """卸载清理：仅处理应用数据目录，不碰用户自定义快照路径。

    - ``delete_data=True``（默认）：删除整个应用数据文件夹
      （``WhoShitsOnMyC`` 目录本身及其内 snapshots、settings.yaml 等）。
      用 ``_app_base_dir_path()``，**不会**先 makedirs 再删。
    - ``delete_data=False``：仅删除 ``settings.yaml``（若存在）。

    **不会**删除用户自选的外部快照目录（即使其中有 .db/.dbz）。
    **不**删除程序本体。

    返回 ``{ok, deleted_data, path, removed, errors}``。
    """
    import shutil

    global _snapshot_dir, _data_wiped
    global _settings_recovered_path, _settings_recovered_reason
    global _settings_recovered_backup

    # 切勿调用 _app_base_dir()：它会 makedirs 把刚要删的目录又建出来
    base = _app_base_dir_path()
    result: dict = {
        "ok": True,
        "deleted_data": bool(delete_data),
        "path": base,
        "removed": [],
        "errors": [],
    }
    if not _is_app_data_like_root(base):
        # 清理目标必须是具体的应用目录：盘符根与共享根一旦被当作目标，
        # 整个卷或整个共享都会被删。走到这里说明记录或设置已经异常，直接拒绝。
        result["ok"] = False
        result["errors"].append("unsafe_root")
        applog.warn("wipe_app_data refused | reason=unsafe_root")
        return result
    if delete_data:
        # 整夹删掉 WhoShitsOnMyC（含子目录）；失败则逐项清再尝试 rmdir
        try:
            if os.path.isdir(base):
                try:
                    shutil.rmtree(base)
                    result["removed"].append(
                        os.path.basename(base.rstrip("\\/")) or base
                    )
                except OSError:
                    for name in os.listdir(base):
                        p = os.path.join(base, name)
                        try:
                            if os.path.isdir(p) and not os.path.islink(p):
                                shutil.rmtree(p)
                            else:
                                os.remove(p)
                            result["removed"].append(name)
                        except OSError as exc:
                            result["ok"] = False
                            result["errors"].append(f"{name}: {exc}")
                    try:
                        os.rmdir(base)
                        result["removed"].append(
                            os.path.basename(base.rstrip("\\/")) or base
                        )
                    except OSError as exc:
                        result["ok"] = False
                        result["errors"].append(f"rmdir: {exc}")
            elif os.path.exists(base):
                try:
                    os.remove(base)
                    result["removed"].append(
                        os.path.basename(base.rstrip("\\/")) or base
                    )
                except OSError as exc:
                    result["ok"] = False
                    result["errors"].append(str(exc))
        except OSError as exc:
            result["ok"] = False
            result["errors"].append(str(exc))

        # 内存设置回默认，避免继续写已删目录；自定义路径配置一并忘掉
        _snapshot_dir = ""
        _settings_recovered_path = ""
        _settings_recovered_reason = ""
        _settings_recovered_backup = ""
        # 本会话禁止再 makedirs 重建应用数据根
        _data_wiped = True
    else:
        # settings_path() 会经 _app_base_dir 创建目录——仅在文件可能存在时再碰路径
        conf = os.path.join(base, _SETTINGS_FILE)
        try:
            if os.path.isfile(conf):
                os.remove(conf)
                result["removed"].append(_SETTINGS_FILE)
        except OSError as exc:
            result["ok"] = False
            result["errors"].append(str(exc))
    return result


def _settings_dict_impl() -> dict:
    """当前设置快照的实际取值；目录不可用时由 :func:`settings_dict` 兜底。"""
    try:
        builtin = builtin_snapshot_dir()
    except OSError:
        # 内置目录不可用只影响这一项展示信息，不能拖垮自定义目录的正常读写。
        builtin = ""
    snapshot_dir = builtin
    snapshot_dir_available = True
    try:
        snapshot_dir = get_snapshot_dir()
    except SnapshotDirUnavailable as exc:
        # 自定义目录配置保留原值，界面才能显示用户选过的位置并提示重新选择。
        snapshot_dir = exc.path or _snapshot_dir or builtin
        snapshot_dir_available = False
    return {
        "scan_workers": _scan_workers,
        "compress_snapshots": _compress_snapshots,
        "use_mft": _use_mft,
        "search_memory_index": _search_memory_index,
        "temp_cleanup": _temp_cleanup,
        "auto_check_updates": bool(_auto_check_updates),
        "remember_window_size": bool(_remember_window_size),
        "window_width": int(_window_width),
        "window_height": int(_window_height),
        "log_sanitize": bool(_log_sanitize),
        "log_sanitize_explicit": _log_sanitize_explicit is not None,
        "log_level": _normalize_log_level(_log_level),
        "log_level_explicit": _log_level_explicit is not None,
        "settings_path": settings_path(),
        "settings_file_exists": os.path.isfile(settings_path()),
        "app_data_dir": app_data_dir(),
        "account": (_current_user.account if _current_user is not None else ""),
        "data_dir_pointer_invalid": bool(
            _storage_state.get("pointer_invalid")
        ),
        "lang": _lang,
        "theme": _theme,
        "snapshot_dir": snapshot_dir,
        "snapshot_dir_available": snapshot_dir_available,
        "snapshot_dir_unavailable": not snapshot_dir_available,
        "snapshot_dir_configured": _snapshot_dir,
        "snapshot_dir_builtin": builtin,
        "snapshot_dir_is_custom": bool(_snapshot_dir),
        "settings_recovered": settings_recovered(),
        "delete_blacklist": get_delete_blacklist(),
        "pinned_snapshots": get_pinned_snapshots(),
        "ui_parent_row_bg": _ui_parent_row_bg,
        "ui_parent_row_dim": _ui_parent_row_dim,
        "ui_hover_group_outline": _ui_hover_group_outline,
        "ui_hover_group_min": _ui_hover_group_min,
        "ui_tree_guide": _ui_tree_guide,
        "ui_parent_sep": _ui_parent_sep,
        "ui_multiselect_exit_after_add": _ui_multiselect_exit_after_add,
        "ui_multiselect_double_esc": _ui_multiselect_double_esc,
        "ui_toast_info_base": _ui_toast_info_base,
        "ui_toast_info_max": _ui_toast_info_max,
        "ui_toast_warn_base": _ui_toast_warn_base,
        "ui_toast_warn_max": _ui_toast_warn_max,
        "ui_tool_panel_open": _ui_tool_panel_open,
        "ui_tool_panel_open_explicit": _ui_tool_panel_open_explicit is not None,
        "ui_tool_panel_width": _ui_tool_panel_width,
        # AI 配置与通用设置同文件；模块层再决定是否暴露明文 key
        "ai": _ai_payload(),
    }


def settings_dict() -> dict:
    """当前设置快照。

    应用数据目录不可用时返回错误对象；自定义快照目录不可写时仍返回完整
    设置，另带 ``snapshot_dir_unavailable``，界面据此提示重新选择。
    """
    try:
        return _settings_dict_impl()
    except OSError as exc:
        return storage_error_payload(exc)


# ---- 快照文件操作 ----
# 这块只依赖设置的只读部分；拆到 core/snapshots_store.py，此处转发旧名字，
# ``from core.store import list_snapshots`` 等旧导入路径继续可用。

from .snapshots_store import (  # noqa: E402,F401
    SnapshotInfo,
    create_snapshot_folder,
    delete_snapshot,
    delete_snapshot_folder,
    list_snapshot_folders,
    list_snapshots,
    move_snapshot_to_folder,
    new_snapshot_path,
    rename_snapshot_folder,
    sanitize_folder_name,
    set_note,
    snapshot_content_key,
    snapshot_info,
)

