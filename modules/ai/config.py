"""AI 配置：读写 ``settings.yaml`` 的 ``ai:`` 节（经 store）。

API Key 明文保存在本机设置文件中（不加密）。
旧版独立 ``ai.json`` 若仍存在会在首次读取时迁移后删除。
"""

from __future__ import annotations

import json
import os
from typing import Any

from core import applog
from core import store
from modules.ai import tools as ai_tools


_LEGACY_NAME = "ai.json"

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "base_url": "https://api.openai.com/v1",
    "model": "",
    "api_key": "",
    "extra_prompt": "",
    "consented": False,
    "model_options": [],
    "cleanup_max_depth": 3,
    # None = 使用 tools 目录默认；load 时归一为 list[str]
    "enabled_tools": None,
}

_CLEANUP_DEPTH_MIN = 1
_CLEANUP_DEPTH_MAX = 8


def _normalize_cleanup_max_depth(val: Any, default: int | None = None) -> int:
    """合法清理深度 1–8；失败用 default 或内置默认。"""
    fb = int(_DEFAULTS["cleanup_max_depth"] if default is None else default)
    try:
        n = int(val)
    except (TypeError, ValueError):
        return fb
    if n < _CLEANUP_DEPTH_MIN:
        return _CLEANUP_DEPTH_MIN
    if n > _CLEANUP_DEPTH_MAX:
        return _CLEANUP_DEPTH_MAX
    return n


def _normalize_enabled_tools(
    raw: Any, *, legacy_tools_enabled: Any = None
) -> list[str]:
    """合法已启用 tool 列表；兼容旧 ``tools_enabled`` 布尔。"""
    if raw is None and legacy_tools_enabled is not None:
        if legacy_tools_enabled is False or str(legacy_tools_enabled).strip().lower() in (
            "0",
            "false",
            "no",
            "off",
        ):
            return []
        if legacy_tools_enabled is True or str(legacy_tools_enabled).strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            return list(ai_tools.default_enabled_tools())
    if raw is None:
        return list(ai_tools.default_enabled_tools())
    return list(ai_tools.normalize_enabled_tools(raw))


def _legacy_path(app_data_dir: str) -> str:
    return os.path.join(app_data_dir, _LEGACY_NAME)


def _migrate_legacy_ai_json(app_data_dir: str) -> None:
    """若存在旧 ai.json，合并进 settings.yaml 后删除。

    只在 store 当前 AI 仍是默认空配置时迁入，避免覆盖用户已写入的设置。
    """
    path = _legacy_path(app_data_dir)
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raw = {}
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raw = {}

    current = store.get_ai_settings()
    looks_default = (
        not current.get("enabled")
        and not (current.get("model") or "").strip()
        and not (current.get("api_key") or "").strip()
        and not current.get("consented")
        and not (current.get("model_options") or [])
    )
    if looks_default and raw:
        # 旧版 api_key_enc 无法在无 DPAPI 路径可靠还原；有明文 api_key 才迁
        key = str(raw.get("api_key") or "").strip()
        payload = {
            "enabled": bool(raw.get("enabled", False)),
            "base_url": str(raw.get("base_url") or _DEFAULTS["base_url"]).strip()
            or _DEFAULTS["base_url"],
            "model": str(raw.get("model") or "").strip(),
            "extra_prompt": str(raw.get("extra_prompt") or ""),
            "consented": bool(raw.get("consented", False)),
            "model_options": raw.get("model_options") or [],
        }
        if key:
            payload["api_key"] = key
        try:
            store.set_ai_settings(payload)
            applog.info(
                "AI legacy ai.json migrated"
                f" | has_key={bool(key)}"
                f" | model={payload.get('model') or '-'}"
            )
        except OSError as exc:
            applog.exception("AI legacy ai.json migrate failed", exc)
    try:
        os.remove(path)
        applog.debug("AI legacy ai.json removed")
    except OSError as exc:
        applog.warn(f"AI legacy ai.json remove failed: {exc}")


def load(app_data_dir: str = "") -> dict[str, Any]:
    """读取 AI 配置；返回含明文 api_key 的 dict。"""
    base = app_data_dir or store.app_data_dir()
    _migrate_legacy_ai_json(base)
    data = store.get_ai_settings()
    out = dict(_DEFAULTS)
    out.update(data or {})
    out["base_url"] = (
        str(out.get("base_url") or _DEFAULTS["base_url"]).strip()
        or _DEFAULTS["base_url"]
    )
    out["model"] = str(out.get("model") or "").strip()
    out["api_key"] = str(out.get("api_key") or "")
    out["extra_prompt"] = str(out.get("extra_prompt") or "")
    out["enabled"] = bool(out.get("enabled"))
    out["consented"] = bool(out.get("consented"))
    opts = out.get("model_options") or []
    out["model_options"] = (
        [str(x).strip() for x in opts if str(x or "").strip()]
        if isinstance(opts, list)
        else []
    )
    out["cleanup_max_depth"] = _normalize_cleanup_max_depth(
        out.get("cleanup_max_depth")
    )
    # enabled_tools 优先；兼容旧 tools_enabled 布尔
    src = data or {}
    if "enabled_tools" in src and src.get("enabled_tools") is not None:
        out["enabled_tools"] = _normalize_enabled_tools(src.get("enabled_tools"))
    elif "tools_enabled" in src:
        out["enabled_tools"] = _normalize_enabled_tools(
            None, legacy_tools_enabled=src.get("tools_enabled")
        )
    else:
        out["enabled_tools"] = _normalize_enabled_tools(None)
    out.pop("tools_enabled", None)
    return out


def save(app_data_dir: str, data: dict[str, Any]) -> dict[str, Any]:
    """写入 AI 配置到 settings.yaml；返回规范化后的数据。"""
    # app_data_dir 保留参数以兼容旧调用；实际路径由 store 决定
    _ = app_data_dir
    if "enabled_tools" in data:
        enabled = _normalize_enabled_tools(data.get("enabled_tools"))
    else:
        enabled = _normalize_enabled_tools(
            None, legacy_tools_enabled=data.get("tools_enabled")
        )
    payload = {
        "enabled": bool(data.get("enabled", False)),
        "base_url": (
            str(data.get("base_url") or _DEFAULTS["base_url"]).strip()
            or _DEFAULTS["base_url"]
        ),
        "model": str(data.get("model") or "").strip(),
        "api_key": str(data.get("api_key") or ""),
        "extra_prompt": str(data.get("extra_prompt") or ""),
        "consented": bool(data.get("consented", False)),
        "model_options": data.get("model_options") or [],
        "cleanup_max_depth": _normalize_cleanup_max_depth(
            data.get("cleanup_max_depth")
        ),
        "enabled_tools": enabled,
        # 直接覆盖 key（含清空）
        "clear_key": True,
    }
    return store.set_ai_settings(payload)


def reset(app_data_dir: str = "") -> None:
    """清理 AI 配置。

    - 删除旧版 ``ai.json``（若有）
    - 若 ``settings.yaml`` 仍存在：把 ai 节写回默认
    - 若配置文件已不存在（例如刚「恢复默认」删过）：只保证不再残留独立文件，
      不重新创建 yaml（内存清理由 store.reset_settings_to_defaults 负责）
    """
    base = app_data_dir or store.app_data_dir()
    legacy = _legacy_path(base)
    try:
        if os.path.isfile(legacy):
            os.remove(legacy)
    except OSError:
        pass
    if not os.path.isfile(store.settings_path()):
        return
    store.set_ai_settings(
        {
            "enabled": False,
            "base_url": _DEFAULTS["base_url"],
            "model": "",
            "api_key": "",
            "extra_prompt": "",
            "consented": False,
            "model_options": [],
            "cleanup_max_depth": int(_DEFAULTS["cleanup_max_depth"]),
            "enabled_tools": list(ai_tools.default_enabled_tools()),
            "clear_key": True,
        }
    )


def public_view(data: dict[str, Any]) -> dict[str, Any]:
    """给前端的配置视图：不返回明文 key，只给 has_key。"""
    key = str(data.get("api_key") or "").strip()
    opts = data.get("model_options") or []
    if not isinstance(opts, list):
        opts = []
    if "enabled_tools" in data:
        enabled = _normalize_enabled_tools(data.get("enabled_tools"))
    else:
        enabled = _normalize_enabled_tools(
            None, legacy_tools_enabled=data.get("tools_enabled")
        )
    return {
        "enabled": bool(data.get("enabled")),
        "base_url": data.get("base_url") or _DEFAULTS["base_url"],
        "model": data.get("model") or "",
        "has_key": bool(key),
        "extra_prompt": data.get("extra_prompt") or "",
        "consented": bool(data.get("consented")),
        "model_options": [str(x) for x in opts if str(x or "").strip()],
        "cleanup_max_depth": _normalize_cleanup_max_depth(
            data.get("cleanup_max_depth")
        ),
        "enabled_tools": enabled,
        "tool_catalog": [
            {
                "name": t["name"],
                "label_zh": t.get("label_zh") or t["name"],
                "label_en": t.get("label_en") or t["name"],
                "desc_zh": t.get("desc_zh") or "",
                "desc_en": t.get("desc_en") or "",
            }
            for t in ai_tools.CATALOG_TOOLS
        ],
    }


def get_api_key(data: dict[str, Any]) -> str:
    """取出 API key。"""
    return str(data.get("api_key") or "").strip()
