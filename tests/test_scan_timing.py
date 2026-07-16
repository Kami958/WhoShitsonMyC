"""扫描计时并入 applog：启用条件与出口。"""

from __future__ import annotations

import core.applog as applog
from dev import scan_timing


def setup_function():
    applog.clear()
    applog.set_min_level("INFO")


def teardown_function():
    applog.clear()
    applog.set_min_level(None)


def test_disabled_when_info_and_no_timing_flag(monkeypatch):
    monkeypatch.delenv("WSMC_SCAN_TIMING", raising=False)
    applog.set_min_level("INFO")
    assert scan_timing.is_enabled() is False
    t = scan_timing.start_timer(root="C:\\x", workers=2)
    assert t.finish(status="ok") is None
    assert applog.count() == 0


def test_enabled_when_debug_level(monkeypatch):
    monkeypatch.delenv("WSMC_SCAN_TIMING", raising=False)
    applog.set_min_level("DEBUG")
    assert scan_timing.is_enabled() is True
    t = scan_timing.start_timer(root="C:\\Users\\Alice\\data", workers=4)
    t.span_start("scan_to_snapshot")
    t.span_end("scan_to_snapshot")
    t.set_meta(file_count=10, dir_count=2, backend="scandir")
    rep = t.finish(status="ok")
    assert isinstance(rep, dict)
    assert rep["status"] == "ok"
    msgs = [e["message"] for e in applog.get_entries()]
    assert any("[scan-timing]" in m for m in msgs)
    # root 经 applog 脱敏
    joined = "\n".join(msgs)
    assert "Alice" not in joined


def test_enabled_by_wsmc_scan_timing_flag(monkeypatch):
    monkeypatch.setenv("WSMC_SCAN_TIMING", "1")
    applog.set_min_level("INFO")
    assert scan_timing.is_enabled() is True
    t = scan_timing.start_timer(root="D:\\x", workers=1)
    t.finish(status="ok")
    entries = applog.get_entries()
    assert entries
    assert entries[-1]["level"] == "INFO"
    assert "[scan-timing]" in entries[-1]["message"]


def test_error_status_is_warn(monkeypatch):
    monkeypatch.setenv("WSMC_SCAN_TIMING", "1")
    applog.set_min_level("INFO")
    t = scan_timing.start_timer(root="E:\\x")
    t.finish(status="error")
    levels = [e["level"] for e in applog.get_entries() if "[scan-timing]" in e["message"]]
    assert "WARN" in levels


def test_jsonl_optional(monkeypatch, tmp_path):
    log_path = tmp_path / "t.jsonl"
    monkeypatch.setenv("WSMC_SCAN_TIMING", "1")
    monkeypatch.setenv("WSMC_SCAN_TIMING_LOG", str(log_path))
    applog.set_min_level("INFO")
    t = scan_timing.start_timer(root="F:\\x", workers=1)
    t.set_meta(file_count=3)
    t.finish(status="ok")
    text = log_path.read_text(encoding="utf-8")
    assert "total_s" in text
    assert "file_count" in text
