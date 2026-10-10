"""工具续跑：continue_tools / _run_continue 与工具提议上报。"""

from __future__ import annotations

from core import applog
from modules.ai import client as ai_client
from modules.ai import tools as ai_tools
from typing import Any
import threading
import time

from .logfmt import _format_messages_for_log, _format_response_for_log

class ToolFlowMixin:
    def continue_tools(
        self,
        id: str = "",  # noqa: A002
        results: list | None = None,
        **kwargs: Any,
    ) -> dict:
        """用户审批 tool 后第二轮续写；不阻塞等人，由前端在确认后调用。"""
        req_id = str(id or kwargs.get("id") or "").strip()
        raw_results = results if results is not None else kwargs.get("results")
        if not req_id:
            return {
                "error": self._t("缺少请求 id", "Missing request id"),
            }
        with self._lock:
            loop = self._pending_tool_loops.pop(req_id, None)
            if not loop:
                return {
                    "error": self._t(
                        "没有可继续的工具请求，可能已过期或已处理",
                        "No pending tool request (expired or already handled)",
                    )
                }
            if self._active_id is not None:
                # 有其他进行中 stream：把 loop 放回并拒绝
                self._pending_tool_loops[req_id] = loop
                return {
                    "error": self._t(
                        "当前有进行中的请求，请稍后再试",
                        "Another request is in progress",
                    )
                }
            self._cancel = threading.Event()
            self._active_id = req_id
            cancel = self._cancel
            thread = threading.Thread(
                target=self._run_continue,
                args=(req_id, loop, list(raw_results or []), cancel),
                daemon=True,
            )
            self._thread = thread
            thread.start()
        applog.info(f"AI continue_tools started | id={req_id}")
        return {"id": req_id}

    def _run_continue(
        self,
        req_id: str,
        loop: dict[str, Any],
        results: list,
        cancel: threading.Event,
    ) -> None:
        """第二轮 stream：把 tool 结果塞回模型后续写（不带 tools）。"""
        t0 = time.perf_counter()
        first_emit_ms = -1
        base = str(loop.get("base") or "")
        key = str(loop.get("key") or "")
        model = str(loop.get("model") or "")
        first_messages = list(loop.get("messages") or [])
        assistant_text = str(loop.get("assistant_text") or "")
        tool_calls = list(loop.get("tool_calls") or [])

        cont_messages = ai_tools.build_continue_messages(
            first_messages,
            assistant_text=assistant_text,
            tool_calls=tool_calls,
            results=list(results or []),
        )

        # 流式过程中先缓冲，结束后再剥伪 tool 标记，避免半截 XML 闪到 UI
        stream_parts: list[str] = []

        def on_delta(text: str) -> None:
            if cancel.is_set():
                return
            if text:
                stream_parts.append(text)

        try:
            if applog.is_enabled("DEBUG"):
                applog.debug(
                    f"AI continue payload | id={req_id} | model={model}\n"
                    f"{_format_messages_for_log(cont_messages)}"
                )
            result = ai_client.stream_chat(
                base_url=base,
                api_key=key,
                model=model,
                messages=cont_messages,
                cancel=cancel,
                on_delta=on_delta,
                tools=None,
                tool_choice="none",
            )
            if isinstance(result, dict):
                raw_text = str(result.get("text") or "")
            else:
                raw_text = str(result or "")
            if not raw_text and stream_parts:
                raw_text = "".join(stream_parts)
            full_text = ai_tools.strip_pseudo_tool_markup(raw_text)
            total_ms = int((time.perf_counter() - t0) * 1000)
            if cancel.is_set():
                applog.info(
                    f"AI continue stopped | id={req_id} | total_ms={total_ms}"
                )
                self._emit(
                    "ai-error",
                    {
                        "id": req_id,
                        "message": self._t("已停止", "Stopped"),
                        "cancelled": True,
                        "phase": "continue",
                    },
                )
            else:
                # 一次性推送清洗后的正文（续写通常较短；避免半截 tool XML 闪现）
                if full_text:
                    first_emit_ms = int((time.perf_counter() - t0) * 1000)
                    self._emit(
                        "ai-chunk",
                        {"id": req_id, "text": full_text, "phase": "continue"},
                    )
                applog.info(
                    f"AI continue done | id={req_id} | model={model}"
                    f" | total_ms={total_ms} | first_emit_ms={first_emit_ms}"
                    f" | reply_chars={len(full_text or '')}"
                )
                if applog.is_enabled("DEBUG"):
                    applog.debug(
                        f"AI continue response | id={req_id} | model={model}\n"
                        f"{_format_response_for_log(full_text)}"
                    )
                self._emit(
                    "ai-done",
                    {
                        "id": req_id,
                        "awaiting_tools": False,
                        "phase": "continue",
                    },
                )
        except ai_client.AiClientError as exc:
            total_ms = int((time.perf_counter() - t0) * 1000)
            if exc.kind == "cancelled" or cancel.is_set():
                applog.info(
                    f"AI continue cancelled | id={req_id} | total_ms={total_ms}"
                )
                self._emit(
                    "ai-error",
                    {
                        "id": req_id,
                        "message": self._t("已停止", "Stopped"),
                        "cancelled": True,
                        "phase": "continue",
                    },
                )
            else:
                applog.warn(
                    f"AI continue failed | id={req_id} | kind={exc.kind}"
                    f" | total_ms={total_ms} | {exc.message}"
                )
                self._emit(
                    "ai-error",
                    {
                        "id": req_id,
                        "message": self._format_client_error(exc),
                        "phase": "continue",
                    },
                )
        except Exception as exc:  # noqa: BLE001
            total_ms = int((time.perf_counter() - t0) * 1000)
            applog.exception(
                f"AI continue failed | id={req_id} | total_ms={total_ms}", exc
            )
            self._emit(
                "ai-error",
                {
                    "id": req_id,
                    "message": self._t(
                        f"请求失败：{exc}",
                        f"Request failed: {exc}",
                    ),
                    "phase": "continue",
                },
            )
        finally:
            with self._lock:
                if self._active_id == req_id:
                    self._active_id = None
                self._drop_tool_loop(req_id)

    def _emit_tool_proposes(
        self,
        req_id: str,
        raw_calls: list,
        *,
        meta_path_cap: int = 0,
        default_root: str = "",
    ) -> tuple[int, list[dict]]:
        """规范化 tool_calls 并向前端推送 ``ai-tool-propose``。

        不入队、不真删。返回 ``(路径条数合计, 写入 loop 的 tool_calls 列表)``。
        """
        if not isinstance(raw_calls, list) or not raw_calls:
            return 0, []
        total_items = 0
        stored: list[dict] = []
        for i, call in enumerate(raw_calls):
            if not isinstance(call, dict):
                continue
            name = call.get("name")
            arguments = call.get("arguments")
            normalized = ai_tools.normalize_tool_call(
                name,
                arguments,
                default_root=default_root,
                max_items=meta_path_cap,
            )
            if not normalized:
                applog.debug(
                    f"AI tool_call ignored | id={req_id}"
                    f" | name={name or '-'}"
                )
                continue
            items = list(normalized.get("items") or [])
            if not items:
                continue
            tid = str(call.get("id") or "").strip() or f"call_{req_id}_{i}"
            # 第二轮需要原始 arguments 字符串
            args_raw = arguments
            if not isinstance(args_raw, str):
                import json

                try:
                    args_raw = json.dumps(
                        args_raw if args_raw is not None else {"items": items},
                        ensure_ascii=False,
                    )
                except (TypeError, ValueError):
                    import json as _json

                    args_raw = _json.dumps({"items": items}, ensure_ascii=False)
            total_items += len(items)
            stored.append(
                {
                    "id": tid,
                    "name": normalized.get("name") or ai_tools.TOOL_PROPOSE_PENDING,
                    "arguments": args_raw,
                }
            )
            self._emit(
                "ai-tool-propose",
                {
                    "id": req_id,
                    "tool_call_id": tid,
                    "name": normalized.get("name") or ai_tools.TOOL_PROPOSE_PENDING,
                    "items": items,
                },
            )
            applog.info(
                f"AI tool propose | id={req_id}"
                f" | items={len(items)} | meta_cap={meta_path_cap}"
                f" | tool_call_id={tid}"
            )
        return total_items, stored
