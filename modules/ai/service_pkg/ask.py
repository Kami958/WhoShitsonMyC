"""单轮提问：ask / cancel 与后台执行体 _run_ask。"""

from __future__ import annotations

from core import applog
from modules.ai import client as ai_client
from modules.ai import config as ai_config
from modules.ai import prompts as ai_prompts
from modules.ai import tools as ai_tools
import threading
import time
import uuid

from .logfmt import _format_messages_for_log, _format_response_for_log

class AskMixin:
    def ask(
        self,
        context: dict | None = None,
        question: str = "",
        history: list | None = None,
    ) -> dict:
        """后台流式提问；立即返回 ``{id}``，内容经 emit 推送。"""
        data = ai_config.load(self._app_data_dir())
        if not data.get("consented"):
            applog.debug("AI ask rejected: consent required")
            return {
                "error": self._t(
                    "请先确认隐私说明",
                    "Please accept the privacy notice first",
                ),
                "need_consent": True,
            }
        if not data.get("enabled"):
            applog.debug("AI ask rejected: disabled")
            return {
                "error": self._t(
                    "请先在设置中启用 AI",
                    "Enable AI in Settings first",
                ),
                "need_enable": True,
            }
        key = ai_config.get_api_key(data)
        model = (data.get("model") or "").strip()
        base = data.get("base_url") or ""
        if not key:
            applog.debug("AI ask rejected: missing API key")
            return {
                "error": self._t(
                    "请先在设置中填写 API Key",
                    "Please set an API key in Settings first",
                )
            }
        if not model:
            applog.debug("AI ask rejected: missing model")
            return {
                "error": self._t(
                    "请先在设置中填写模型名",
                    "Please set a model name in Settings first",
                )
            }

        req_id = uuid.uuid4().hex[:12]
        lang = self._get_lang() or "en"
        ctx = dict(context or {})
        messages = ai_prompts.build_messages(
            question=str(question or ""),
            context=ctx,
            extra_prompt=str(data.get("extra_prompt") or ""),
            lang=lang,
            history=list(history or []),
        )
        q_len = len(str(question or ""))
        hist_n = len(list(history or []))
        ctx_kind = str(ctx.get("kind") or ctx.get("type") or "")
        # 用户启用的 tool 列表 + 有节点上下文时注入；自由聊不带 tools
        enabled_tools = ai_tools.normalize_enabled_tools(
            data.get("enabled_tools")
            if data.get("enabled_tools") is not None
            else ai_tools.default_enabled_tools()
        )
        # 兼容旧配置字段
        if "enabled_tools" not in data and data.get("tools_enabled") is False:
            enabled_tools = []
        use_tools = ai_tools.context_allows_propose_tools(
            ctx, enabled_tools=enabled_tools
        )
        tools_payload = (
            ai_tools.openai_tool_definitions(enabled_tools) if use_tools else None
        )
        meta_path_cap = (
            ai_tools.count_context_meta_paths(ctx) if use_tools else 0
        )
        default_root = (
            ai_tools.default_root_from_context(ctx) if use_tools else ""
        )

        with self._lock:
            # 同一时刻只允许一个进行中请求；新 ask 清掉未完成的 tool loop
            self._cancel.set()
            self._cancel = threading.Event()
            self._active_id = req_id
            self._clear_tool_loops()
            cancel = self._cancel
            thread = threading.Thread(
                target=self._run_ask,
                args=(
                    req_id,
                    base,
                    key,
                    model,
                    messages,
                    cancel,
                    tools_payload,
                    meta_path_cap,
                    default_root,
                ),
                daemon=True,
            )
            self._thread = thread
            thread.start()
        applog.info(f"AI ask started | id={req_id} | model={model}")
        applog.debug(
            f"AI ask detail | id={req_id}"
            f" | q_len={q_len} | history={hist_n}"
            f" | ctx={ctx_kind or '-'} | msgs={len(messages)}"
            f" | tools={'yes' if use_tools else 'no'}"
            f" | meta_cap={meta_path_cap}"
            f" | base={base or '-'}"
        )
        # 实际发送内容：仅 DEBUG；路径等由 applog 脱敏
        if applog.is_enabled("DEBUG"):
            applog.debug(
                f"AI ask payload | id={req_id} | model={model}\n"
                f"{_format_messages_for_log(messages)}"
            )
        return {"id": req_id}

    def cancel(self, id: str = "") -> dict:  # noqa: A002 - 与前端 kwargs 对齐
        """取消进行中的请求；id 不匹配时仍取消当前活跃请求。

        同时丢弃未完成的 tool loop（含仅等人审、无活跃 stream 的情况）。
        """
        with self._lock:
            active = self._active_id
            had_loop = bool(self._pending_tool_loops)
            if id:
                self._drop_tool_loop(id)
            else:
                self._clear_tool_loops()
            if active is None and not had_loop:
                return {"ok": True, "cancelled": False}
            if id and active and active != id:
                # 仍取消当前 stream（前端可能丢了 id）
                pass
            self._cancel.set()
            self._active_id = None
        applog.info(f"AI ask cancelled | id={id or active or '-'}")
        return {"ok": True, "cancelled": True}

    def _run_ask(
        self,
        req_id: str,
        base: str,
        key: str,
        model: str,
        messages: list[dict],
        cancel: threading.Event,
        tools: list | None = None,
        meta_path_cap: int = 0,
        default_root: str = "",
    ) -> None:
        t0 = time.perf_counter()
        first_emit_ms = -1

        def on_delta(text: str) -> None:
            nonlocal first_emit_ms
            if cancel.is_set():
                return
            if first_emit_ms < 0:
                first_emit_ms = int((time.perf_counter() - t0) * 1000)
            self._emit("ai-chunk", {"id": req_id, "text": text})

        try:
            # 第一轮 stream；若有 propose 则暂存 loop，等人审后 continue_tools
            result = ai_client.stream_chat(
                base_url=base,
                api_key=key,
                model=model,
                messages=messages,
                cancel=cancel,
                on_delta=on_delta,
                tools=tools,
            )
            if isinstance(result, dict):
                full_text = str(result.get("text") or "")
                raw_calls = result.get("tool_calls") or []
            else:
                full_text = str(result or "")
                raw_calls = []
            total_ms = int((time.perf_counter() - t0) * 1000)
            if cancel.is_set():
                applog.info(
                    f"AI ask stopped | id={req_id}"
                    f" | total_ms={total_ms} | first_emit_ms={first_emit_ms}"
                )
                if applog.is_enabled("DEBUG") and full_text:
                    applog.debug(
                        f"AI ask response (partial, stopped) | id={req_id}\n"
                        f"{_format_response_for_log(full_text)}"
                    )
                self._emit(
                    "ai-error",
                    {
                        "id": req_id,
                        "message": self._t("已停止", "Stopped"),
                        "cancelled": True,
                    },
                )
            else:
                propose_n = 0
                stored_calls: list[dict] = []
                if tools and raw_calls and not cancel.is_set():
                    propose_n, stored_calls = self._emit_tool_proposes(
                        req_id,
                        raw_calls,
                        meta_path_cap=meta_path_cap,
                        default_root=default_root,
                    )
                awaiting = bool(stored_calls)
                if awaiting:
                    with self._lock:
                        self._pending_tool_loops[req_id] = {
                            "messages": list(messages),
                            "assistant_text": full_text,
                            "tool_calls": stored_calls,
                            "base": base,
                            "key": key,
                            "model": model,
                            "created_at": time.time(),
                        }
                applog.info(
                    f"AI ask done | id={req_id} | model={model}"
                    f" | total_ms={total_ms} | first_emit_ms={first_emit_ms}"
                    f" | reply_chars={len(full_text or '')}"
                    f" | tool_calls={len(raw_calls) if isinstance(raw_calls, list) else 0}"
                    f" | propose={propose_n}"
                    f" | awaiting_tools={awaiting}"
                )
                if applog.is_enabled("DEBUG"):
                    applog.debug(
                        f"AI ask response | id={req_id} | model={model}\n"
                        f"{_format_response_for_log(full_text)}"
                    )
                self._emit(
                    "ai-done",
                    {"id": req_id, "awaiting_tools": awaiting},
                )
        except ai_client.AiClientError as exc:
            total_ms = int((time.perf_counter() - t0) * 1000)
            if exc.kind == "cancelled" or cancel.is_set():
                applog.info(
                    f"AI ask cancelled | id={req_id}"
                    f" | total_ms={total_ms} | first_emit_ms={first_emit_ms}"
                )
                self._emit(
                    "ai-error",
                    {
                        "id": req_id,
                        "message": self._t("已停止", "Stopped"),
                        "cancelled": True,
                    },
                )
            else:
                applog.warn(
                    f"AI ask failed | id={req_id} | kind={exc.kind}"
                    f" | total_ms={total_ms} | first_emit_ms={first_emit_ms}"
                    f" | {exc.message}"
                )
                self._emit(
                    "ai-error",
                    {"id": req_id, "message": self._format_client_error(exc)},
                )
        except Exception as exc:  # noqa: BLE001
            total_ms = int((time.perf_counter() - t0) * 1000)
            applog.exception(
                f"AI ask failed | id={req_id} | total_ms={total_ms}", exc
            )
            self._emit(
                "ai-error",
                {
                    "id": req_id,
                    "message": self._t(
                        f"请求失败：{exc}",
                        f"Request failed: {exc}",
                    ),
                },
            )
        finally:
            with self._lock:
                if self._active_id == req_id:
                    self._active_id = None
