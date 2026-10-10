"""迁移类作业的占位交接：完成事件送到界面时，占位已经放开。

前端收到完成事件后会立刻开下一项，而那时后台线程可能还没退出。占位若按
「线程是否存活」来判定，下一项就会撞上「已有目录迁移正在进行」被误判失败。
这里把三条边界固定下来：回调里能立刻接上下一项、作业真在跑时仍然互斥、
作业中途异常也要放开占位。
"""

from __future__ import annotations

import threading
import time

import pytest


class _FakeWindow:
    """假窗口：事件推送有个落点即可，脚本执行不做任何事。"""

    def evaluate_js(self, script: str) -> None:
        pass


@pytest.fixture
def api(monkeypatch, tmp_path):
    """构造一个数据目录落在临时目录里的 Api。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    import app
    from core import fs_migrate, i18n

    language = i18n.get_lang()
    instance = app.Api()
    instance.set_window(_FakeWindow())
    yield instance
    instance.set_language(language)
    # 迁移会把备份后缀改成当前界面语言的写法，改回默认的中文写法
    fs_migrate.set_backup_suffix("")
    fs_migrate.set_old_target_suffix("")


@pytest.fixture
def stub_migration(monkeypatch):
    """把真实的迁移流程换成立刻返回的桩，只留下线程与占位的时序。"""
    from core import fs_migrate

    runs: list[str] = []

    def migrate_directory(source, dest, **_kwargs):
        runs.append(str(source))
        return {
            "source": str(source),
            "dest": str(dest),
            "files": 1,
            "bytes": 16,
            "links": 1,
            "skipped": 0,
        }

    monkeypatch.setattr(
        fs_migrate,
        "validate_migrate",
        lambda source, dest: {"ok": True, "source": source, "dest": dest},
    )
    monkeypatch.setattr(fs_migrate, "free_space_ok", lambda _src, _dst: True)
    monkeypatch.setattr(fs_migrate, "migrate_directory", migrate_directory)
    monkeypatch.setattr(fs_migrate, "register_link", lambda *a, **k: None)
    monkeypatch.setattr(fs_migrate, "record_history", lambda *a, **k: None)
    return runs


def test_done_event_can_hand_over_to_next_job(api, stub_migration, monkeypatch):
    """完成事件送达时就该能接上下一项，且那时占位已经放开。"""
    hand_overs: list[dict] = []
    slots_at_done: list[dict | None] = []
    finished = threading.Event()

    def fake_emit(event, payload):
        if event != "migrate-dir-done":
            return
        # 送达界面时占位应当已经放开，界面据此开下一项不会被拒
        slots_at_done.append(api._link_job_busy_error())
        if len(slots_at_done) == 1:
            hand_overs.append(api.start_directory_migration("D:/next-src", "E:/next-dst"))
        else:
            finished.set()

    monkeypatch.setattr(api, "_emit", fake_emit)

    assert api.start_directory_migration("D:/src", "E:/dst") == {"started": True}
    assert finished.wait(5), "第二项没有跑完，完成事件没有连续送达"
    assert slots_at_done == [None, None], slots_at_done
    assert hand_overs[0] == {"started": True}, hand_overs[0]
    assert stub_migration == ["D:/src", "D:/next-src"]


def test_backup_action_reaches_core(api, monkeypatch):
    """界面选的备份处理要原样传到核心，并随完成事件回给界面。"""
    from core import fs_migrate

    seen: list[str] = []

    def migrate_directory(source, dest, **kwargs):
        seen.append(str(kwargs.get("backup_action")))
        return {
            "source": str(source),
            "dest": str(dest),
            "files": 1,
            "bytes": 16,
            "links": 0,
            "skipped": 0,
            "backup_action": str(kwargs.get("backup_action")),
            "backup_cleaned": True,
            "backup_permanent": True,
        }

    monkeypatch.setattr(
        fs_migrate,
        "validate_migrate",
        lambda source, dest: {"ok": True, "source": source, "dest": dest},
    )
    monkeypatch.setattr(fs_migrate, "free_space_ok", lambda _src, _dst: True)
    monkeypatch.setattr(fs_migrate, "migrate_directory", migrate_directory)
    monkeypatch.setattr(fs_migrate, "register_link", lambda *a, **k: None)
    monkeypatch.setattr(fs_migrate, "record_history", lambda *a, **k: None)

    payloads: list[dict] = []
    finished = threading.Event()

    def fake_emit(event, payload):
        if event != "migrate-dir-done":
            return
        payloads.append(dict(payload))
        finished.set()

    monkeypatch.setattr(api, "_emit", fake_emit)

    assert api.start_directory_migration("D:/src", "E:/dst", "purge") == {"started": True}
    assert finished.wait(5), "迁移没有跑完"
    assert seen == ["purge"]
    assert payloads[0].get("backup_action") == "purge"
    assert payloads[0].get("backup_permanent") is True


def test_running_job_still_blocks_new_one(api, stub_migration, monkeypatch):
    """作业还在跑时，新的迁移请求仍要被拒绝。"""
    from core import fs_migrate

    blocked: list[dict] = []

    def migrate_directory(source, dest, **_kwargs):
        # 迁移进行中再点一次开始：此时占位必须还在
        blocked.append(api.start_directory_migration("D:/other", "E:/other"))
        return {
            "source": str(source),
            "dest": str(dest),
            "files": 1,
            "bytes": 16,
            "links": 1,
            "skipped": 0,
        }

    monkeypatch.setattr(fs_migrate, "migrate_directory", migrate_directory)
    done = threading.Event()
    monkeypatch.setattr(
        api,
        "_emit",
        lambda event, payload: done.set() if event == "migrate-dir-done" else None,
    )

    assert api.start_directory_migration("D:/src", "E:/dst") == {"started": True}
    assert done.wait(5)
    assert blocked and blocked[0].get("started") is None
    assert "error" in blocked[0]


def test_slot_released_when_job_body_raises(api, stub_migration, monkeypatch):
    """作业中途抛出未捕获异常时，占位也要放开，后续迁移不能被永久挡住。"""
    from core import fs_migrate

    # 写历史失败会逃出线程体，只有外壳兜底才能放开占位
    monkeypatch.setattr(
        fs_migrate,
        "record_history",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("写历史失败")),
    )
    monkeypatch.setattr(fs_migrate, "migrate_directory", lambda *a, **k: {"source": "s", "dest": "d"})
    monkeypatch.setattr(threading, "excepthook", lambda _args: None)
    monkeypatch.setattr(api, "_emit", lambda event, payload: None)

    assert api.start_directory_migration("D:/src", "E:/dst") == {"started": True}
    for _ in range(100):
        if not api._link_job:
            break
        time.sleep(0.05)
    assert api._link_job == "", "线程异常退出后占位没有放开"
