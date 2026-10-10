"""AiService 基类：上下文注入、工具循环登记、清理作业去重与错误文案。"""

from __future__ import annotations

from core import applog
from modules.ai import client as ai_client
from typing import Any
from typing import Callable
import threading

from .public import PUBLIC_METHODS

class AiServiceBase:
    """AI 模块运行时实例。"""

    PUBLIC_METHODS = PUBLIC_METHODS

    def __init__(self, ctx: dict) -> None:
        self._emit: Callable[[str, dict], None] = ctx.get("emit") or (lambda *_a, **_k: None)
        self._app_data_dir: Callable[[], str] = ctx.get("app_data_dir") or (lambda: "")
        self._t: Callable[[str, str], str] = ctx.get("t") or (lambda zh, en: en)
        self._get_lang: Callable[[], str] = ctx.get("get_lang") or (lambda: "en")
        # (old_path, new_path, parent_rel) -> list[dict]；对比树 get_children
        self._get_diff_children: Callable[[str, str, str], list] | None = (
            ctx.get("get_diff_children")
        )

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._active_id: str | None = None
        # 等人审 tool 的会话快照（不阻塞线程）；key=req_id
        self._pending_tool_loops: dict[str, dict[str, Any]] = {}
        # 对比树清理多切片任务；key=job_id
        self._cleanup_jobs: dict[str, dict[str, Any]] = {}

    def _clear_tool_loops(self) -> None:
        self._pending_tool_loops.clear()

    def _drop_tool_loop(self, req_id: str | None) -> None:
        if not req_id:
            return
        self._pending_tool_loops.pop(str(req_id), None)

    def _clear_cleanup_jobs(self) -> None:
        self._cleanup_jobs.clear()

    def _make_diff_get_children(
        self, old_path: str, new_path: str
    ) -> Callable[[str], list[dict[str, Any]]]:
        """闭包：packing 只需 parent_rel。"""

        def get_children(parent_rel: str) -> list[dict[str, Any]]:
            fn = self._get_diff_children
            if not callable(fn):
                return []
            try:
                raw = fn(str(old_path or ""), str(new_path or ""), str(parent_rel or ""))
            except Exception as exc:  # noqa: BLE001
                applog.warn(f"AI cleanup get_children failed | {exc}")
                return []
            if not isinstance(raw, list):
                return []
            return [x for x in raw if isinstance(x, dict)]

        return get_children

    def _cleanup_slice_response(
        self, job_id: str, packed: dict[str, Any]
    ) -> dict[str, Any]:
        """统一返回：context 可直接塞进 ask。"""
        has_more = bool(packed.get("has_more"))
        return {
            "ok": True,
            "job_id": job_id,
            "context": packed,
            "has_more": has_more,
            "slice": packed.get("slice"),
            "paths_in_slice": packed.get("paths_in_slice"),
            "deferred_top": list(packed.get("deferred_top") or []),
        }

    def _format_client_error(self, exc: ai_client.AiClientError) -> str:
        kind = exc.kind
        detail = " ".join(str(exc.message or "").split())
        if len(detail) > 180:
            detail = detail[:180] + "…"
        if kind == "auth":
            base = self._t(
                "API Key 无效或无权限",
                "Invalid API key or unauthorized",
            )
            return f"{base}：{detail}" if detail else base
        if kind == "rate":
            base = self._t(
                "请求过于频繁，请稍后再试",
                "Rate limited — try again later",
            )
            return f"{base}：{detail}" if detail else base
        if kind == "network":
            return self._t(
                f"网络错误：{detail or exc.message}",
                f"Network error: {detail or exc.message}",
            )
        if kind == "cancelled":
            return self._t("已停止", "Stopped")
        if kind == "format":
            return self._t(
                f"服务响应异常：{detail or exc.message}",
                f"Bad response: {detail or exc.message}",
            )
        return self._t(
            f"请求失败：{detail or exc.message}",
            f"Request failed: {detail or exc.message}",
        )
