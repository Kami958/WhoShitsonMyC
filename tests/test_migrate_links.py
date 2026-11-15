"""链接登记文件的读写锁：读与写串行，替换被占用时短暂重试。"""

from __future__ import annotations

import os
import threading
import time

import pytest

from core import migrate_links


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(
        migrate_links, "_registry_path", lambda: str(tmp_path / "links.json")
    )
    monkeypatch.setattr(migrate_links, "_ensure_registry_backup", lambda path: None)
    return tmp_path / "links.json"


def test_save_links_retries_replace(tmp_path, monkeypatch):
    target = _isolate(tmp_path, monkeypatch)
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "file in use")
        return real_replace(src, dst)

    monkeypatch.setattr(migrate_links.os, "replace", flaky_replace)
    migrate_links.save_links([{"link": "a", "target": "b"}])

    assert calls["n"] == 2
    assert target.is_file()
    assert migrate_links.load_links() == [{"link": "a", "target": "b"}]


def test_save_links_raises_after_retries(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    calls = {"n": 0}

    def always_busy(src, dst):
        calls["n"] += 1
        raise PermissionError(13, "file in use")

    monkeypatch.setattr(migrate_links.os, "replace", always_busy)
    with pytest.raises(OSError):
        migrate_links.save_links([{"link": "a"}])

    assert calls["n"] == 6
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_load_links_blocked_while_writing(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    real_replace = os.replace
    entered_write = threading.Event()
    entered_read = threading.Event()
    release = threading.Event()

    def slow_replace(src, dst):
        entered_write.set()
        release.wait(3.0)
        return real_replace(src, dst)

    def slow_read():
        entered_read.set()
        return []

    monkeypatch.setattr(migrate_links.os, "replace", slow_replace)
    monkeypatch.setattr(migrate_links, "_read_registry", slow_read)

    writer = threading.Thread(
        target=lambda: migrate_links.save_links([{"link": "a"}]), daemon=True
    )
    writer.start()
    assert entered_write.wait(3.0), "写线程没有进入替换步骤"

    reader = threading.Thread(target=migrate_links.load_links, daemon=True)
    reader.start()
    time.sleep(0.3)
    assert not entered_read.is_set(), "读没有被写锁挡住"

    release.set()
    writer.join(3.0)
    reader.join(3.0)
    assert not writer.is_alive()
    assert not reader.is_alive()
    assert entered_read.is_set()
