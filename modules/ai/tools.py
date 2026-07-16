"""AI tool 契约（安全边界 + 规范化）。

本模块**只**定义允许暴露给模型的 tool 名称、参数形状与归一化结果。
不负责：

- 拼装发送给模型的快照 / diff meta（策略未定，故意留空）
- 调用 ``delete_path`` 或任何真删
- 静默写入待删除列表

加入待删除的唯一安全路径：

1. 模型产出 ``propose_pending_delete`` 提议
2. 前端页面勾选弹窗 → **用户手工同意**
3. AI 确认路径 **直接入队**（不在此步跑白名单预检）
4. 真删仍走用户确认后的 ``delete_path``（白名单在删除时生效）

用户审批结果经 ``tool_result_for_model`` 回传模型后，可发起第二轮续写。

对比树右键问 AI：一层子项（RIGHT_CLICK_MAX_CHILDREN）。
清理场景：packing.pack_cleanup_slice 产出本批 items + has_more/deferred；
meta 条数按本批实际路径计，不与右键「每层 10」共用。
"""

from __future__ import annotations

from typing import Any

# 允许的 tool 名。禁止 delete / execute / unlink 等真删 tool。
TOOL_PROPOSE_PENDING = "propose_pending_delete"

ALLOWED_TOOL_NAMES = frozenset({TOOL_PROPOSE_PENDING})

# 设置页可勾选的 tool 目录（顺序固定；仅登记在此的才可启用）
CATALOG_TOOLS: tuple[dict[str, str], ...] = (
    {
        "name": TOOL_PROPOSE_PENDING,
        "label_zh": "申请加入待删除",
        "label_en": "Propose pending delete",
        "desc_zh": "向软件申请把路径加入待删除列表，须你勾选确认后才会入队",
        "desc_en": "Ask the app to add paths to pending delete; you still confirm first",
    },
)

# 单次提议条数上限（归一化时截断）
_MAX_PROPOSE_ITEMS = 50
_MAX_PATH_CHARS = 512
_MAX_REASON_CHARS = 200


def catalog_tool_names() -> list[str]:
    """设置页可选 tool 名（稳定顺序）。"""
    return [str(t["name"]) for t in CATALOG_TOOLS]


def normalize_enabled_tools(raw: Any) -> list[str]:
    """收成合法已启用 tool 名列表；未知名丢弃，保序去重。"""
    allowed = set(catalog_tool_names())
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [p.strip() for p in raw.replace(";", ",").split(",")]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = list(raw)
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = str(item or "").strip()
        if not name or name not in allowed or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def default_enabled_tools() -> list[str]:
    """默认启用全部目录内 tool。"""
    return catalog_tool_names()


def openai_tool_definitions(
    enabled_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    """OpenAI 兼容 ``tools`` 数组（function calling）。

    仅输出用户启用且目录内登记的 tool；执行与入队不在模型侧。
    """
    if enabled_names is None:
        enabled = set(default_enabled_tools())
    else:
        enabled = set(normalize_enabled_tools(enabled_names))
    if not enabled:
        return []

    defs: list[dict[str, Any]] = []
    if TOOL_PROPOSE_PENDING in enabled:
        defs.append(
            {
                "type": "function",
                "function": {
                    "name": TOOL_PROPOSE_PENDING,
                    "description": (
                        "Propose paths to add to the app's pending-delete list. "
                        "Use only when the user clearly wants cleanup / enqueue candidates; "
                        "do not call for pure analysis. "
                        "Does NOT delete anything and does not enqueue by itself. "
                        "The user must approve in the UI; after they confirm or cancel, "
                        "a tool result is returned and you must continue with a normal "
                        "markdown reply only (no further tool calls)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "items": {
                                "type": "array",
                                "description": "Candidate paths (compare-tree relative or with root).",
                                "maxItems": _MAX_PROPOSE_ITEMS,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "root": {
                                            "type": "string",
                                            "description": "Compare/scan root absolute path when known.",
                                        },
                                        "rel": {
                                            "type": "string",
                                            "description": "Path relative to root (preferred).",
                                        },
                                        "path": {
                                            "type": "string",
                                            "description": "Absolute path if rel unknown.",
                                        },
                                        "name": {
                                            "type": "string",
                                            "description": "Display name.",
                                        },
                                        "is_dir": {
                                            "type": "boolean",
                                            "description": "True if directory.",
                                        },
                                        "reason": {
                                            "type": "string",
                                            "description": (
                                                "Brief reason for the user (required when possible): "
                                                "what the path likely is and why it may be safe or risky "
                                                "to delete; ≤50 chars; same language as the reply."
                                            ),
                                        },
                                    },
                                },
                            }
                        },
                        "required": ["items"],
                    },
                },
            }
        )
    return defs


def is_allowed_tool_name(name: str | None) -> bool:
    """是否为白名单 tool（真删类名称一律 False）。"""
    n = str(name or "").strip()
    if not n:
        return False
    # 硬拒绝危险名，即使将来误登记
    lowered = n.lower()
    for bad in (
        "delete",
        "unlink",
        "rmtree",
        "remove_path",
        "execute_delete",
        "force_delete",
    ):
        if bad in lowered and n not in ALLOWED_TOOL_NAMES:
            return False
    return n in ALLOWED_TOOL_NAMES


def _clip(s: str, limit: int) -> str:
    text = str(s or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def normalize_propose_items(
    raw_items: Any,
    *,
    default_root: str = "",
) -> list[dict[str, Any]]:
    """把 tool 参数里的 items 收成前端 / check_pending_paths 可用结构。

    无效项丢弃；条数截断。不查盘、不过白名单（那是 check_pending_paths 的事）。
    """
    if not isinstance(raw_items, list):
        return []
    root_default = str(default_root or "").strip()
    out: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        root = str(item.get("root") or root_default).strip()
        rel = str(item.get("rel") or item.get("rel_path") or "").strip()
        path = str(item.get("path") or item.get("full") or "").strip()
        name = str(item.get("name") or "").strip()
        reason = _clip(str(item.get("reason") or "").strip(), _MAX_REASON_CHARS)
        if not rel and path and root and path.lower().startswith(root.lower().rstrip("\\/") ):
            # 尽力从绝对路径剥相对段（不保证；前端仍可只用 path 展示）
            try:
                import os

                rel_try = os.path.relpath(path, root)
                if rel_try and not rel_try.startswith(".."):
                    rel = rel_try
            except (OSError, ValueError):
                pass
        # 至少要有相对路径或绝对路径；仅 root 不足以成项
        if not rel and not path:
            continue
        if len(root) > _MAX_PATH_CHARS or len(rel) > _MAX_PATH_CHARS or len(path) > _MAX_PATH_CHARS:
            continue
        row: dict[str, Any] = {
            "root": root,
            "rel": rel,
            "name": name or (rel or path or root),
            "is_dir": bool(item.get("is_dir")),
        }
        if path:
            row["path"] = path
        if reason:
            row["reason"] = reason
        out.append(row)
        if len(out) >= _MAX_PROPOSE_ITEMS:
            break
    return out


def context_has_node(context: dict[str, Any] | None) -> bool:
    """本轮是否带有对比树节点上下文。"""
    ctx = dict(context or {})
    return bool(
        ctx.get("path")
        or ctx.get("rel_path")
        or ctx.get("name")
        or ("old_size" in ctx)
        or ("new_size" in ctx)
        or ("is_dir" in ctx)
        or ctx.get("children")
        or ctx.get("top_children")
        or ctx.get("items")
    )


def context_allows_propose_tools(
    context: dict[str, Any] | None,
    *,
    enabled_tools: list[str] | None = None,
    tools_enabled: bool | None = None,
) -> bool:
    """是否注入 propose_pending_delete。

    - ``enabled_tools`` 未包含该 tool 时不注入
    - 兼容旧参数 ``tools_enabled=False``（等价于空列表）
    - 自由聊（无节点上下文）不注入
    - 有节点上下文时（右键问 AI / cleanup）可注入；仍须用户勾选后才入队
    """
    if tools_enabled is False:
        return False
    names = normalize_enabled_tools(
        enabled_tools if enabled_tools is not None else default_enabled_tools()
    )
    if TOOL_PROPOSE_PENDING not in names:
        return False
    return context_has_node(context)


def _count_list_paths(rows: Any, *, cap: int | None) -> int:
    if not isinstance(rows, list):
        return 0
    n = 0
    for ch in rows:
        if isinstance(ch, dict) and (
            ch.get("name") or ch.get("path") or ch.get("rel") or ch.get("rel_path")
        ):
            n += 1
        if cap is not None and n >= cap:
            break
    return n


def count_context_meta_paths(context: dict[str, Any] | None) -> int:
    """本轮发给模型的路径 meta 条数。

    - right_click / 默认：主项 1 + children（最多 10）
    - cleanup 且带 items：按 items 实际条数计（可 >10；有 paths_in_slice 时以其为准上限参考）
    """
    ctx = dict(context or {})
    scenario = str(ctx.get("scenario") or "").strip().lower()
    items = ctx.get("items")
    if scenario == "cleanup" and isinstance(items, list):
        n = _count_list_paths(items, cap=None)
        # 安全阀：与 packing 单批预算同量级
        if n > 80:
            return 80
        return n

    n = 0
    if ctx.get("path") or ctx.get("rel_path") or ctx.get("name"):
        n += 1
    elif (
        "old_size" in ctx
        or "new_size" in ctx
        or "is_dir" in ctx
        or ctx.get("kind")
    ):
        n += 1
    children = ctx.get("children") or ctx.get("top_children") or []
    # 右键语义：子项最多计 10
    n += _count_list_paths(children, cap=10)
    return n


def default_root_from_context(context: dict[str, Any] | None) -> str:
    """尽力从节点 context 推出扫描根（完整 path 去掉 rel_path）。"""
    import os

    ctx = dict(context or {})
    root = str(ctx.get("root") or ctx.get("scan_root") or "").strip()
    if root:
        return root
    path = str(ctx.get("path") or "").strip()
    rel = str(ctx.get("rel_path") or ctx.get("rel") or "").strip()
    if path and rel:
        # path 以 rel 结尾时剥掉
        norm_path = path.replace("/", os.sep).rstrip("\\/")
        norm_rel = rel.replace("/", os.sep).strip("\\/")
        if norm_rel and norm_path.lower().endswith(norm_rel.lower()):
            base = norm_path[: len(norm_path) - len(norm_rel)].rstrip("\\/")
            if base:
                return base
    return ""


def clamp_propose_items(
    items: list[dict[str, Any]] | None,
    max_n: int,
) -> list[dict[str, Any]]:
    """按产品上限截断提议列表；max_n<=0 表示不允许任何项。"""
    if not items:
        return []
    try:
        limit = int(max_n)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        return []
    if limit > _MAX_PROPOSE_ITEMS:
        limit = _MAX_PROPOSE_ITEMS
    return list(items)[:limit]


def normalize_tool_call(
    name: str | None,
    arguments: Any,
    *,
    default_root: str = "",
    max_items: int | None = None,
) -> dict[str, Any] | None:
    """规范化一次 tool 调用；非法名或空结果返回 None。

    成功时::

        {
          "name": "propose_pending_delete",
          "items": [ {root, rel, name, is_dir, reason?, path?}, ... ]
        }
    """
    if not is_allowed_tool_name(name):
        return None
    args = arguments
    if isinstance(args, str):
        import json

        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
    if not isinstance(args, dict):
        return None
    if str(name).strip() != TOOL_PROPOSE_PENDING:
        return None
    items = normalize_propose_items(
        args.get("items"),
        default_root=default_root,
    )
    if max_items is not None:
        items = clamp_propose_items(items, max_items)
    if not items:
        return None
    return {"name": TOOL_PROPOSE_PENDING, "items": items}


def tool_result_for_model(
    *,
    status: str,
    accepted: int = 0,
    rejected: int = 0,
    message: str = "",
) -> str:
    """写回给模型的 tool 结果摘要（用户同意/拒绝后由服务填）。

    status: proposed | approved | cancelled | filtered_empty | error
    """
    parts = [f"status={status}", f"accepted={int(accepted)}", f"rejected={int(rejected)}"]
    if message:
        parts.append(f"message={_clip(message, 300)}")
    # 明确下一步，降低续写阶段再吐 tool_call 伪标记的概率
    parts.append(
        "next=reply_user_in_response_format_only;"
        "do_not_call_tools_again;"
        "do_not_output_tool_call_markup"
    )
    return "; ".join(parts)


def strip_pseudo_tool_markup(text: str | None) -> str:
    """去掉模型误当作正文输出的 tool_call / function 伪标记。

    部分兼容接口在第二轮不带 tools 时仍会把 function call 写成 XML/文本。
    """
    import re

    s = str(text or "")
    if not s:
        return ""
    # 成对标签
    s = re.sub(r"<tool_call\b[^>]*>[\s\S]*?</tool_call\s*>", "", s, flags=re.I)
    s = re.sub(r"<function\b[^>]*>[\s\S]*?</function\s*>", "", s, flags=re.I)
    s = re.sub(r"<parameter\b[^>]*>[\s\S]*?</parameter\s*>", "", s, flags=re.I)
    # 未闭合残留
    s = re.sub(r"<tool_call\b[\s\S]*$", "", s, flags=re.I)
    s = re.sub(r"<function\s*=[\s\S]*$", "", s, flags=re.I)
    # 常见独立标记行
    s = re.sub(r"</?\s*tool_call\s*>", "", s, flags=re.I)
    s = re.sub(r"</?\s*function\b[^>]*>", "", s, flags=re.I)
    # 压缩多余空行
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def openai_assistant_tool_message(
    text: str,
    tool_calls: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """拼装带 tool_calls 的 assistant 消息（第二轮请求用）。

    ``tool_calls`` 项至少含 id / name / arguments（arguments 为 JSON 字符串）。
    """
    calls_out: list[dict[str, Any]] = []
    for i, call in enumerate(tool_calls or []):
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or "").strip()
        if not name:
            continue
        tid = str(call.get("id") or "").strip() or f"call_{i}"
        args = call.get("arguments")
        if not isinstance(args, str):
            import json

            try:
                args = json.dumps(args if args is not None else {}, ensure_ascii=False)
            except (TypeError, ValueError):
                args = "{}"
        calls_out.append(
            {
                "id": tid,
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        )
    msg: dict[str, Any] = {
        "role": "assistant",
        "content": text if text else None,
    }
    if calls_out:
        msg["tool_calls"] = calls_out
    return msg


def openai_tool_message(tool_call_id: str, content: str) -> dict[str, Any]:
    """拼装 role=tool 消息。"""
    return {
        "role": "tool",
        "tool_call_id": str(tool_call_id or "").strip() or "call_0",
        "content": str(content or ""),
    }


def build_continue_messages(
    first_messages: list[dict[str, Any]] | None,
    *,
    assistant_text: str,
    tool_calls: list[dict[str, Any]] | None,
    results: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """第一轮 messages + assistant(tool_calls) + tool 结果 → 第二轮请求体。"""
    out: list[dict[str, Any]] = []
    for m in first_messages or []:
        if isinstance(m, dict):
            out.append(dict(m))
    # 规范化 tool_calls id
    norm_calls: list[dict[str, Any]] = []
    for i, call in enumerate(tool_calls or []):
        if not isinstance(call, dict):
            continue
        c = dict(call)
        if not str(c.get("id") or "").strip():
            c["id"] = f"call_{i}"
        norm_calls.append(c)
    out.append(openai_assistant_tool_message(assistant_text, norm_calls))

    by_id: dict[str, dict[str, Any]] = {}
    for r in results or []:
        if not isinstance(r, dict):
            continue
        tid = str(r.get("tool_call_id") or "").strip()
        if tid:
            by_id[tid] = r

    for i, call in enumerate(norm_calls):
        tid = str(call.get("id") or f"call_{i}")
        r = by_id.get(tid) or {}
        status = str(r.get("status") or "cancelled").strip() or "cancelled"
        try:
            accepted = int(r.get("accepted") or 0)
        except (TypeError, ValueError):
            accepted = 0
        try:
            rejected = int(r.get("rejected") or 0)
        except (TypeError, ValueError):
            rejected = 0
        message = str(r.get("message") or "")
        content = tool_result_for_model(
            status=status,
            accepted=accepted,
            rejected=rejected,
            message=message,
        )
        out.append(openai_tool_message(tid, content))
    return out
