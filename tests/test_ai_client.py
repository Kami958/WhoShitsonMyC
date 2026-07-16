"""AI 客户端 SSE 解析与流式逻辑（不发真实网络）。"""

from __future__ import annotations

import json
import threading
from unittest.mock import patch

import httpx
import pytest

from modules.ai import client as ai_client


def test_parse_sse_normal():
    lines = [
        b"data: " + json.dumps({"choices": [{"delta": {"content": "Hel"}}]}).encode()
        + b"\n",
        b"\n",
        b"data: " + json.dumps({"choices": [{"delta": {"content": "lo"}}]}).encode()
        + b"\n",
        b"data: [DONE]\n",
        b"data: " + json.dumps({"choices": [{"delta": {"content": "x"}}]}).encode()
        + b"\n",  # 不应到达
    ]
    chunks = list(ai_client.parse_sse_lines(iter(lines)))
    assert len(chunks) == 2
    texts = [ai_client.extract_delta_text(c) for c in chunks]
    assert texts == ["Hel", "lo"]


def test_parse_sse_bad_json_skipped():
    lines = [
        b"data: {not-json\n",
        b"data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]}).encode()
        + b"\n",
        b"data: [DONE]\n",
    ]
    chunks = list(ai_client.parse_sse_lines(iter(lines)))
    assert len(chunks) == 1
    assert ai_client.extract_delta_text(chunks[0]) == "ok"


def test_parse_sse_done_stops():
    lines = [
        b"data: [DONE]\n",
        b"data: " + json.dumps({"choices": [{"delta": {"content": "no"}}]}).encode()
        + b"\n",
    ]
    assert list(ai_client.parse_sse_lines(iter(lines))) == []


def test_extract_delta_empty():
    assert ai_client.extract_delta_text({}) == ""
    assert ai_client.extract_delta_text({"choices": []}) == ""
    assert ai_client.extract_delta_text({"choices": [{"delta": {}}]}) == ""


def test_base_url_needs_v1_hint():
    assert ai_client.base_url_needs_v1_hint("") is False
    assert ai_client.base_url_needs_v1_hint("   ") is False
    assert ai_client.base_url_needs_v1_hint("https://api.openai.com/v1") is False
    assert ai_client.base_url_needs_v1_hint("https://api.openai.com/v1/") is False
    assert ai_client.base_url_needs_v1_hint("https://api.openai.com") is True
    assert ai_client.base_url_needs_v1_hint("https://proxy.example/openai") is True
    # 完整端点不是 base，仍提示
    assert (
        ai_client.base_url_needs_v1_hint(
            "https://api.openai.com/v1/chat/completions"
        )
        is True
    )
    assert ai_client.base_url_needs_v1_hint("https://x.com/v1/models") is True


def test_chat_completions_url():
    assert ai_client.chat_completions_url("https://api.openai.com/v1").endswith(
        "/chat/completions"
    )
    assert (
        ai_client.chat_completions_url(
            "https://x.com/v1/chat/completions"
        )
        == "https://x.com/v1/chat/completions"
    )
    # 不以 /v1 结尾也不强制改写，仍按原样拼接
    assert (
        ai_client.chat_completions_url("https://proxy.example/openai")
        == "https://proxy.example/openai/chat/completions"
    )


def test_models_url():
    assert ai_client.models_url("https://api.openai.com/v1") == (
        "https://api.openai.com/v1/models"
    )
    assert ai_client.models_url("https://x.com/v1/models") == "https://x.com/v1/models"
    assert (
        ai_client.models_url("https://x.com/v1/chat/completions")
        == "https://x.com/v1/models"
    )


class _FakeStreamResp:
    """模拟 httpx 流式响应（context manager + iter_lines / read）。"""

    def __init__(
        self,
        *,
        status_code: int = 200,
        lines: list[str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self._lines = list(lines or [])
        self._body = body
        self._i = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True
        return False

    def iter_lines(self):
        for line in self._lines:
            yield line

    def read(self) -> bytes:
        return self._body


class _FakeGetResp:
    def __init__(self, *, status_code: int = 200, body: bytes = b"") -> None:
        self.status_code = status_code
        self.content = body
        self.text = body.decode("utf-8", errors="replace")


class _FakeClient:
    def __init__(
        self,
        stream_resp: _FakeStreamResp | None = None,
        get_resp: _FakeGetResp | None = None,
    ) -> None:
        self._stream_resp = stream_resp
        self._get_resp = get_resp
        self.closed = False
        self.stream_calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False

    def get(self, url, **kwargs):
        if self._get_resp is None:
            raise RuntimeError("no get_resp")
        return self._get_resp

    def stream(self, method, url, **kwargs):
        if self._stream_resp is None:
            raise RuntimeError("no stream_resp")
        self.stream_calls.append({"method": method, "url": url, **kwargs})
        return self._stream_resp

    def close(self):
        self.closed = True


def test_list_models_openai_style():
    body = json.dumps(
        {
            "data": [
                {"id": "gpt-4o-mini"},
                {"id": "gpt-4o"},
                {"id": "gpt-4o"},
                {"name": "only-name"},
            ]
        }
    ).encode("utf-8")
    fake = _FakeClient(get_resp=_FakeGetResp(status_code=200, body=body))
    with patch.object(ai_client, "_new_client", return_value=fake):
        ids = ai_client.list_models(
            base_url="https://example.com/v1",
            api_key="k",
        )
    assert ids == ["gpt-4o", "gpt-4o-mini", "only-name"]
    assert fake.closed is True


def test_stream_chat_fake_response():
    lines = [
        'data: {"choices":[{"delta":{"content":"A"}}]}',
        'data: {"choices":[{"delta":{"content":"B"}}]}',
        "data: [DONE]",
    ]
    fake = _FakeClient(_FakeStreamResp(status_code=200, lines=lines))
    deltas: list[str] = []
    with patch.object(ai_client, "_new_client", return_value=fake):
        result = ai_client.stream_chat(
            base_url="https://example.com/v1",
            api_key="k",
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            on_delta=deltas.append,
        )
    assert isinstance(result, dict)
    assert result["text"] == "AB"
    assert result["tool_calls"] == []
    assert deltas == ["A", "B"]
    assert fake.closed is True
    assert fake.stream_calls and fake.stream_calls[0]["method"] == "POST"


def test_stream_chat_cancel():
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ai_client.AiClientError) as ei:
        ai_client.stream_chat(
            base_url="https://example.com/v1",
            api_key="k",
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            cancel=cancel,
        )
    assert ei.value.kind == "cancelled"


def test_stream_chat_stops_at_done_without_extra_read():
    """DONE 后不得再读下一行。"""
    reads = {"n": 0}
    payload_lines = [
        'data: {"choices":[{"delta":{"content":"Hi"}}]}',
        "data: [DONE]",
        "SHOULD_NOT_BE_READ",
    ]

    class CountingResp(_FakeStreamResp):
        def iter_lines(self):
            for line in self._lines:
                reads["n"] += 1
                yield line

    fake = _FakeClient(CountingResp(status_code=200, lines=payload_lines))
    deltas: list[str] = []
    with patch.object(ai_client, "_new_client", return_value=fake):
        result = ai_client.stream_chat(
            base_url="https://example.com/v1",
            api_key="k",
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            on_delta=deltas.append,
            connect_timeout=1.0,
            read_timeout=1.0,
        )
    assert result["text"] == "Hi"
    assert result["tool_calls"] == []
    assert deltas == ["Hi"]
    # content + DONE 两行即停（for 循环在 break 前已各 yield 一次）
    assert reads["n"] == 2


def test_stream_chat_http_401():
    body = b"unauthorized"
    fake = _FakeClient(_FakeStreamResp(status_code=401, body=body))
    with patch.object(ai_client, "_new_client", return_value=fake):
        with pytest.raises(ai_client.AiClientError) as ei:
            ai_client.stream_chat(
                base_url="https://example.com/v1",
                api_key="bad",
                model="m",
                messages=[{"role": "user", "content": "hi"}],
            )
    assert ei.value.kind == "auth"


def test_stream_chat_http_429():
    fake = _FakeClient(_FakeStreamResp(status_code=429, body=b"slow"))
    with patch.object(ai_client, "_new_client", return_value=fake):
        with pytest.raises(ai_client.AiClientError) as ei:
            ai_client.stream_chat(
                base_url="https://example.com/v1",
                api_key="k",
                model="m",
                messages=[{"role": "user", "content": "hi"}],
            )
    assert ei.value.kind == "rate"


def test_stream_chat_timeout_maps_to_network():
    class BoomClient(_FakeClient):
        def stream(self, method, url, **kwargs):
            raise httpx.ConnectTimeout("connect timed out")

    fake = BoomClient(_FakeStreamResp())
    with patch.object(ai_client, "_new_client", return_value=fake):
        with pytest.raises(ai_client.AiClientError) as ei:
            ai_client.stream_chat(
                base_url="https://example.com/v1",
                api_key="k",
                model="m",
                messages=[{"role": "user", "content": "hi"}],
            )
    assert ei.value.kind == "network"


def test_merge_tool_call_delta_fragments():
    acc: dict[int, dict[str, str]] = {}
    ai_client.merge_tool_call_delta(
        acc,
        [
            {
                "index": 0,
                "id": "call_1",
                "function": {"name": "propose_pending_delete", "arguments": ""},
            }
        ],
    )
    full_args = json.dumps({"items": [{"rel": "a.txt"}]})
    mid = max(1, len(full_args) // 2)
    ai_client.merge_tool_call_delta(
        acc,
        [{"index": 0, "function": {"arguments": full_args[:mid]}}],
    )
    ai_client.merge_tool_call_delta(
        acc,
        [{"index": 0, "function": {"arguments": full_args[mid:]}}],
    )
    finalized = ai_client.finalize_tool_calls(acc)
    assert len(finalized) == 1
    assert finalized[0]["id"] == "call_1"
    assert finalized[0]["name"] == "propose_pending_delete"
    assert '"rel": "a.txt"' in finalized[0]["arguments"] or '"rel":"a.txt"' in finalized[0]["arguments"]


def test_stream_chat_tool_calls_and_tools_payload():
    """SSE tool_calls 分片拼接；请求体带 tools。"""
    tools = [{"type": "function", "function": {"name": "propose_pending_delete"}}]
    full_args = json.dumps({"items": [{"rel": "cache/x", "is_dir": True}]})
    mid = max(1, len(full_args) // 2)
    arg1 = full_args[:mid]
    arg2 = full_args[mid:]
    lines = [
        json.dumps(
            {"choices": [{"delta": {"content": "先说明一下"}}]}
        ),
        json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {
                                        "name": "propose_pending_delete",
                                        "arguments": arg1,
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        ),
        json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": arg2},
                                }
                            ]
                        }
                    }
                ]
            }
        ),
        "[DONE]",
    ]
    # parse expects "data: ..." lines
    sse_lines = [
        ("data: " + lines[0]),
        ("data: " + lines[1]),
        ("data: " + lines[2]),
        "data: [DONE]",
    ]
    fake = _FakeClient(_FakeStreamResp(status_code=200, lines=sse_lines))
    deltas: list[str] = []
    with patch.object(ai_client, "_new_client", return_value=fake):
        result = ai_client.stream_chat(
            base_url="https://example.com/v1",
            api_key="k",
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            on_delta=deltas.append,
            tools=tools,
        )
    assert result["text"] == "先说明一下"
    assert deltas == ["先说明一下"]
    assert len(result["tool_calls"]) == 1
    tc = result["tool_calls"][0]
    assert tc["name"] == "propose_pending_delete"
    assert tc["id"] == "c1"
    args = json.loads(tc["arguments"])
    assert args["items"][0]["rel"] == "cache/x"
    body = fake.stream_calls[0].get("json") or {}
    assert body.get("tools") == tools
