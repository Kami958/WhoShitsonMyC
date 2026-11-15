"""界面服务端口：启动前挑可用端口，启动后确认服务真的在接受连接。

背景：依赖库在端口绑定失败时只让后台线程退出，主线程照样把窗口建出来并
导航过去，用户看到的是浏览器错误页而不是程序自己的提示。这两个函数就是
为堵住这条静默失败路径而加的。
"""

from __future__ import annotations

import socket
import threading

import pytest

from app import webview2


def _listening_socket() -> tuple[socket.socket, int]:
    """起一个真正在监听的 socket，返回它与端口。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    return sock, sock.getsockname()[1]


def test_pick_ui_port_returns_bindable_port():
    port = webview2._pick_ui_port()
    assert port is not None
    assert webview2._UI_PORT_MIN <= port <= webview2._UI_PORT_MAX
    # 真的能绑定，说明挑出来的端口当前空闲
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()


def test_pick_ui_port_skips_busy_port(monkeypatch):
    busy, busy_port = _listening_socket()
    free_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    free_sock.bind(("127.0.0.1", 0))
    free_port = free_sock.getsockname()[1]
    free_sock.close()

    sequence = [busy_port, free_port]
    monkeypatch.setattr(
        "random.randint", lambda _a, _b: sequence.pop(0) if sequence else free_port
    )
    try:
        assert webview2._pick_ui_port() == free_port
    finally:
        busy.close()


def test_pick_ui_port_returns_none_when_all_busy(monkeypatch):
    busy, busy_port = _listening_socket()
    monkeypatch.setattr("random.randint", lambda _a, _b: busy_port)
    try:
        assert webview2._pick_ui_port() is None
    finally:
        busy.close()


def test_server_accepts_only_live_port():
    sock, port = _listening_socket()
    try:
        assert webview2._ui_server_accepts(port) is True
    finally:
        sock.close()

    # 同一个端口关掉监听后应当判为没有服务
    assert webview2._ui_server_accepts(port, timeout=0.15) is False


def test_watch_ui_server_warns_when_nothing_listens(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(webview2, "_warn_ui_server_failed", calls.append)
    monkeypatch.setattr(webview2, "_UI_SERVER_WAIT_SECONDS", 0.3)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()

    webview2._watch_ui_server(dead_port, threading.Event())
    assert calls == [dead_port]


def test_watch_ui_server_quiet_when_service_is_up(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(webview2, "_warn_ui_server_failed", calls.append)
    monkeypatch.setattr(webview2, "_UI_SERVER_WAIT_SECONDS", 1.0)

    sock, port = _listening_socket()
    try:
        webview2._watch_ui_server(port, threading.Event())
    finally:
        sock.close()
    assert calls == []


def test_watch_ui_server_stops_on_exit(monkeypatch):
    """窗口关闭后立刻收手，不再为一个已经不用的端口报警。"""
    calls: list[int] = []
    monkeypatch.setattr(webview2, "_warn_ui_server_failed", calls.append)
    monkeypatch.setattr(webview2, "_UI_SERVER_WAIT_SECONDS", 0.6)

    stop = threading.Event()
    stop.set()
    webview2._watch_ui_server(1, stop)
    assert calls == []


@pytest.mark.parametrize("port", [0, 65535])
def test_server_accepts_handles_unusable_port(port):
    assert webview2._ui_server_accepts(port, timeout=0.1) is False
