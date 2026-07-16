"""OpenAI 兼容 chat/completions 客户端（httpx + SSE 流式）。

使用 httpx 走系统/环境代理；支持取消与错误分类。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable, Iterator

import httpx

from core import applog


class AiClientError(Exception):
    """可预期的 AI 请求错误（网络 / 鉴权 / 限流 / 格式）。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # network | auth | rate | format | cancelled | other
        self.message = message


def _ms_since(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _proxy_enabled_for_log() -> bool:
    """环境是否配置了代理（与 httpx trust_env 读取的 HTTP(S)_PROXY 一致，仅 true/false）。"""
    for key in (
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        if (os.environ.get(key) or "").strip():
            return True
    return False


def _normalize_base_url(base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    if not url:
        url = "https://api.openai.com/v1"
    return url


def base_url_needs_v1_hint(base_url: str) -> bool:
    """是否应对接口地址给出「通常以 /v1 结尾」的软提示。

    OpenAI 兼容约定：base 为 API 根（如 ``https://api.openai.com/v1``），
    再拼 ``/chat/completions``、``/models`` 等路径；``/models`` 本身不是 base。
    只识别、不改写：空值或已以 ``/v1`` 结尾不提示，其它结尾均提示。
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return False
    return not url.lower().endswith("/v1")


def chat_completions_url(base_url: str) -> str:
    """拼出 chat/completions 端点。"""
    base = _normalize_base_url(base_url)
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def models_url(base_url: str) -> str:
    """拼出 models 列表端点（OpenAI 兼容 GET /v1/models）。"""
    base = _normalize_base_url(base_url)
    if base.endswith("/models"):
        return base
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    return base.rstrip("/") + "/models"


def _auth_headers(api_key: str, *, accept: str) -> dict[str, str]:
    headers = {"Accept": accept}
    key = (api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _http_status_to_client_error(status: int, detail: str = "") -> AiClientError:
    code = int(status or 0)
    msg = (detail or "").strip() or f"HTTP {code}"
    if code == 401:
        return AiClientError("auth", msg)
    if code == 429:
        return AiClientError("rate", msg)
    return AiClientError("other", msg)


def _response_error_detail(resp: httpx.Response) -> str:
    try:
        text = resp.text
    except Exception:  # noqa: BLE001
        return ""
    return (text or "")[:300]


def _raise_for_httpx(exc: BaseException) -> AiClientError:
    """把 httpx 异常映射为 AiClientError。"""
    if isinstance(exc, AiClientError):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        detail = _response_error_detail(resp) if resp is not None else str(exc)
        status = int(resp.status_code) if resp is not None else 0
        return _http_status_to_client_error(status, detail)
    if isinstance(exc, httpx.TimeoutException):
        return AiClientError("network", str(exc) or "timeout")
    if isinstance(exc, (httpx.TransportError, httpx.RequestError)):
        return AiClientError("network", str(exc) or "network error")
    return AiClientError("other", str(exc) or "request failed")


def _new_client(*, connect_timeout: float, read_timeout: float) -> httpx.Client:
    """创建同步 Client：信任环境/系统代理（trust_env=True）。"""
    timeout = httpx.Timeout(
        connect=float(connect_timeout or 15.0),
        read=float(read_timeout or 60.0),
        write=float(connect_timeout or 15.0),
        pool=float(connect_timeout or 15.0),
    )
    # trust_env=True：HTTP(S)_PROXY / 系统代理；不强制直连
    return httpx.Client(timeout=timeout, trust_env=True, follow_redirects=True)


def list_models(
    *,
    base_url: str,
    api_key: str,
    timeout: float = 20.0,
) -> list[str]:
    """GET /models，返回模型 id 列表（排序后）。

    兼容 OpenAI 风格 ``{"data":[{"id":...},...]}``；若顶层是 list 也尽量解析。
    """
    url = models_url(base_url)
    headers = _auth_headers(api_key, accept="application/json")
    proxy_on = _proxy_enabled_for_log()
    t0 = time.perf_counter()
    t_headers = 0.0
    raw = b""
    try:
        with _new_client(connect_timeout=timeout, read_timeout=timeout) as client:
            resp = client.get(url, headers=headers)
            t_headers = time.perf_counter()
            raw = resp.content or b""
            if resp.status_code >= 400:
                detail = ""
                try:
                    detail = (resp.text or "")[:300]
                except Exception:  # noqa: BLE001
                    pass
                raise _http_status_to_client_error(resp.status_code, detail)
    except AiClientError:
        applog.info(
            f"AI list_models timing | fail=client"
            f" | headers_ms={_ms_since(t0) if not t_headers else int((t_headers - t0) * 1000)}"
            f" | total_ms={_ms_since(t0)} | proxy={proxy_on}"
        )
        raise
    except Exception as exc:  # noqa: BLE001
        err = _raise_for_httpx(exc)
        applog.info(
            f"AI list_models timing | fail={err.kind}"
            f" | total_ms={_ms_since(t0)} | proxy={proxy_on}"
        )
        raise err from exc

    t_read = time.perf_counter()
    try:
        body = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeError, TypeError, ValueError) as exc:
        applog.info(
            f"AI list_models timing | fail=format | total_ms={_ms_since(t0)}"
            f" | proxy={proxy_on}"
        )
        raise AiClientError("format", "invalid models response") from exc

    items: list = []
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, list):
            items = data
        elif isinstance(body.get("models"), list):
            items = body["models"]
    elif isinstance(body, list):
        items = body

    ids: list[str] = []
    seen: set[str] = set()
    for it in items:
        mid = ""
        if isinstance(it, str):
            mid = it.strip()
        elif isinstance(it, dict):
            mid = str(it.get("id") or it.get("name") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        ids.append(mid)
    ids.sort(key=lambda s: s.lower())
    t1 = time.perf_counter()
    applog.info(
        "AI list_models timing"
        f" | headers_ms={int((t_headers - t0) * 1000) if t_headers else -1}"
        f" | read_ms={int((t_read - t_headers) * 1000) if t_headers else -1}"
        f" | parse_ms={int((t1 - t_read) * 1000)}"
        f" | total_ms={int((t1 - t0) * 1000)}"
        f" | count={len(ids)}"
        f" | bytes={len(raw)}"
        f" | proxy={proxy_on}"
    )
    return ids


def _sse_line_payload(raw: bytes | str) -> tuple[str | None, bool]:
    """解析单行 SSE。

    返回 ``(data载荷, 是否结束)``：
    - 结束：``data: [DONE]``
    - 载荷：``data:`` 后的非空内容（不含 DONE）
    - 其它行（空行/注释/非 data）→ ``(None, False)``
    """
    if raw is None:
        return None, False
    try:
        if isinstance(raw, bytes):
            line = raw.decode("utf-8", errors="replace").strip()
        else:
            line = str(raw).strip()
    except Exception:  # noqa: BLE001
        return None, False
    if not line or line.startswith(":"):
        return None, False
    if not line.startswith("data:"):
        return None, False
    data = line[5:].strip()
    if not data:
        return None, False
    if data == "[DONE]":
        return None, True
    return data, False


def parse_sse_lines(raw_iter: Iterator[bytes | str]) -> Iterator[dict]:
    """从字节/文本流迭代解析 SSE ``data:`` 行，产出 JSON 对象。

    - 忽略空行与注释
    - ``data: [DONE]`` 结束（停止迭代）
    - 非法 JSON 跳过（不中断整条流）
    """
    for raw in raw_iter:
        data, done = _sse_line_payload(raw)
        if done:
            return
        if data is None:
            continue
        try:
            obj = json.loads(data)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(obj, dict):
            yield obj


def extract_delta_text(chunk: dict) -> str:
    """从 OpenAI 风格 chunk 里取出 delta.content。"""
    try:
        choices = chunk.get("choices") or []
        if not choices:
            return ""
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        return content if isinstance(content, str) else ""
    except (AttributeError, IndexError, TypeError, KeyError):
        return ""


def extract_delta_tool_calls(chunk: dict) -> list[dict]:
    """从 chunk 取出 ``delta.tool_calls`` 分片列表（可能为空）。"""
    try:
        choices = chunk.get("choices") or []
        if not choices:
            return []
        delta = choices[0].get("delta") or {}
        raw = delta.get("tool_calls")
        if not isinstance(raw, list):
            return []
        return [x for x in raw if isinstance(x, dict)]
    except (AttributeError, IndexError, TypeError, KeyError):
        return []


def merge_tool_call_delta(
    acc: dict[int, dict[str, str]],
    pieces: list[dict],
) -> None:
    """把 SSE 分片累加进 ``acc[index] = {id, name, arguments}``。"""
    for piece in pieces or []:
        if not isinstance(piece, dict):
            continue
        try:
            idx = int(piece.get("index", 0))
        except (TypeError, ValueError):
            idx = 0
        slot = acc.get(idx)
        if slot is None:
            slot = {"id": "", "name": "", "arguments": ""}
            acc[idx] = slot
        tid = piece.get("id")
        if isinstance(tid, str) and tid:
            slot["id"] = tid
        fn = piece.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str) and name:
                # 名多为整段到达；若分片则追加
                prev = slot.get("name") or ""
                slot["name"] = name if not prev else (prev + name if not prev.endswith(name) else prev)
            args = fn.get("arguments")
            if isinstance(args, str) and args:
                slot["arguments"] = (slot.get("arguments") or "") + args
        # 少数网关把 name/arguments 放在顶层
        top_name = piece.get("name")
        if isinstance(top_name, str) and top_name:
            prev = slot.get("name") or ""
            if not prev:
                slot["name"] = top_name
            elif not prev.endswith(top_name):
                slot["name"] = prev + top_name
        top_args = piece.get("arguments")
        if isinstance(top_args, str) and top_args:
            slot["arguments"] = (slot.get("arguments") or "") + top_args


def finalize_tool_calls(acc: dict[int, dict[str, str]]) -> list[dict]:
    """按 index 排序输出完整 tool_calls。"""
    if not acc:
        return []
    out: list[dict] = []
    for idx in sorted(acc.keys()):
        slot = acc[idx]
        name = str(slot.get("name") or "").strip()
        if not name:
            continue
        out.append(
            {
                "id": str(slot.get("id") or ""),
                "name": name,
                "arguments": str(slot.get("arguments") or ""),
            }
        )
    return out


def stream_chat(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    cancel: threading.Event | None = None,
    connect_timeout: float = 15.0,
    read_timeout: float = 60.0,
    on_delta: Callable[[str], None] | None = None,
    tools: list | None = None,
    tool_choice: str | None = None,
) -> dict:
    """发起流式 chat/completions。

    返回 ``{"text": str, "tool_calls": [{"id","name","arguments"}, ...]}``。
    无 tool 调用时 ``tool_calls`` 为空列表。

    ``tool_choice``：可选，如 ``"none"``（续写阶段禁止再调 tool）。
    仅在提供 ``tools`` 或显式传入时写入 payload。

    每收到一段正文 delta 调 ``on_delta``；``cancel`` 置位时关闭连接并抛
    ``AiClientError(kind='cancelled')``。

    收到 ``data: [DONE]`` 后立即结束读循环并关闭响应，不再继续读——
    否则部分服务商在 DONE 后仍保持 TCP，会一直堵到 read timeout。
    """
    if cancel is not None and cancel.is_set():
        raise AiClientError("cancelled", "cancelled")

    url = chat_completions_url(base_url)
    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    tools_list = list(tools) if tools else []
    if tools_list:
        payload["tools"] = tools_list
    choice = str(tool_choice or "").strip()
    if choice:
        payload["tool_choice"] = choice
    headers = _auth_headers(api_key, accept="text/event-stream")
    headers["Content-Type"] = "application/json"

    parts: list[str] = []
    tool_acc: dict[int, dict[str, str]] = {}
    t_headers = 0.0
    t_first_token = 0.0
    t_stream_end = 0.0
    lines_n = 0
    delta_n = 0
    end_reason = "unknown"

    proxy_on = _proxy_enabled_for_log()
    applog.info(
        "AI stream_chat start"
        f" | model={model or '-'}"
        f" | connect_s={float(connect_timeout or 15.0):g}"
        f" | read_s={float(read_timeout or 60.0):g}"
        f" | msgs={len(messages or [])}"
        f" | tools={len(tools_list)}"
        f" | proxy={proxy_on}"
    )

    t0 = time.perf_counter()
    client: httpx.Client | None = None
    try:
        client = _new_client(
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )
        # stream 上下文：拿到响应头后按行读 SSE，DONE 即停
        with client.stream(
            "POST",
            url,
            headers=headers,
            json=payload,
        ) as resp:
            t_headers = time.perf_counter()
            if resp.status_code >= 400:
                try:
                    detail = resp.read().decode("utf-8", errors="replace")[:300]
                except Exception:  # noqa: BLE001
                    detail = ""
                end_reason = "http_error"
                raise _http_status_to_client_error(resp.status_code, detail)

            for line in resp.iter_lines():
                if cancel is not None and cancel.is_set():
                    end_reason = "cancel"
                    raise AiClientError("cancelled", "cancelled")
                lines_n += 1
                data, done = _sse_line_payload(line)
                if done:
                    end_reason = "done"
                    break
                if data is None:
                    continue
                try:
                    chunk = json.loads(data)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if not isinstance(chunk, dict):
                    continue
                if "error" in chunk and not chunk.get("choices"):
                    err = chunk.get("error")
                    msg = (
                        err.get("message")
                        if isinstance(err, dict)
                        else str(err)
                    )
                    end_reason = "provider_error"
                    raise AiClientError("format", msg or "provider error")
                text = extract_delta_text(chunk)
                if text:
                    if not t_first_token:
                        t_first_token = time.perf_counter()
                    delta_n += 1
                    parts.append(text)
                    if on_delta is not None:
                        on_delta(text)
                tc_pieces = extract_delta_tool_calls(chunk)
                if tc_pieces:
                    if not t_first_token:
                        t_first_token = time.perf_counter()
                    merge_tool_call_delta(tool_acc, tc_pieces)
            else:
                # 迭代自然耗尽（无 DONE）
                if end_reason == "unknown":
                    end_reason = "eof"
            t_stream_end = time.perf_counter()
    except AiClientError:
        raise
    except Exception as exc:  # noqa: BLE001
        if cancel is not None and cancel.is_set():
            end_reason = "cancel"
            raise AiClientError("cancelled", "cancelled") from exc
        if end_reason == "unknown":
            if isinstance(exc, httpx.TimeoutException):
                end_reason = "timeout"
            else:
                end_reason = "error"
        raise _raise_for_httpx(exc) from exc
    finally:
        t_close_start = time.perf_counter()
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        t_close = time.perf_counter()
        if not t_stream_end:
            t_stream_end = t_close_start
        tool_calls = finalize_tool_calls(tool_acc)
        applog.info(
            "AI stream_chat timing"
            f" | end={end_reason}"
            f" | headers_ms="
            f"{int((t_headers - t0) * 1000) if t_headers else -1}"
            f" | first_token_ms="
            f"{int((t_first_token - t0) * 1000) if t_first_token else -1}"
            f" | stream_ms="
            f"{int((t_stream_end - t_headers) * 1000) if t_headers else -1}"
            f" | close_ms={int((t_close - t_stream_end) * 1000)}"
            f" | total_ms={int((t_close - t0) * 1000)}"
            f" | lines={lines_n} | deltas={delta_n}"
            f" | chars={sum(len(p) for p in parts)}"
            f" | tool_calls={len(tool_calls)}"
            f" | model={model or '-'}"
            f" | proxy={proxy_on}"
        )

    if cancel is not None and cancel.is_set():
        raise AiClientError("cancelled", "cancelled")
    return {
        "text": "".join(parts),
        "tool_calls": finalize_tool_calls(tool_acc),
    }
