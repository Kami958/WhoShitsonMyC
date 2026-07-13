"""进程内 applog：脱敏、环形缓冲、导出格式。"""

from __future__ import annotations

import core.applog as applog


def setup_function():
    applog.clear()


def teardown_function():
    applog.clear()


def test_sanitize_redacts_windows_path():
    raw = r"failed open C:\Users\Alice\Documents\secret.db"
    out = applog.sanitize(raw)
    assert "Alice" not in out
    assert "secret.db" not in out or "<path>" in out
    assert "C:\\Users\\Alice" not in out


def test_log_exception_includes_traceback_sanitized():
    try:
        raise RuntimeError(r"boom at D:\Games\Steam\app.exe")
    except RuntimeError as exc:
        applog.exception("unit test error", exc)
    entries = applog.get_entries()
    assert entries
    last = entries[-1]
    assert last["level"] == "ERROR"
    assert "unit test error" in last["message"]
    assert last["traceback"]
    assert r"D:\Games" not in last["traceback"]
    assert "RuntimeError" in last["traceback"]


def test_does_not_auto_persist_and_export_text():
    applog.info("hello")
    applog.warn("careful")
    text = applog.format_export()
    assert "WhoShitsOnMyC application log" in text
    assert "not recorded" in text.lower() or "Privacy" in text
    assert "hello" in text
    assert "careful" in text
    # 明确不写磁盘：模块无默认日志文件路径 API
    assert not hasattr(applog, "log_file_path")


def test_clear_and_count():
    applog.info("a")
    applog.info("b")
    assert applog.count() >= 2
    n = applog.clear()
    assert n >= 2
    # clear 本身不自动再写（由 API 层记一条）；缓冲可为空
    assert applog.count() == 0


def test_ring_buffer_cap_is_1024():
    assert applog._MAX_ENTRIES == 1024
    applog.clear()
    for i in range(1100):
        applog.info(f"row-{i}")
    assert applog.count() == 1024
    entries = applog.get_entries()
    assert entries[0]["message"] == "row-76"  # 1100-1024=76
    assert entries[-1]["message"] == "row-1099"


def test_env_summary_and_startup():
    env = applog.collect_env_summary()
    assert "cpu_logical=" in env
    assert "python=" in env
    applog.clear()
    applog.note_startup("9.9.9-test")
    assert applog.get_env_summary()
    text = applog.format_export()
    assert "Env:" in text
    assert "buffer_cap: 1024" in text
    entries = applog.get_entries()
    assert any("App started" in (e.get("message") or "") for e in entries)
    assert any("9.9.9-test" in (e.get("message") or "") for e in entries)
