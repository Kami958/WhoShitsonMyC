"""进程内 applog：脱敏、内存条数上限、导出格式、等级门槛。"""

from __future__ import annotations

import core.applog as applog


def setup_function():
    applog.clear()
    # 测试默认 INFO，避免环境里的 WSMC_DEBUG 干扰
    applog.set_min_level("INFO")
    applog.set_sanitize_enabled(True)


def teardown_function():
    applog.clear()
    applog.set_min_level(None)
    applog.set_sanitize_enabled(True)


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
    assert "backend: logging" in text
    assert "file: on" not in text
    assert "file: off" not in text


def test_long_message_not_content_truncated():
    big = "Z" * 9000
    applog.info(big)
    entries = applog.get_entries()
    assert entries
    assert entries[-1]["message"] == big


def test_clear_and_count():
    applog.info("a")
    applog.info("b")
    assert applog.count() >= 2
    n = applog.clear()
    assert n >= 2
    # clear 本身不自动再写（由 API 层记一条）；缓冲可为空
    assert applog.count() == 0


def test_entries_cap_is_5000():
    assert applog._MAX_ENTRIES == 5000
    assert applog.get_entries_cap() == 5000
    applog.clear()
    for i in range(5100):
        applog.info(f"row-{i}")
    assert applog.count() == 5000
    entries = applog.get_entries()
    assert entries[0]["message"] == "row-100"  # 5100-5000=100
    assert entries[-1]["message"] == "row-5099"


def test_env_summary_and_startup():
    env = applog.collect_env_summary()
    assert "cpu_logical=" in env
    assert "python=" in env
    applog.clear()
    applog.note_startup("9.9.9-test")
    assert applog.get_env_summary()
    text = applog.format_export()
    assert "Env:" in text
    assert "entries_cap: 5000" in text
    assert "min_level:" in text
    entries = applog.get_entries()
    assert any("App started" in (e.get("message") or "") for e in entries)
    assert any("9.9.9-test" in (e.get("message") or "") for e in entries)


def test_min_level_filters_debug_by_default():
    applog.set_min_level("INFO")
    applog.clear()
    applog.debug("dbg-hidden")
    applog.info("info-visible")
    applog.warn("warn-visible")
    msgs = [e["message"] for e in applog.get_entries()]
    assert "dbg-hidden" not in msgs
    assert "info-visible" in msgs
    assert "warn-visible" in msgs


def test_min_level_debug_keeps_all():
    applog.set_min_level("DEBUG")
    applog.clear()
    applog.debug("dbg-ok")
    applog.info("info-ok")
    msgs = [e["message"] for e in applog.get_entries()]
    assert "dbg-ok" in msgs
    assert "info-ok" in msgs
    assert applog.is_enabled("DEBUG")
    assert applog.get_min_level() == "DEBUG"


def test_min_level_warn_drops_info():
    applog.set_min_level("WARN")
    applog.clear()
    applog.debug("d")
    applog.info("i")
    applog.warn("w")
    applog.error("e")
    levels = [e["level"] for e in applog.get_entries()]
    assert "DEBUG" not in levels
    assert "INFO" not in levels
    assert "WARN" in levels
    assert "ERROR" in levels


def test_wsmc_log_level_env(monkeypatch):
    monkeypatch.setenv("WSMC_LOG_LEVEL", "DEBUG")
    monkeypatch.delenv("WSMC_DEBUG", raising=False)
    applog.set_min_level(None)
    assert applog.get_min_level() == "DEBUG"
    monkeypatch.setenv("WSMC_LOG_LEVEL", "warn")
    applog.set_min_level(None)
    assert applog.get_min_level() == "WARN"


def test_wsmc_debug_env(monkeypatch):
    monkeypatch.delenv("WSMC_LOG_LEVEL", raising=False)
    monkeypatch.setenv("WSMC_DEBUG", "1")
    applog.set_min_level(None)
    assert applog.get_min_level() == "DEBUG"


def test_uses_stdlib_logging_logger():
    import logging

    applog.set_min_level("INFO")
    applog.clear()
    lg = applog.get_logger()
    assert isinstance(lg, logging.Logger)
    assert lg.name == applog.LOGGER_NAME
    # 业务 API 与 logger 同一出口
    applog.info("via-api")
    assert any(e["message"] == "via-api" for e in applog.get_entries())


def test_log_settings_changed_default_debug_and_empty_noop():
    applog.set_min_level("INFO")
    applog.clear()
    applog.log_settings_changed(
        "settings",
        ["scan_workers: 4 -> 8"],
    )
    # 默认 DEBUG，INFO 门槛下不可见
    assert not any("settings changed" in (e.get("message") or "") for e in applog.get_entries())

    applog.set_min_level("DEBUG")
    applog.clear()
    applog.log_settings_changed("settings", [])
    applog.log_settings_changed("settings", None)
    assert applog.count() == 0

    applog.log_settings_changed("settings", ["scan_workers: 4 -> 8"])
    msgs = [e["message"] for e in applog.get_entries()]
    assert len(msgs) == 1
    assert msgs[0].startswith("settings changed | ")
    assert "scan_workers: 4 -> 8" in msgs[0]
    assert applog.get_entries()[0]["level"] == "DEBUG"


def test_log_settings_changed_respects_sanitize_switch():
    """路径原文交给统一接口；脱敏开/关由写入管线决定。"""
    path_change = r"snapshot_dir: C:\Users\Alice\Data -> D:\Snaps"
    applog.set_min_level("DEBUG")

    applog.set_sanitize_enabled(True)
    applog.clear()
    applog.log_settings_changed("settings", [path_change])
    msg_on = applog.get_entries()[-1]["message"]
    assert "Alice" not in msg_on
    assert r"C:\Users\Alice" not in msg_on
    assert "snapshot_dir:" in msg_on

    applog.set_sanitize_enabled(False)
    applog.clear()
    applog.log_settings_changed("settings", [path_change])
    msg_off = applog.get_entries()[-1]["message"]
    assert r"C:\Users\Alice\Data" in msg_off
    assert r"D:\Snaps" in msg_off


def test_log_settings_event():
    applog.set_min_level("DEBUG")
    applog.clear()
    applog.log_settings_event("ai", "reset to defaults", level="INFO")
    rows = applog.get_entries()
    assert rows
    assert rows[-1]["level"] == "INFO"
    assert rows[-1]["message"] == "ai | reset to defaults"


def test_sanitize_toggle_affects_new_entries_only():
    applog.set_sanitize_enabled(True)
    applog.clear()
    applog.info(r"open C:\Users\Alice\secret.db")
    redacted = applog.get_entries()[-1]["message"]
    assert "Alice" not in redacted
    assert "<path>" in redacted or "<home>" in redacted or "Users" not in redacted

    applog.set_sanitize_enabled(False)
    applog.info(r"open C:\Users\Bob\plain.db")
    plain = applog.get_entries()[-1]["message"]
    assert "Bob" in plain
    assert "plain.db" in plain
    # 旧条目仍是脱敏后的文本
    assert "Alice" not in applog.get_entries()[0]["message"]


def test_export_header_reflects_sanitize_flag():
    applog.set_sanitize_enabled(True)
    text_on = applog.format_export()
    assert "sanitize: on" in text_on
    assert "ON" in text_on or "redacted" in text_on.lower()

    applog.set_sanitize_enabled(False)
    text_off = applog.format_export()
    assert "sanitize: off" in text_off
    assert "OFF" in text_off


def test_env_log_sanitize_parser(monkeypatch):
    monkeypatch.delenv("WSMC_LOG_SANITIZE", raising=False)
    assert applog.env_log_sanitize() is None
    monkeypatch.setenv("WSMC_LOG_SANITIZE", "0")
    assert applog.env_log_sanitize() is False
    monkeypatch.setenv("WSMC_LOG_SANITIZE", "true")
    assert applog.env_log_sanitize() is True
    monkeypatch.setenv("WSMC_LOG_SANITIZE", "maybe")
    assert applog.env_log_sanitize() is None
