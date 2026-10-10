"""模型列表拉取与连通性测试。"""

from __future__ import annotations

from core import applog
from modules.ai import client as ai_client
from modules.ai import config as ai_config
import threading
import time

from .logfmt import _format_messages_for_log, _format_response_for_log

class ModelsMixin:
    def list_models(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> dict:
        """拉取 OpenAI 兼容 ``/models``。

        可选传入表单草稿中的 base_url / api_key（尚未点完成时也能拉取）。
        空 api_key 表示沿用已保存密钥。
        **不写盘**：列表由前端草稿持有，点设置「完成」再持久化。
        """
        data = ai_config.load(self._app_data_dir())
        base = (
            str(base_url).strip()
            if base_url is not None and str(base_url).strip()
            else (data.get("base_url") or "")
        )
        key_override = str(api_key).strip() if api_key is not None else ""
        key = key_override or ai_config.get_api_key(data)
        if not key:
            applog.debug("AI list_models rejected: missing API key")
            return {
                "error": self._t(
                    "请先填写 API Key",
                    "Please enter an API key first",
                )
            }
        if not base:
            applog.debug("AI list_models rejected: missing base_url")
            return {
                "error": self._t(
                    "请先填写接口地址",
                    "Please enter a base URL first",
                )
            }
        applog.debug(f"AI list_models start | base={base}")
        t0 = time.perf_counter()
        try:
            models = ai_client.list_models(
                base_url=base,
                api_key=key,
                timeout=20.0,
            )
        except ai_client.AiClientError as exc:
            applog.warn(
                f"AI list_models failed | kind={exc.kind}"
                f" | total_ms={int((time.perf_counter() - t0) * 1000)}"
                f" | {exc.message}"
            )
            return {"error": self._format_client_error(exc)}
        except Exception as exc:  # noqa: BLE001
            applog.exception("AI list_models failed", exc)
            return {
                "error": self._t(
                    f"获取模型失败：{exc}",
                    f"Failed to list models: {exc}",
                )
            }

        applog.info(
            f"AI list_models ok | count={len(models)}"
            f" | service_ms={int((time.perf_counter() - t0) * 1000)}"
        )
        return {
            "ok": True,
            "models": list(models),
            "model_options": list(models),
        }

    # ---- 连接测试 ----------------------------------------------------------

    def test_connection(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> dict:
        """发一条最小请求验证配置。

        可选传入草稿中的 base_url / model / api_key，便于点完成前自测。
        """
        data = ai_config.load(self._app_data_dir())
        key_override = str(api_key).strip() if api_key is not None else ""
        key = key_override or ai_config.get_api_key(data)
        model_name = (
            str(model).strip()
            if model is not None and str(model).strip()
            else (data.get("model") or "").strip()
        )
        base = (
            str(base_url).strip()
            if base_url is not None and str(base_url).strip()
            else (data.get("base_url") or "")
        )
        if not key:
            applog.debug("AI test_connection rejected: missing API key")
            return {
                "error": self._t(
                    "请先填写 API Key",
                    "Please enter an API key first",
                )
            }
        if not model_name:
            applog.debug("AI test_connection rejected: missing model")
            return {
                "error": self._t(
                    "请先填写模型名",
                    "Please enter a model name first",
                )
            }

        applog.info(
            f"AI test_connection start | model={model_name} | base={base or '-'}"
        )
        test_messages = [
            {"role": "user", "content": "hi，你是什么模型？"},
        ]
        if applog.is_enabled("DEBUG"):
            applog.debug(
                f"AI test_connection payload | model={model_name}\n"
                f"{_format_messages_for_log(test_messages)}"
            )
        cancel = threading.Event()
        t0 = time.perf_counter()
        try:
            result = ai_client.stream_chat(
                base_url=base,
                api_key=key,
                model=model_name,
                messages=test_messages,
                cancel=cancel,
                connect_timeout=15.0,
                read_timeout=30.0,
            )
            text = (
                result.get("text")
                if isinstance(result, dict)
                else str(result or "")
            )
        except ai_client.AiClientError as exc:
            applog.warn(
                f"AI test_connection failed | kind={exc.kind}"
                f" | total_ms={int((time.perf_counter() - t0) * 1000)}"
                f" | {exc.message}"
            )
            return {"error": self._format_client_error(exc)}
        except Exception as exc:  # noqa: BLE001
            applog.exception("AI test_connection failed", exc)
            return {
                "error": self._t(
                    f"连接失败：{exc}",
                    f"Connection failed: {exc}",
                )
            }
        preview = " ".join(str(text or "").split())
        if len(preview) > 200:
            preview = preview[:200] + "…"
        applog.info(
            f"AI test_connection ok | model={model_name}"
            f" | total_ms={int((time.perf_counter() - t0) * 1000)}"
            f" | preview_chars={len(preview)}"
        )
        if applog.is_enabled("DEBUG"):
            applog.debug(
                f"AI test_connection response | model={model_name}\n"
                f"{_format_response_for_log(text)}"
            )
        return {"ok": True, "preview": preview, "model": model_name}
