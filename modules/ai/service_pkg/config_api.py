"""AI 配置读写：get_config / set_config / reset。"""

from __future__ import annotations

from core import applog
from modules.ai import config as ai_config

from .logfmt import _diff_ai_config

class ConfigMixin:
    def get_config(self) -> dict:
        """返回公开配置视图（无明文 key）。"""
        data = ai_config.load(self._app_data_dir())
        return ai_config.public_view(data)

    def set_config(self, payload: dict | None = None, **kwargs) -> dict:
        """合并写入配置；api_key 只写不读。

        支持 ``set_config({...})`` 或 ``set_config(enabled=True, ...)``
        （module_invoke 以 kwargs 展开传入）。

        无实质变更时不写盘、不打 INFO；有变更时只记录修改字段（前→后）。
        """
        body = dict(payload or {})
        body.update(kwargs)
        before = ai_config.load(self._app_data_dir())
        data = dict(before)

        if "enabled" in body:
            data["enabled"] = bool(body.get("enabled"))
        if "base_url" in body and body.get("base_url") is not None:
            data["base_url"] = str(body.get("base_url") or "").strip()
        if "model" in body and body.get("model") is not None:
            data["model"] = str(body.get("model") or "").strip()
        if "extra_prompt" in body and body.get("extra_prompt") is not None:
            data["extra_prompt"] = str(body.get("extra_prompt") or "")
        if "consented" in body:
            data["consented"] = bool(body.get("consented"))
        if "model_options" in body and body.get("model_options") is not None:
            opts = body.get("model_options")
            cleaned: list[str] = []
            if isinstance(opts, list):
                seen: set[str] = set()
                for item in opts:
                    s = str(item or "").strip()
                    if not s or s in seen:
                        continue
                    seen.add(s)
                    cleaned.append(s)
                    if len(cleaned) >= 500:
                        break
            data["model_options"] = cleaned
        if "cleanup_max_depth" in body and body.get("cleanup_max_depth") is not None:
            data["cleanup_max_depth"] = ai_config._normalize_cleanup_max_depth(
                body.get("cleanup_max_depth"),
                default=data.get("cleanup_max_depth"),
            )
        if "enabled_tools" in body:
            data["enabled_tools"] = ai_config._normalize_enabled_tools(
                body.get("enabled_tools")
            )
        elif "tools_enabled" in body:
            # 兼容旧布尔：false → 空列表；true → 默认目录
            data["enabled_tools"] = ai_config._normalize_enabled_tools(
                None, legacy_tools_enabled=body.get("tools_enabled")
            )

        # key：空串 / 缺省 = 保留原值；非空 = 明文写入；clear_key 清空
        if "api_key" in body:
            key = body.get("api_key")
            if key is None:
                pass
            else:
                key_s = str(key).strip()
                if key_s:
                    data["api_key"] = key_s
                elif body.get("clear_key"):
                    data["api_key"] = ""

        changes = _diff_ai_config(before, data)
        if not changes:
            # 设置页点「完成」总会带上 AI 草稿；无改动则静默返回
            return {"ok": True, "unchanged": True, **ai_config.public_view(before)}

        try:
            saved = ai_config.save(self._app_data_dir(), data)
        except OSError as exc:
            applog.exception("AI set_config failed", exc)
            return {
                "error": self._t(
                    f"保存 AI 配置失败：{exc}",
                    f"Failed to save AI settings: {exc}",
                )
            }
        view = ai_config.public_view(saved)
        # 统一设置变更出口（DEBUG）；路径/字段原文写入，脱敏由 applog 开关决定
        applog.log_settings_changed("ai", changes)
        return {"ok": True, **view}

    def reset(self) -> dict:
        """恢复 AI 默认配置（恢复默认设置时由 app 调用）。"""
        with self._lock:
            self._cancel.set()
            self._active_id = None
            self._clear_tool_loops()
            self._clear_cleanup_jobs()
        ai_config.reset(self._app_data_dir())
        applog.log_settings_event("ai", "reset to defaults", level="INFO")
        return {"ok": True}
