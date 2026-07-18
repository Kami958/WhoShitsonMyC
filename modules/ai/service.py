"""AI 服务：对前端暴露的方法集合。

经 Api.module_invoke 按 PUBLIC_METHODS 白名单调用。
配置落在 settings.yaml 的 ai: 节（经 modules.ai.config → store）。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

from core import applog
from modules.ai import client as ai_client
from modules.ai import config as ai_config
from modules.ai import packing as ai_packing
from modules.ai import prompts as ai_prompts
from modules.ai import tools as ai_tools


PUBLIC_METHODS = frozenset(
    {
        "get_config",
        "set_config",
        "list_models",
        "test_connection",
        "ask",
        "continue_tools",
        "cancel",
        "reset",
        # 对比树清理：有限递归多切片（不真删；context 交前端再 ask）
        "start_compare_cleanup",
        "next_compare_cleanup",
        "cancel_compare_cleanup",
    }
)

# DEBUG 下 dump 实际发给模型的 messages / 模型回复（全文，不截断）。
# 路径仍走 applog.sanitize；默认 INFO 门槛下不会进日志。

# 配置 diff 日志字段顺序（api_key 只记有无，不记明文）
_AI_DIFF_FIELDS = (
    "enabled",
    "base_url",
    "model",
    "extra_prompt",
    "consented",
    "model_options",
    "cleanup_max_depth",
    "enabled_tools",
    "api_key",
)


def _norm_model_options(opts: Any) -> list[str]:
    if not isinstance(opts, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in opts:
        s = str(item or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _ai_field_log_value(
    key: str,
    data: dict[str, Any],
    *,
    role: str = "value",
    other: dict[str, Any] | None = None,
) -> str:
    """把配置字段格式化为日志可读串（key 只显示有/无/更新，不记明文）。

    ``role`` 为 ``before`` / ``after`` 时用于 api_key 差分：
    before 侧只记 set/empty；after 侧在内容变更时记 updated。
    """
    if key == "api_key":
        cur = str(data.get("api_key") or "").strip()
        if not cur:
            return "empty"
        if role == "after" and other is not None:
            prev = str(other.get("api_key") or "").strip()
            if prev and prev != cur:
                return "updated"
        return "set"
    if key == "model_options":
        opts = _norm_model_options(data.get("model_options"))
        if not opts:
            return "[]"
        # 列表过长只记数量，避免刷屏
        if len(opts) > 8:
            return f"[{len(opts)} models]"
        return "[" + ", ".join(opts) + "]"
    if key == "enabled_tools":
        names = ai_tools.normalize_enabled_tools(data.get("enabled_tools"))
        if not names:
            return "[]"
        return "[" + ", ".join(names) + "]"
    if key in ("enabled", "consented"):
        return "true" if bool(data.get(key)) else "false"
    if key == "cleanup_max_depth":
        try:
            return str(int(data.get("cleanup_max_depth")))
        except (TypeError, ValueError):
            return str(ai_config._DEFAULTS.get("cleanup_max_depth", 3))
    if key == "extra_prompt":
        text = str(data.get("extra_prompt") or "")
        if not text:
            return '""'
        one = text.replace("\r", "\\r").replace("\n", "\\n")
        if len(one) > 80:
            one = one[:79] + "…"
        return f'"{one}"'
    val = str(data.get(key) or "").strip()
    return val if val else "-"


def _diff_ai_config(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """返回已变更字段的「名: 旧 → 新」片段；无变更则空列表。"""
    parts: list[str] = []
    for key in _AI_DIFF_FIELDS:
        if key == "api_key":
            b = str(before.get("api_key") or "").strip()
            a = str(after.get("api_key") or "").strip()
            if b == a:
                continue
        elif key == "model_options":
            if _norm_model_options(before.get("model_options")) == _norm_model_options(
                after.get("model_options")
            ):
                continue
        elif key == "enabled_tools":
            b = ai_tools.normalize_enabled_tools(before.get("enabled_tools"))
            a = ai_tools.normalize_enabled_tools(after.get("enabled_tools"))
            if b == a:
                continue
        elif key in ("enabled", "consented"):
            b = bool(before.get(key, False))
            a = bool(after.get(key, False))
            if b == a:
                continue
        elif key == "cleanup_max_depth":
            try:
                b = int(before.get("cleanup_max_depth"))
            except (TypeError, ValueError):
                b = None
            try:
                a = int(after.get("cleanup_max_depth"))
            except (TypeError, ValueError):
                a = None
            if b == a:
                continue
        else:
            b = str(before.get(key) or "").strip()
            a = str(after.get(key) or "").strip()
            if b == a:
                continue
        parts.append(
            f"{key}: {_ai_field_log_value(key, before, role='before', other=after)}"
            f" -> {_ai_field_log_value(key, after, role='after', other=before)}"
        )
    return parts


def _format_messages_for_log(messages: list[dict] | None) -> str:
    """把 messages 压成可读多行文本（仅调试用，全文不截断）。

    格式示例::

        #0 system · 213 字
        （正文）
        ---
        #1 user · 42 字
        （正文）
    """
    lines: list[str] = []
    for i, item in enumerate(messages or []):
        if not isinstance(item, dict):
            lines.append(f"#{i} <非字典消息>")
            continue
        role = str(item.get("role") or "?")
        content = item.get("content")
        if content is None:
            raw = ""
        elif isinstance(content, str):
            raw = content
        else:
            raw = str(content)
        # 「字」= Python 字符串长度（字符数），不是 token
        lines.append(f"#{i} {role} · {len(raw)} 字\n{raw}")
    return "\n---\n".join(lines) if lines else "(empty)"


def _format_response_for_log(text: str | None) -> str:
    """模型完整回复的调试文本（全文不截断）。"""
    raw = text if isinstance(text, str) else str(text or "")
    return f"assistant · {len(raw)} 字\n{raw}"


class AiService:
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

    # ---- 配置 --------------------------------------------------------------

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

    # ---- 对比树清理多切片（packing）-----------------------------------------

    def start_compare_cleanup(
        self,
        old_path: str = "",
        new_path: str = "",
        seed: dict | None = None,
        root: str = "",
        **kwargs: Any,
    ) -> dict:
        """从对比树 seed（默认根）开始清理 packing，返回第一批 context。

        不调用模型、不删除。前端把 ``context`` 交给 ``ask``。
        入口：对比树右键；磁盘清理另入口（本方法不负责）。
        """
        old = str(old_path or kwargs.get("old_path") or "").strip()
        new = str(new_path or kwargs.get("new_path") or "").strip()
        root_s = str(root or kwargs.get("root") or "").strip()
        raw_seed = seed if seed is not None else kwargs.get("seed")
        if not old or not new:
            return {
                "error": self._t(
                    "缺少对比快照路径",
                    "Missing compare snapshot paths",
                )
            }
        if not callable(self._get_diff_children):
            return {
                "error": self._t(
                    "对比子项接口不可用",
                    "Compare children API unavailable",
                )
            }

        seed_node: dict[str, Any]
        if isinstance(raw_seed, dict) and (
            raw_seed.get("path")
            or raw_seed.get("rel")
            or raw_seed.get("rel_path")
            or raw_seed.get("name")
            or "is_dir" in raw_seed
        ):
            seed_node = dict(raw_seed)
            # 统一 rel：对比树 node.path 即相对根
            if not seed_node.get("rel") and not seed_node.get("rel_path"):
                rel = str(seed_node.get("path") or "").strip()
                # 若 path 是绝对路径且带 root，尽量剥 rel
                if root_s and rel.lower().startswith(root_s.lower().rstrip("\\/")):
                    try:
                        import os

                        rel = os.path.relpath(rel, root_s)
                        if rel.startswith(".."):
                            rel = str(seed_node.get("path") or "")
                    except (OSError, ValueError):
                        pass
                # 前端常传 rel_path 在 path 字段（相对）
                if "\\" in rel or "/" in rel or rel:
                    # 若仍像绝对盘符，保留空 rel 表示根
                    if len(rel) >= 2 and rel[1] == ":":
                        seed_node["rel"] = ""
                        seed_node["rel_path"] = ""
                    else:
                        seed_node["rel"] = rel.replace("/", "\\")
                        seed_node["rel_path"] = seed_node["rel"]
            else:
                r = str(
                    seed_node.get("rel") or seed_node.get("rel_path") or ""
                ).replace("/", "\\")
                seed_node["rel"] = r
                seed_node["rel_path"] = r
            if "is_dir" not in seed_node:
                seed_node["is_dir"] = True
        else:
            seed_node = {
                "rel": "",
                "rel_path": "",
                "path": root_s or "",
                "name": root_s or "root",
                "is_dir": True,
                "new_size": 0,
                "old_size": 0,
                "delta": 0,
                "kind": "",
            }

        get_children = self._make_diff_get_children(old, new)
        cfg = ai_config.load(self._app_data_dir())
        max_depth = int(cfg.get("cleanup_max_depth") or ai_packing.CLEANUP_MAX_DEPTH)
        try:
            job = ai_packing.start_cleanup_job(
                seed_node, get_children, root=root_s
            )
            packed = ai_packing.pack_cleanup_slice(
                job, get_children, max_depth=max_depth
            )
        except Exception as exc:  # noqa: BLE001
            applog.exception("AI start_compare_cleanup failed", exc)
            return {
                "error": self._t(
                    f"清理上下文打包失败：{exc}",
                    f"Failed to pack cleanup context: {exc}",
                )
            }

        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            # 单任务简化：新开清理时清掉旧 job，避免堆积
            self._cleanup_jobs.clear()
            self._cleanup_jobs[job_id] = {
                "job": job,
                "old_path": old,
                "new_path": new,
                "root": root_s,
            }
        applog.info(
            f"AI compare cleanup started | job={job_id}"
            f" | slice={packed.get('slice')} | paths={packed.get('paths_in_slice')}"
            f" | has_more={packed.get('has_more')}"
        )
        return self._cleanup_slice_response(job_id, packed)

    def next_compare_cleanup(self, job_id: str = "", **kwargs: Any) -> dict:
        """同一清理任务的下一批 context；无更多则 has_more=false。"""
        jid = str(job_id or kwargs.get("job_id") or "").strip()
        if not jid:
            return {
                "error": self._t("缺少任务 id", "Missing job id"),
            }
        with self._lock:
            slot = self._cleanup_jobs.get(jid)
            if not slot:
                return {
                    "error": self._t(
                        "没有进行中的清理任务，或已结束",
                        "No active cleanup job (ended or unknown)",
                    )
                }
            job = slot["job"]
            old = str(slot.get("old_path") or "")
            new = str(slot.get("new_path") or "")

        if not getattr(job, "pending", None):
            packed = {
                "scenario": ai_packing.SCENARIO_CLEANUP,
                "slice": getattr(job, "slice_index", 0),
                "has_more": False,
                "paths_in_slice": 0,
                "items": [],
                "deferred_top": [],
                "children": [],
            }
            seed = getattr(job, "seed", None) or {}
            if isinstance(seed, dict):
                for key in (
                    "path",
                    "rel",
                    "rel_path",
                    "name",
                    "is_dir",
                    "kind",
                    "old_size",
                    "new_size",
                    "delta",
                ):
                    if key in seed:
                        packed[key] = seed[key]
            with self._lock:
                self._cleanup_jobs.pop(jid, None)
            return self._cleanup_slice_response(jid, packed)

        get_children = self._make_diff_get_children(old, new)
        cfg = ai_config.load(self._app_data_dir())
        max_depth = int(cfg.get("cleanup_max_depth") or ai_packing.CLEANUP_MAX_DEPTH)
        try:
            packed = ai_packing.pack_cleanup_slice(
                job, get_children, max_depth=max_depth
            )
        except Exception as exc:  # noqa: BLE001
            applog.exception("AI next_compare_cleanup failed", exc)
            return {
                "error": self._t(
                    f"下一批打包失败：{exc}",
                    f"Failed to pack next slice: {exc}",
                )
            }

        if not packed.get("has_more"):
            with self._lock:
                self._cleanup_jobs.pop(jid, None)
        applog.info(
            f"AI compare cleanup slice | job={jid}"
            f" | slice={packed.get('slice')} | paths={packed.get('paths_in_slice')}"
            f" | has_more={packed.get('has_more')}"
        )
        return self._cleanup_slice_response(jid, packed)

    def cancel_compare_cleanup(self, job_id: str = "", **kwargs: Any) -> dict:
        """丢弃清理任务（可选 id；空则清空全部）。"""
        jid = str(job_id or kwargs.get("job_id") or "").strip()
        with self._lock:
            if not jid:
                n = len(self._cleanup_jobs)
                self._cleanup_jobs.clear()
                return {"ok": True, "cancelled": n}
            if jid in self._cleanup_jobs:
                self._cleanup_jobs.pop(jid, None)
                return {"ok": True, "cancelled": 1}
        return {"ok": True, "cancelled": 0}

    # ---- 模型列表 ----------------------------------------------------------

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

    # ---- 对话 --------------------------------------------------------------

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
