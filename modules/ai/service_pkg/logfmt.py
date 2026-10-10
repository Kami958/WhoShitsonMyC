"""AI 请求日志与配置 diff 的格式化：模型选项归一、字段脱敏、正文落日志。"""

from __future__ import annotations

from modules.ai import config as ai_config
from modules.ai import tools as ai_tools
from typing import Any

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
