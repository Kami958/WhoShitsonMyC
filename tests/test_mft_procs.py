"""解析进程数设置与进程池的回归测试。

这三条分别锁住三个曾经踩过的坑：设置项没写进配置文件、建池时忽略调用方
指定的进程数、以及测试被打断时把超时和用户取消混为一谈。
"""

import os
import threading

import pytest

from core import store
from core.store_yaml import _write_settings_yaml


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_app_base_dir", lambda: str(tmp_path))
    store._mft_procs = 0
    yield


def test_mft_procs_survives_reload(tmp_path):
    """设成固定值后要真的写进配置文件，重新加载还在。

    只改内存不落盘时，界面显示已生效，重开程序又回到自动，
    用户每次都得重设一遍。
    """
    payload = store._settings_payload()
    payload["mft_procs"] = 4
    _write_settings_yaml(store.settings_path(), payload)

    text = open(store.settings_path(), "r", encoding="utf-8").read()
    assert "mft_procs: 4" in text

    store._mft_procs = 0
    store._apply_loaded({"common": {"mft_procs": 4}})
    assert store.get_mft_procs() == 4


def test_begin_parse_pool_honours_given_procs(monkeypatch):
    """显式给出的进程数要照办，不能被按记录数推算出来的值顶掉。"""
    from core.mft import worker

    seen = {}

    class _FakePool:
        def __init__(self, processes):
            seen["n"] = processes

    monkeypatch.setattr(worker, "Pool", _FakePool)
    monkeypatch.setattr(worker, "_warmup_pool", lambda pool: None)
    monkeypatch.delenv("WSMC_MFT_WORKERS", raising=False)
    monkeypatch.delenv("WSMC_MFT_PROCS", raising=False)

    pool, procs = worker.begin_parse_pool(1_300_000, procs=3)
    assert procs == 3
    assert seen["n"] == 3
    assert pool is not None


def test_begin_parse_pool_falls_back_to_serial(monkeypatch):
    """算出 1 个进程时不起池，交给调用方走串行。"""
    from core.mft import worker

    pool, procs = worker.begin_parse_pool(1_300_000, procs=1)
    assert pool is None
    assert procs == 1


def test_begin_parse_pool_clamps_to_upper_limit(monkeypatch):
    """超过上限的进程数要收口，不能照单全收。"""
    from core.mft import worker

    seen = {}

    class _FakePool:
        def __init__(self, processes):
            seen["n"] = processes

    monkeypatch.setattr(worker, "Pool", _FakePool)
    monkeypatch.setattr(worker, "_warmup_pool", lambda pool: None)

    _, procs = worker.begin_parse_pool(1_300_000, procs=999)
    assert procs == worker._MAX_PROCS
    assert seen["n"] == worker._MAX_PROCS



def test_finished_run_cancels_timer(monkeypatch):
    """测试正常跑完后定时器要立刻死掉，不能留到下一轮才触发。

    把限时压到零点三秒，跑一轮几乎不耗时的假测试，然后等过这个时限，
    确认没有超时事件冒出来，说明定时器确实被取消了。
    """
    import time

    from app.api import mftprocs

    monkeypatch.setattr(mftprocs, "_TIMEOUT_S", 0.3)

    events = []

    class _Api(mftprocs.MftProcsMixin):
        def _emit(self, name, payload):
            events.append(name)

        def _measure_read_speed(self, drive, cancel):
            return 0.1, 1.0

        def _measure_parse(self, drive, cancel):
            # 每进程实测内存给一个正数，界面才会显示内存
            return 2.0, 2, 1000, 40.0

        def _drive_letter_and_root(self, root):
            return None

    api = _Api()
    api._mft_test_cancel = threading.Event()
    api._mft_test_timed_out = False
    api._mft_test_partial = {}
    api._mft_test_thread = None

    monkeypatch.setattr(mftprocs, "_drive_letter_and_root", lambda root: ("C", "C:\\"))

    api._run_mft_proc_test("C:\\")
    assert events and events[-1] == "mft-proc-test-done"
    assert api._mft_test_timed_out is False

    # 越过原定限时，若定时器没被取消，这里就会多出一个超时收尾事件
    time.sleep(0.5)
    assert events.count("mft-proc-test-done") == 1
    assert "mft-proc-test-cancelled" not in events


def test_timeout_and_cancel_emit_different_events(monkeypatch):
    """超时要交出已量到的数据，用户取消只回一句已取消。"""
    from app.api import mftprocs

    events = []

    class _Api(mftprocs.MftProcsMixin):
        def _emit(self, name, payload):
            events.append((name, dict(payload or {})))

        def _settings_apply_progress(self, *a, **k):
            pass

    api = _Api()
    api._mft_test_partial = {"read_s": 6.7, "probe_s": 0.3, "pct": 42, "parse_ratio": 0.4}

    api._mft_test_timed_out = True
    api._finish_interrupted()
    assert events[-1][0] == "mft-proc-test-done"
    assert events[-1][1]["timed_out"] is True
    assert events[-1][1]["suggest"] == 0
    assert events[-1][1]["read_s"] == 6.7

    api._mft_test_timed_out = False
    api._finish_interrupted()
    assert events[-1][0] == "mft-proc-test-cancelled"
