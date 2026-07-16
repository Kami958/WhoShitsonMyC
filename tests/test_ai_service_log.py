"""AI 服务：DEBUG 下记录实际发送 / 回复正文。"""

from __future__ import annotations

from modules.ai import service as ai_service
from modules.ai.service import (
    AiService,
    _diff_ai_config,
    _format_messages_for_log,
    _format_response_for_log,
)
import core.applog as applog


def setup_function():
    applog.clear()
    applog.set_min_level("INFO")


def teardown_function():
    applog.clear()
    applog.set_min_level(None)


def test_format_messages_for_log_roles_and_body():
    text = _format_messages_for_log(
        [
            {"role": "system", "content": "sys rule"},
            {"role": "user", "content": "hello path C:\\Temp"},
        ]
    )
    assert "#0 system" in text
    assert "sys rule" in text
    assert "#1 user" in text
    assert "hello path" in text
    # 用「N 字」而不是 chars=
    assert "字" in text
    assert "chars=" not in text


def test_format_messages_truncates_long_content():
    big = "X" * (ai_service._LOG_MSG_CONTENT_MAX + 500)
    text = _format_messages_for_log([{"role": "user", "content": big}])
    assert "…" in text
    assert len(text) < len(big)


def test_format_response_for_log():
    text = _format_response_for_log("你好，这是回复")
    assert "assistant" in text
    assert "你好，这是回复" in text
    assert "字" in text


def test_ask_logs_payload_and_response_only_at_debug(tmp_path, monkeypatch):
    """默认 INFO 不记正文；DEBUG 记 request payload 与 response。"""

    monkeypatch.setattr(
        "modules.ai.config.load",
        lambda _dir: {
            "enabled": True,
            "consented": True,
            "base_url": "https://example.com/v1",
            "model": "demo-model",
            "api_key": "sk-test",
            "extra_prompt": "",
        },
    )
    monkeypatch.setattr(
        "modules.ai.config.get_api_key",
        lambda data: str(data.get("api_key") or ""),
    )

    def fake_stream_chat(**kwargs):
        on_delta = kwargs.get("on_delta")
        reply = "这是模型完整回复正文"
        if on_delta:
            on_delta("这是")
            on_delta("模型完整回复正文")
        return {"text": reply, "tool_calls": []}

    monkeypatch.setattr("modules.ai.client.stream_chat", fake_stream_chat)

    svc = AiService(
        {
            "emit": lambda *_a, **_k: None,
            "app_data_dir": lambda: str(tmp_path),
            "t": lambda zh, en: en,
            "get_lang": lambda: "zh",
        }
    )

    applog.set_min_level("INFO")
    applog.clear()
    res = svc.ask(context={"path": r"C:\Users\Alice\foo"}, question="为什么变大？")
    assert "id" in res
    if svc._thread is not None:
        svc._thread.join(timeout=2.0)
    msgs = [e["message"] for e in applog.get_entries()]
    assert any("AI ask started" in m for m in msgs)
    assert any("AI ask done" in m for m in msgs)
    assert not any("AI ask payload" in m for m in msgs)
    assert not any("AI ask response" in m for m in msgs)

    applog.set_min_level("DEBUG")
    applog.clear()
    res2 = svc.ask(context={"path": r"C:\Users\Alice\foo"}, question="为什么变大？")
    assert "id" in res2
    if svc._thread is not None:
        svc._thread.join(timeout=2.0)
    msgs2 = [e["message"] for e in applog.get_entries()]

    payload_lines = [m for m in msgs2 if "AI ask payload" in m]
    assert payload_lines, msgs2
    body = payload_lines[0]
    assert "#0 system" in body or "system" in body
    assert "user" in body
    assert "为什么变大" in body
    assert "Alice" not in body
    assert "chars=" not in body

    response_lines = [m for m in msgs2 if "AI ask response" in m]
    assert response_lines, msgs2
    assert "这是模型完整回复正文" in response_lines[0]


def test_diff_ai_config_only_changed_fields():
    before = {
        "enabled": True,
        "base_url": "https://example.com/v1",
        "model": "m1",
        "api_key": "sk-old",
        "extra_prompt": "",
        "consented": True,
        "model_options": ["a", "b"],
    }
    same = dict(before)
    assert _diff_ai_config(before, same) == []

    after = dict(before)
    after["model"] = "m2"
    after["enabled"] = False
    parts = _diff_ai_config(before, after)
    assert any(p.startswith("model:") and "m1" in p and "m2" in p for p in parts)
    assert any(p.startswith("enabled:") and "true" in p and "false" in p for p in parts)
    assert not any(p.startswith("base_url:") for p in parts)
    # key 内容变更：set -> updated，不出现明文
    after2 = dict(before)
    after2["api_key"] = "sk-new"
    key_parts = _diff_ai_config(before, after2)
    assert key_parts == ["api_key: set -> updated"]
    after3 = dict(before)
    after3["api_key"] = ""
    assert _diff_ai_config(before, after3) == ["api_key: set -> empty"]


def test_set_config_skips_log_when_unchanged(tmp_path, monkeypatch):
    cfg = {
        "enabled": True,
        "base_url": "https://example.com/v1",
        "model": "demo",
        "api_key": "sk-test",
        "extra_prompt": "",
        "consented": True,
        "model_options": ["demo"],
    }
    monkeypatch.setattr("modules.ai.config.load", lambda _dir: dict(cfg))
    monkeypatch.setattr(
        "modules.ai.config.public_view",
        lambda data: {
            "enabled": bool(data.get("enabled")),
            "base_url": data.get("base_url") or "",
            "model": data.get("model") or "",
            "has_key": bool(str(data.get("api_key") or "").strip()),
            "extra_prompt": data.get("extra_prompt") or "",
            "consented": bool(data.get("consented")),
            "model_options": list(data.get("model_options") or []),
        },
    )
    saved = {"n": 0}

    def fake_save(_dir, data):
        saved["n"] += 1
        return data

    monkeypatch.setattr("modules.ai.config.save", fake_save)
    svc = AiService(
        {
            "emit": lambda *_a, **_k: None,
            "app_data_dir": lambda: str(tmp_path),
            "t": lambda zh, en: en,
            "get_lang": lambda: "zh",
        }
    )
    applog.clear()
    res = svc.set_config(
        {
            "enabled": True,
            "base_url": "https://example.com/v1",
            "model": "demo",
            "extra_prompt": "",
            "model_options": ["demo"],
        }
    )
    assert res.get("ok") is True
    assert res.get("unchanged") is True
    assert saved["n"] == 0
    msgs = [e["message"] for e in applog.get_entries()]
    assert not any("AI config" in m for m in msgs)

    applog.set_min_level("DEBUG")
    applog.clear()
    res2 = svc.set_config({"model": "other"})
    assert res2.get("ok") is True
    assert not res2.get("unchanged")
    assert saved["n"] == 1
    msgs2 = [e["message"] for e in applog.get_entries()]
    # 统一接口：scope=ai，默认 DEBUG
    changed = [m for m in msgs2 if "ai changed" in m]
    assert changed, msgs2
    assert "model:" in changed[0]
    assert "demo" in changed[0] and "other" in changed[0]
    assert "enabled:" not in changed[0]


def test_ask_emits_tool_propose_with_node_context(tmp_path, monkeypatch):
    """有节点上下文且启用 propose tool 时带 tools；mock tool_calls 后 emit ai-tool-propose。"""
    import json

    monkeypatch.setattr(
        "modules.ai.config.load",
        lambda _dir: {
            "enabled": True,
            "consented": True,
            "base_url": "https://example.com/v1",
            "model": "demo-model",
            "api_key": "sk-test",
            "extra_prompt": "",
            "enabled_tools": ["propose_pending_delete"],
        },
    )
    monkeypatch.setattr(
        "modules.ai.config.get_api_key",
        lambda data: str(data.get("api_key") or ""),
    )

    calls = {"n": 0, "tools": None}
    events: list[tuple] = []

    def fake_stream_chat(**kwargs):
        calls["n"] += 1
        calls["tools"] = kwargs.get("tools")
        on_delta = kwargs.get("on_delta")
        if on_delta:
            on_delta("分析完成")
        return {
            "text": "分析完成",
            "tool_calls": [
                {
                    "id": "call_x",
                    "name": "propose_pending_delete",
                    "arguments": json.dumps(
                        {
                            "items": [
                                {"rel": "cache\\tmp", "is_dir": True, "reason": "temp"},
                                {"rel": "logs\\a.log", "is_dir": False},
                            ]
                        }
                    ),
                }
            ],
        }

    monkeypatch.setattr("modules.ai.client.stream_chat", fake_stream_chat)

    svc = AiService(
        {
            "emit": lambda ev, payload: events.append((ev, payload)),
            "app_data_dir": lambda: str(tmp_path),
            "t": lambda zh, en: en,
            "get_lang": lambda: "zh",
        }
    )
    ctx = {
        "scenario": "right_click",
        "path": r"C:\Scan\foo",
        "rel_path": "foo",
        "name": "foo",
        "is_dir": True,
        "children": [
            {"name": "cache", "rel": "cache"},
            {"name": "logs", "rel": "logs"},
        ],
    }
    res = svc.ask(context=ctx, question="可以清理吗？")
    assert "id" in res
    if svc._thread is not None:
        svc._thread.join(timeout=2.0)

    assert calls["n"] == 1
    assert calls["tools"]  # 右键有节点应注入 tools
    propose_events = [e for e in events if e[0] == "ai-tool-propose"]
    assert len(propose_events) == 1
    payload = propose_events[0][1]
    assert payload.get("id") == res["id"]
    assert len(payload.get("items") or []) == 2
    assert any(e[0] == "ai-done" for e in events)


def test_ask_no_tools_when_disabled_or_freechat(tmp_path, monkeypatch):
    """enabled_tools 为空或自由聊不带 tools，即使 mock 返回 tool_calls 也不 emit propose。"""
    import json

    cfg = {
        "enabled": True,
        "consented": True,
        "base_url": "https://example.com/v1",
        "model": "demo-model",
        "api_key": "sk-test",
        "extra_prompt": "",
        "enabled_tools": [],
    }
    monkeypatch.setattr("modules.ai.config.load", lambda _dir: dict(cfg))
    monkeypatch.setattr(
        "modules.ai.config.get_api_key",
        lambda data: str(data.get("api_key") or ""),
    )

    calls = {"tools": "unset"}
    events: list[tuple] = []

    def fake_stream_chat(**kwargs):
        calls["tools"] = kwargs.get("tools")
        return {
            "text": "hi",
            "tool_calls": [
                {
                    "id": "c",
                    "name": "propose_pending_delete",
                    "arguments": json.dumps({"items": [{"rel": "x"}]}),
                }
            ],
        }

    monkeypatch.setattr("modules.ai.client.stream_chat", fake_stream_chat)
    svc = AiService(
        {
            "emit": lambda ev, payload: events.append((ev, payload)),
            "app_data_dir": lambda: str(tmp_path),
            "t": lambda zh, en: en,
            "get_lang": lambda: "en",
        }
    )
    # tools 关闭 + 有节点
    res = svc.ask(
        context={
            "scenario": "right_click",
            "path": r"C:\Scan\foo",
            "name": "foo",
            "is_dir": True,
        },
        question="删除它",
    )
    assert "id" in res
    if svc._thread is not None:
        svc._thread.join(timeout=2.0)
    assert calls["tools"] is None
    assert not any(e[0] == "ai-tool-propose" for e in events)

    # 自由聊：即使启用 tool 列表也不注入
    cfg["enabled_tools"] = ["propose_pending_delete"]
    events.clear()
    calls["tools"] = "unset"
    res2 = svc.ask(context={}, question="hello")
    assert "id" in res2
    if svc._thread is not None:
        svc._thread.join(timeout=2.0)
    assert calls["tools"] is None
    assert not any(e[0] == "ai-tool-propose" for e in events)
    done = [e[1] for e in events if e[0] == "ai-done"][-1]
    assert done.get("awaiting_tools") is False


def test_ask_awaiting_tools_and_continue(tmp_path, monkeypatch):
    """第一轮 awaiting_tools；continue_tools 触发第二次 stream（含 tool 消息）。"""
    import json

    monkeypatch.setattr(
        "modules.ai.config.load",
        lambda _dir: {
            "enabled": True,
            "consented": True,
            "base_url": "https://example.com/v1",
            "model": "demo-model",
            "api_key": "sk-test",
            "extra_prompt": "",
            "enabled_tools": ["propose_pending_delete"],
        },
    )
    monkeypatch.setattr(
        "modules.ai.config.get_api_key",
        lambda data: str(data.get("api_key") or ""),
    )

    stream_n = {"n": 0}
    last_messages = {"m": None}
    events: list[tuple] = []

    def fake_stream_chat(**kwargs):
        stream_n["n"] += 1
        last_messages["m"] = kwargs.get("messages")
        last_messages["tool_choice"] = kwargs.get("tool_choice")
        on_delta = kwargs.get("on_delta")
        if stream_n["n"] == 1:
            if on_delta:
                on_delta("先说明")
            return {
                "text": "先说明",
                "tool_calls": [
                    {
                        "id": "call_ab",
                        "name": "propose_pending_delete",
                        "arguments": json.dumps(
                            {"items": [{"rel": "tmp\\x", "is_dir": True}]}
                        ),
                    }
                ],
            }
        # 模拟部分服务商把 tool 再吐成正文；服务端应清洗
        junk = (
            "### 简要说明\n已取消\n"
            "<tool_call><function=propose_pending_delete>"
            "<parameter=items>[]</parameter></function></tool_call>"
        )
        if on_delta:
            on_delta(junk)
        return {"text": junk, "tool_calls": []}

    monkeypatch.setattr("modules.ai.client.stream_chat", fake_stream_chat)
    svc = AiService(
        {
            "emit": lambda ev, payload: events.append((ev, payload)),
            "app_data_dir": lambda: str(tmp_path),
            "t": lambda zh, en: en,
            "get_lang": lambda: "zh",
        }
    )
    ctx = {
        "scenario": "right_click",
        "path": r"C:\Scan\foo",
        "rel_path": "foo",
        "name": "foo",
        "is_dir": True,
        "children": [{"name": "tmp"}],
    }
    res = svc.ask(context=ctx, question="清理？")
    req_id = res["id"]
    if svc._thread is not None:
        svc._thread.join(timeout=2.0)

    assert stream_n["n"] == 1
    done1 = [e[1] for e in events if e[0] == "ai-done"][-1]
    assert done1.get("awaiting_tools") is True
    assert req_id in svc._pending_tool_loops

    events.clear()
    cont = svc.continue_tools(
        id=req_id,
        results=[
            {
                "tool_call_id": "call_ab",
                "status": "approved",
                "accepted": 1,
                "rejected": 0,
                "message": "用户已确认加入 1 项",
            }
        ],
    )
    assert cont.get("id") == req_id
    if svc._thread is not None:
        svc._thread.join(timeout=2.0)

    assert stream_n["n"] == 2
    assert last_messages.get("tool_choice") == "none"
    msgs = last_messages["m"] or []
    roles = [m.get("role") for m in msgs]
    assert "tool" in roles
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in msgs)
    tool_msg = next(m for m in msgs if m.get("role") == "tool")
    assert tool_msg.get("tool_call_id") == "call_ab"
    assert "status=approved" in tool_msg.get("content", "")
    chunks = [e[1] for e in events if e[0] == "ai-chunk"]
    assert chunks, events
    assert "tool_call" not in (chunks[-1].get("text") or "").lower()
    assert "已取消" in (chunks[-1].get("text") or "")
    done2 = [e[1] for e in events if e[0] == "ai-done"][-1]
    assert done2.get("awaiting_tools") is False
    assert done2.get("phase") == "continue"
    assert req_id not in svc._pending_tool_loops


def test_continue_tools_unknown_id(tmp_path):
    svc = AiService(
        {
            "emit": lambda *_a, **_k: None,
            "app_data_dir": lambda: str(tmp_path),
            "t": lambda zh, en: en,
            "get_lang": lambda: "en",
        }
    )
    res = svc.continue_tools(id="nope", results=[])
    assert res.get("error")
