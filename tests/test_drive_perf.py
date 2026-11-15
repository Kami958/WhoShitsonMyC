"""按盘性能参数：编解码、默认回落、写盘往返与扫描取用。"""

import io
import os

import pytest

from core import store
from core.store_values import _drive_perf_from_yaml, _drive_perf_to_yaml
from core.store_yaml import _write_settings_yaml


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_app_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(store, "_drive_perf", {})
    monkeypatch.setattr(store, "_scan_workers", 4)
    monkeypatch.setattr(store, "_mft_procs", 0)
    yield


def test_drive_perf_roundtrip_text():
    text = _drive_perf_to_yaml(
        {"C": {"mft_read": 1, "dir_threads": 4, "mft_procs": 11}}
    )
    assert text == "C|1|4|11"
    assert _drive_perf_from_yaml(text) == {
        "C": {"mft_read": 1, "dir_threads": 4, "mft_procs": 11}
    }


def test_drive_perf_skips_broken_entries():
    assert _drive_perf_from_yaml("坏;E|1|2;C|1|4|11") == {
        "C": {"mft_read": 1, "dir_threads": 4, "mft_procs": 11}
    }


def test_effective_values_fall_back_to_defaults():
    # 没单独设过的盘：读取线程 1、目录遍历跟随全局、进程数跟随全局自动
    eff = store.get_drive_effective("D:\\")
    assert eff == {"mft_read": 1, "dir_threads": 4, "mft_procs": 0}

    store.set_drive_perf("D", mft_read=2, dir_threads=8, mft_procs=6)
    assert store.get_drive_effective("D") == {
        "mft_read": 2,
        "dir_threads": 8,
        "mft_procs": 6,
    }


def test_clearing_one_item_returns_to_default():
    store.set_drive_perf("C", mft_read=2, dir_threads=8, mft_procs=6)
    store.set_drive_perf("C", dir_threads=0)
    assert store.get_drive_perf("C")["dir_threads"] == 0
    assert store.get_drive_effective("C")["dir_threads"] == 4


def test_drive_perf_survives_reload():
    store.set_drive_perf("C", mft_read=3, dir_threads=6, mft_procs=5)
    path = store.settings_path()
    _write_settings_yaml(path, store._settings_payload())
    text = io.open(path, encoding="utf-8").read()
    assert "drives: \"C|3|6|5\"" in text

    store._drive_perf.clear()
    store._apply_loaded({"common": {"drives": "C|3|6|5"}})
    assert store.get_drive_perf("C") == {
        "mft_read": 3,
        "dir_threads": 6,
        "mft_procs": 5,
    }


def test_unknown_drive_letter_is_ignored():
    assert store.get_drive_perf("") == {"mft_read": 0, "dir_threads": 0, "mft_procs": 0}
    assert store.set_drive_perf("", mft_read=2) == {}

def test_perf_test_survives_reload(tmp_path, monkeypatch):
    """测试结果写进配置后，重开程序还能读回来。"""
    monkeypatch.setattr(store, "_app_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(store, "_perf_tests", {})
    payload = store._settings_payload()
    payload["perf_tests"] = store._perf_tests_to_yaml(
        {
            "C": {
                "procs": {"read_s": 7.5, "single_parse_s": 22.0, "per_proc_mb": 41},
                "threads": {"points": [[1, 6.2], [4, 5.0]], "sample": 150000},
            }
        }
    )
    store._write_settings_yaml(store.settings_path(), payload)
    text = io.open(store.settings_path(), encoding="utf-8").read()
    assert "perf_tests:" in text
    assert "single_parse_s" in text

    store._perf_tests.clear()
    store._apply_loaded(store._load_settings_yaml(store.settings_path()))
    back = store.get_perf_test("C")
    assert round(float(back["procs"]["single_parse_s"]), 1) == 22.0
    assert len(back["threads"]["points"]) == 2


def test_perf_test_bad_text_is_ignored():
    """坏串或空串一律当没有，不让界面拿到半截数据。"""
    assert store._perf_tests_from_yaml("") == {}
    assert store._perf_tests_from_yaml("not json") == {}
    assert store._perf_tests_from_yaml("[1,2,3]") == {}

def test_save_then_reload_keeps_both_kinds(tmp_path, monkeypatch):
    """一次测试的两组结果分别保存，重测一组不抹掉另一组，且都能从盘上读回。

    实测数据测完就落盘：用户即使本次不改任何设置，下次打开也应能看到曲线。
    """
    monkeypatch.setattr(store, "_app_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(store, "_perf_tests", {})
    assert store.save_perf_test("Z", {"procs": {"read_s": 7.5, "single_parse_s": 22.0}})
    assert store.save_perf_test("Z", {"threads": {"points": [[1, 6.0], [4, 5.0]], "sample": 150000}})
    mem = store.get_perf_test("Z")
    assert round(float(mem["procs"]["single_parse_s"]), 1) == 22.0
    assert len(mem["threads"]["points"]) == 2

    # 不点保存也能从盘上读回，两组都在
    store._perf_tests.clear()
    store._apply_loaded(store._load_settings_yaml(store.settings_path()))
    disk = store.get_perf_test("Z")
    assert round(float(disk["procs"]["single_parse_s"]), 1) == 22.0
    assert len(disk["threads"]["points"]) == 2
