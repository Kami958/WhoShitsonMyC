"""settings.yaml 文件读写与分节视图（common / ui / ai 三节）。"""

from __future__ import annotations

import os
from . import wingeom
from .store_values import (
    _AI_DEFAULTS,
    _as_bool,
    _default_enabled_tools,
    _delete_blacklist_to_yaml,
    _enabled_tools_to_yaml,
    _model_options_to_yaml,
    _normalize_cleanup_max_depth,
    _normalize_lang,
    _normalize_log_level,
    _normalize_theme,
    _normalize_ui_hover_group_min,
    _pinned_snapshots_to_yaml,
    _put_raw_pair,
    _yaml_quote,
)


def _load_settings_yaml(path: str) -> dict:
    """读 settings.yaml，尽量原样收下键值。

    支持::

        # 按节（common / ai 都是同级顶层节）
        common:
          scan_workers: 4
        ai:
          enabled: false
        future_tab:
          foo: bar

        # 旧：顶层扁平（无节名）
        scan_workers: 4

    规则：
    - 忽略注释与空行
    - 值一律先当字符串（去引号）；类型转换在 :func:`_apply_loaded` 里做
    - 每个顶层节整段进 ``out[节名]`` 子 dict（``common`` 与 ``ai`` 同等对待）
    - 旧扁平键留在顶层；:func:`_apply_loaded` 会把它们当作 common 读
    - 未知键 / 未知节都会保留

    失败或文件不存在返回空 dict。
    """
    out: dict = {}
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return out

    section: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        # 仅接受空格缩进（YAML 惯例）
        indent = len(raw) - len(raw.lstrip(" "))
        s = raw.strip()
        if ":" not in s:
            continue
        key, _, rest = s.partition(":")
        key = key.strip()
        val = rest.strip()
        if indent == 0 and not val and key:
            section = key.lower()
            out.setdefault(section, {})
            if not isinstance(out[section], dict):
                out[section] = {}
            continue
        if indent == 0 and val:
            # 旧扁平：顶层 key: value
            section = None
            _put_raw_pair(out, key, val)
            continue
        if indent > 0:
            if section is None:
                _put_raw_pair(out, key, val)
            else:
                body = out.setdefault(section, {})
                if not isinstance(body, dict):
                    body = {}
                    out[section] = body
                _put_raw_pair(body, key, val)
    return out


def _write_settings_yaml(path: str, data: dict) -> None:
    """写 settings.yaml（只序列化程序管理的字段）。

    解析层会保留未知键，但写回由程序覆盖整文件——扩展新设置时在这里补一行即可。
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    snap = data.get("snapshot_dir") or ""
    ai = data.get("ai") if isinstance(data.get("ai"), dict) else {}
    base_url = (ai.get("base_url") or _AI_DEFAULTS["base_url"]).strip() or _AI_DEFAULTS[
        "base_url"
    ]
    lines = [
        "# WhoShitsOnMyC settings — auto-written when settings change",
        "# Sections map to settings tabs (common = 通用, ui = 界面, ai = AI).",
        "# Missing keys use built-in defaults on load.",
        "common:",
        f"  scan_workers: {int(data['scan_workers'])}",
        f"  compress_snapshots: {'true' if data.get('compress_snapshots') else 'false'}",
        f"  use_mft: {'true' if data.get('use_mft') else 'false'}",
        f"  search_memory_index: {'true' if data.get('search_memory_index', True) else 'false'}",
        f"  temp_cleanup: {'true' if data.get('temp_cleanup', True) else 'false'}",
        f"  auto_check_updates: {'true' if data.get('auto_check_updates', True) else 'false'}",
        f"  remember_window_size: {'true' if data.get('remember_window_size', True) else 'false'}",
        f"  window_width: {int(data.get('window_width') or 0)}",
        f"  window_height: {int(data.get('window_height') or 0)}",
        f"  log_sanitize: {'true' if data.get('log_sanitize', True) else 'false'}",
        f"  log_level: {_normalize_log_level(data.get('log_level', 'INFO'))}",
        f"  lang: {_normalize_lang(data.get('lang', 'en'))}",
        f"  theme: {_normalize_theme(data.get('theme', 'light'))}",
        f"  snapshot_dir: {_yaml_quote(snap)}",
        f"  delete_blacklist: {_yaml_quote(_delete_blacklist_to_yaml(data.get('delete_blacklist') or []))}",
        f"  pinned_snapshots: {_yaml_quote(_pinned_snapshots_to_yaml(data.get('pinned_snapshots') or []))}",
        "ui:",
        f"  ui_parent_row_bg: {'true' if data.get('ui_parent_row_bg', False) else 'false'}",
        f"  ui_parent_row_dim: {'true' if data.get('ui_parent_row_dim', False) else 'false'}",
        f"  ui_hover_group_outline: {'true' if data.get('ui_hover_group_outline', False) else 'false'}",
        f"  ui_hover_group_min: {_normalize_ui_hover_group_min(data.get('ui_hover_group_min', 10))}",
        f"  ui_tree_guide: {'true' if data.get('ui_tree_guide', True) else 'false'}",
        f"  ui_parent_sep: {'true' if data.get('ui_parent_sep', False) else 'false'}",
        f"  ui_multiselect_exit_after_add: {'true' if data.get('ui_multiselect_exit_after_add', True) else 'false'}",
        f"  ui_multiselect_double_esc: {'true' if data.get('ui_multiselect_double_esc', True) else 'false'}",
        f"  ui_tool_panel_open: {'true' if data.get('ui_tool_panel_open', False) else 'false'}",
        f"  ui_tool_panel_width: {int(data.get('ui_tool_panel_width') or wingeom.PANEL_W_DEFAULT)}",
        "ai:",
        f"  enabled: {'true' if _as_bool(ai.get('enabled'), False) else 'false'}",
        f"  base_url: {_yaml_quote(base_url)}",
        f"  model: {_yaml_quote(str(ai.get('model') or ''))}",
        f"  api_key: {_yaml_quote(str(ai.get('api_key') or ''))}",
        f"  extra_prompt: {_yaml_quote(str(ai.get('extra_prompt') or ''))}",
        f"  consented: {'true' if _as_bool(ai.get('consented'), False) else 'false'}",
        f"  model_options: {_yaml_quote(_model_options_to_yaml(ai.get('model_options') or []))}",
        f"  cleanup_max_depth: {int(_normalize_cleanup_max_depth(ai.get('cleanup_max_depth')))}",
        f"  enabled_tools: {_yaml_quote(_enabled_tools_to_yaml(ai.get('enabled_tools') if ai.get('enabled_tools') is not None else _default_enabled_tools()))}",
        "",
    ]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))
    os.replace(tmp, path)


def _common_view(data: dict) -> dict:
    """取出通用设置视图：优先 ``common`` 节，再并入旧顶层扁平键。"""
    common: dict = {}
    if isinstance(data.get("common"), dict):
        common.update(data["common"])
    for k, v in data.items():
        if k == "common" or isinstance(v, dict):
            continue
        # 旧扁平键：节里没有时才用顶层
        if k not in common:
            common[k] = v
    return common


def _ui_view(data: dict) -> dict:
    """取出界面设置视图：优先 ``ui`` 节，兼容旧 common/顶层位置的 ``ui_*`` 键。"""
    ui: dict = {}
    if isinstance(data.get("ui"), dict):
        ui.update(data["ui"])
    for k, v in _common_view(data).items():
        if k.startswith("ui_") and k not in ui:
            ui[k] = v
    return ui
