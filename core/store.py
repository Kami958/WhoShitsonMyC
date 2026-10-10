"""快照的存放位置、命名与列举。

负责「快照存哪、叫什么名、有哪些」这类应用层事务，与纯遍历/对比逻辑分开。
快照固定存于用户数据目录（Windows 为
``%LOCALAPPDATA%\\WhoShitsOnMyC\\snapshots``），其它平台回落到
``~/.local/share`` 或 ``~``，不提供迁移——位置唯一、行为可预期。

应用设置默认不落盘：无 ``settings.yaml`` 时用内置默认。
用户改过设置（设置页点「完成」、或切换语言/主题等）后自动写入该文件；
「恢复默认」删除文件并把内存值重置为内置默认。
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable

from .compress import (
    drop_cache_for,
    is_snapshot_filename,
)
from . import wingeom

from .store_paths import (
    _is_rotational_system_drive,
    _same_dir,
    default_scan_workers,
    is_rotational_drive,
    migrate_snapshots,
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
    _ui_view,
    _write_settings_yaml,
)

_APP_DIR_NAME = "WhoShitsOnMyC"
_SETTINGS_FILE = "settings.yaml"


class StoreError(Exception):
    """快照存储管理相关错误。"""


# 卸载 wipe 后置位：禁止再 makedirs 把 WhoShitsOnMyC 建回来（直到进程退出）
_data_wiped = False


def _app_base_dir_path() -> str:
    """应用数据根路径（**不**创建目录）。"""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get(
            "XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share")
        )
    return os.path.join(base, _APP_DIR_NAME)


def _app_base_dir() -> str:
    """返回应用数据根目录（必要时创建）。默认快照目录就在这。

    卸载 wipe 后本会话内**不再**创建，以免空文件夹又出现。
    """
    path = _app_base_dir_path()
    if not _data_wiped:
        os.makedirs(path, exist_ok=True)
    return path


def builtin_snapshot_dir() -> str:
    """内置快照目录（应用数据根下的 ``snapshots``，必要时创建）。"""
    path = os.path.join(_app_base_dir(), "snapshots")
    if not _data_wiped:
        os.makedirs(path, exist_ok=True)
    return path


def default_snapshot_dir() -> str:
    """当前生效的快照存放目录（自定义或内置；必要时创建）。"""
    return get_snapshot_dir()


def settings_path() -> str:
    """``settings.yaml`` 绝对路径（位于应用数据根）。"""
    return os.path.join(_app_base_dir(), _SETTINGS_FILE)


def _delete_settings_yaml() -> None:
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
        "ui_tool_panel_open": _ui_tool_panel_open,
        "ui_tool_panel_width": _ui_tool_panel_width,
        "ai": _ai_payload(),
    }


def _persist() -> None:
    """把当前内存全部设置写回 YAML（改设置后自动调用）。"""
    _write_settings_yaml(settings_path(), _settings_payload())


def reload_settings_from_disk() -> None:
    """从默认路径的 ``settings.yaml`` 重新加载。

    无文件则不改动内存；有文件则按键覆盖，缺项保留当前/默认值。
    """
    data = _load_settings_yaml(settings_path())
    _apply_loaded(data)


# 模块加载时：默认路径有 yaml 则优先读入
_apply_loaded(_load_settings_yaml(settings_path()))


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
    """当前生效的快照目录（自定义或内置；必要时创建）。"""
    if _snapshot_dir:
        path = _snapshot_dir
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            # 自定义路径不可用时回退内置，避免扫描/列举直接炸
            return builtin_snapshot_dir()
        return path
    return builtin_snapshot_dir()


def get_snapshot_dir_configured() -> str:
    """用户配置的自定义路径；空串表示使用内置目录。"""
    return _snapshot_dir


def set_snapshot_dir(path: str | None) -> str:
    """设置快照存放目录。

    空 / None → 恢复内置目录。非空路径会 abspath 并尝试创建。
    返回**生效**的绝对路径（内置或自定义）。值变化时写 YAML。
    """
    global _snapshot_dir
    raw = _normalize_snapshot_dir(path)
    if not raw:
        if _snapshot_dir:
            _snapshot_dir = ""
            _persist()
        return builtin_snapshot_dir()
    try:
        os.makedirs(raw, exist_ok=True)
    except OSError as exc:
        raise OSError(f"cannot create snapshot dir: {raw} ({exc})") from exc
    if not os.path.isdir(raw):
        raise OSError(f"snapshot dir is not a directory: {raw}")
    if raw != _snapshot_dir:
        _snapshot_dir = raw
        _persist()
    return _snapshot_dir


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
    ``ui_tool_panel_open``、``ui_tool_panel_width``。
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
    global _temp_cleanup, _auto_check_updates

    if not isinstance(payload, dict):
        payload = {}

    old_snap_dir = get_snapshot_dir()
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
    if "ui_tool_panel_open" in payload:
        _ui_tool_panel_open = bool(payload["ui_tool_panel_open"])
        _ui_tool_panel_open_explicit = _ui_tool_panel_open
    if "ui_tool_panel_width" in payload:
        _ui_tool_panel_width = _normalize_ui_tool_panel_width(
            payload.get("ui_tool_panel_width")
        )
    if "snapshot_dir" in payload:
        raw = _normalize_snapshot_dir(payload.get("snapshot_dir"))
        if not raw:
            _snapshot_dir = ""
        else:
            try:
                os.makedirs(raw, exist_ok=True)
            except OSError as exc:
                raise OSError(f"cannot create snapshot dir: {raw} ({exc})") from exc
            if not os.path.isdir(raw):
                raise OSError(f"snapshot dir is not a directory: {raw}")
            _snapshot_dir = raw
        dir_changed = not _same_dir(old_snap_dir, get_snapshot_dir())

    migrate_info: dict = {
        "moved": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
        "total": 0,
    }
    if dir_changed:
        new_dir = get_snapshot_dir()
        migrate_info = migrate_snapshots(old_snap_dir, new_dir, progress=progress)

    _persist()
    out = settings_dict()
    out["snapshot_dir_changed"] = dir_changed
    out["migrate"] = migrate_info
    return out


def reset_settings_to_defaults(*, lang: str | None = None) -> dict:
    """恢复内置默认设置并删除 ``settings.yaml``。

    不删除快照文件，仅清空自定义快照目录配置（回到内置目录）。
    ``lang`` 由调用方传入冷启动默认语言（系统语言）；省略则 ``en``。
    """
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
    _ui_tool_panel_open = False
    _ui_tool_panel_open_explicit = None
    _ui_tool_panel_width = wingeom.PANEL_W_DEFAULT
    _lang = _normalize_lang(lang) if lang is not None else "en"
    _ai_enabled = False
    _ai_base_url = str(_AI_DEFAULTS["base_url"])
    _ai_model = ""
    _ai_api_key = ""
    _ai_extra_prompt = ""
    _ai_consented = False
    _ai_model_options = []
    _ai_cleanup_max_depth = int(_AI_DEFAULTS["cleanup_max_depth"])
    _ai_enabled_tools = list(_default_enabled_tools())
    _delete_settings_yaml()
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
    """应用数据根（``%LOCALAPPDATA%\\WhoShitsOnMyC`` 等）。"""
    return _app_base_dir()


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

    # 切勿调用 _app_base_dir()：它会 makedirs 把刚要删的目录又建出来
    base = _app_base_dir_path()
    result: dict = {
        "ok": True,
        "deleted_data": bool(delete_data),
        "path": base,
        "removed": [],
        "errors": [],
    }
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


def settings_dict() -> dict:
    """当前设置快照（供 API / 测试）。"""
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
        "lang": _lang,
        "theme": _theme,
        "snapshot_dir": get_snapshot_dir(),
        "snapshot_dir_configured": _snapshot_dir,
        "snapshot_dir_builtin": builtin_snapshot_dir(),
        "snapshot_dir_is_custom": bool(_snapshot_dir),
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
        "ui_tool_panel_open": _ui_tool_panel_open,
        "ui_tool_panel_open_explicit": _ui_tool_panel_open_explicit is not None,
        "ui_tool_panel_width": _ui_tool_panel_width,
        # AI 配置与通用设置同文件；模块层再决定是否暴露明文 key
        "ai": _ai_payload(),
    }

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

