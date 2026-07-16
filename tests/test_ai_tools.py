"""AI tool 契约：仅提议入队，无真删 tool。"""

from __future__ import annotations

from modules.ai import tools as ai_tools


def test_allowed_tool_names_only_propose():
    assert ai_tools.TOOL_PROPOSE_PENDING in ai_tools.ALLOWED_TOOL_NAMES
    assert ai_tools.is_allowed_tool_name(ai_tools.TOOL_PROPOSE_PENDING)
    assert not ai_tools.is_allowed_tool_name("delete_path")
    assert not ai_tools.is_allowed_tool_name("execute_delete")
    assert not ai_tools.is_allowed_tool_name("")
    assert not ai_tools.is_allowed_tool_name(None)


def test_openai_tool_definitions_shape():
    defs = ai_tools.openai_tool_definitions()
    assert len(defs) == 1
    assert defs[0]["type"] == "function"
    assert defs[0]["function"]["name"] == ai_tools.TOOL_PROPOSE_PENDING
    # 描述中应强调不删除
    desc = defs[0]["function"]["description"].lower()
    assert "does not delete" in desc
    assert ai_tools.openai_tool_definitions([]) == []
    assert ai_tools.openai_tool_definitions(["nope"]) == []
    only = ai_tools.openai_tool_definitions([ai_tools.TOOL_PROPOSE_PENDING])
    assert len(only) == 1


def test_normalize_enabled_tools_and_catalog():
    assert ai_tools.TOOL_PROPOSE_PENDING in ai_tools.catalog_tool_names()
    assert ai_tools.normalize_enabled_tools(None) == []
    assert ai_tools.normalize_enabled_tools([]) == []
    assert ai_tools.normalize_enabled_tools("nope,also") == []
    assert ai_tools.normalize_enabled_tools(
        [ai_tools.TOOL_PROPOSE_PENDING, "x", ai_tools.TOOL_PROPOSE_PENDING]
    ) == [ai_tools.TOOL_PROPOSE_PENDING]
    assert ai_tools.default_enabled_tools() == ai_tools.catalog_tool_names()


def test_normalize_propose_items_and_tool_call(tmp_path):
    root = str(tmp_path)
    items = ai_tools.normalize_propose_items(
        [
            {"root": root, "rel": "cache\\x", "is_dir": True, "reason": "temp"},
            {"path": "", "rel": ""},  # drop
            "bad",
        ],
        default_root=root,
    )
    assert len(items) == 1
    assert items[0]["rel"] == "cache\\x"
    assert items[0]["is_dir"] is True

    call = ai_tools.normalize_tool_call(
        ai_tools.TOOL_PROPOSE_PENDING,
        {"items": [{"rel": "a.txt", "name": "a"}]},
        default_root=root,
    )
    assert call is not None
    assert call["name"] == ai_tools.TOOL_PROPOSE_PENDING
    assert call["items"][0]["root"] == root
    assert call["items"][0]["rel"] == "a.txt"

    assert ai_tools.normalize_tool_call("delete_path", {"items": []}) is None
    assert ai_tools.normalize_tool_call(
        ai_tools.TOOL_PROPOSE_PENDING, {"items": []}
    ) is None


def test_tool_result_for_model():
    s = ai_tools.tool_result_for_model(
        status="approved", accepted=2, rejected=1, message="ok"
    )
    assert "status=approved" in s
    assert "accepted=2" in s
    assert "do_not_call_tools_again" in s


def test_strip_pseudo_tool_markup():
    raw = (
        "### 简要说明\nok\n"
        "<tool_call>\n"
        "<function=propose_pending_delete>\n"
        '<parameter=items>[{"path":"C:\\\\x"}]</parameter>\n'
        "</function>\n"
        "</tool_call>"
    )
    cleaned = ai_tools.strip_pseudo_tool_markup(raw)
    assert "tool_call" not in cleaned.lower()
    assert "propose_pending_delete" not in cleaned
    assert "简要说明" in cleaned

    only_tool = (
        "<tool_call>\n"
        "<function=propose_pending_delete>\n"
        "<parameter=items>[1]</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )
    assert ai_tools.strip_pseudo_tool_markup(only_tool) == ""


def test_context_has_node_and_meta_count():
    assert ai_tools.context_has_node(None) is False
    assert ai_tools.context_has_node({}) is False
    assert ai_tools.context_has_node({"path": r"C:\Temp\a"}) is True
    assert ai_tools.context_has_node({"is_dir": True}) is True
    # 有节点上下文且启用 propose tool 时注入；自由聊 / 空列表不注入
    assert ai_tools.context_allows_propose_tools(None) is False
    assert ai_tools.context_allows_propose_tools({}) is False
    assert ai_tools.context_allows_propose_tools({"path": r"C:\a"}) is True
    assert (
        ai_tools.context_allows_propose_tools(
            {"path": r"C:\a", "scenario": "right_click"}
        )
        is True
    )
    assert (
        ai_tools.context_allows_propose_tools(
            {"path": r"C:\a", "scenario": "cleanup", "items": []}
        )
        is True
    )
    assert (
        ai_tools.context_allows_propose_tools(
            {"path": r"C:\a"}, tools_enabled=False
        )
        is False
    )
    assert (
        ai_tools.context_allows_propose_tools(
            {"path": r"C:\a"}, enabled_tools=[]
        )
        is False
    )
    assert (
        ai_tools.context_allows_propose_tools(
            {"path": r"C:\a"},
            enabled_tools=[ai_tools.TOOL_PROPOSE_PENDING],
        )
        is True
    )

    n = ai_tools.count_context_meta_paths(
        {
            "path": r"C:\Temp\a",
            "children": [
                {"name": "x"},
                {"name": "y"},
                "bad",
                {"name": "z"},
            ],
        }
    )
    assert n == 4  # 主项 + 3 子项

    many = [{"name": f"c{i}"} for i in range(20)]
    n2 = ai_tools.count_context_meta_paths({"path": r"C:\a", "children": many})
    assert n2 == 11  # 主项 + 最多 10 子项


def test_clamp_and_normalize_max_items(tmp_path):
    root = str(tmp_path)
    items = [{"rel": f"f{i}.txt"} for i in range(5)]
    call = ai_tools.normalize_tool_call(
        ai_tools.TOOL_PROPOSE_PENDING,
        {"items": items},
        default_root=root,
        max_items=2,
    )
    assert call is not None
    assert len(call["items"]) == 2
    assert ai_tools.clamp_propose_items(call["items"], 0) == []
    assert (
        ai_tools.normalize_tool_call(
            ai_tools.TOOL_PROPOSE_PENDING,
            {"items": items},
            default_root=root,
            max_items=0,
        )
        is None
    )


def test_default_root_from_context():
    assert (
        ai_tools.default_root_from_context({"root": r"C:\Scan"})
        == r"C:\Scan"
    )
    ctx = {
        "path": r"C:\Scan\foo\bar",
        "rel_path": r"foo\bar",
    }
    root = ai_tools.default_root_from_context(ctx)
    assert root.replace("/", "\\").lower().endswith("scan") or root.endswith("Scan")


def test_build_continue_messages():
    first = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
    ]
    calls = [
        {
            "id": "call_1",
            "name": ai_tools.TOOL_PROPOSE_PENDING,
            "arguments": '{"items":[{"rel":"a"}]}',
        }
    ]
    results = [
        {
            "tool_call_id": "call_1",
            "status": "approved",
            "accepted": 1,
            "rejected": 0,
            "message": "ok",
        }
    ]
    msgs = ai_tools.build_continue_messages(
        first,
        assistant_text="分析",
        tool_calls=calls,
        results=results,
    )
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["tool_calls"][0]["id"] == "call_1"
    assert msgs[2]["tool_calls"][0]["type"] == "function"
    assert isinstance(msgs[2]["tool_calls"][0]["function"]["arguments"], str)
    assert msgs[3]["role"] == "tool"
    assert msgs[3]["tool_call_id"] == "call_1"
    assert "status=approved" in msgs[3]["content"]
