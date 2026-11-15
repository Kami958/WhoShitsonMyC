"""迁移切换日志：写入、完成、恢复三种状态。"""

from __future__ import annotations

from core import migrate_journal


def test_begin_and_finish(tmp_path):
    src = tmp_path / "src"
    aside = tmp_path / "src 备用"
    dst = tmp_path / "dst"

    entry_id = migrate_journal.begin_switch(str(src), str(aside), str(dst))
    pending = migrate_journal.pending_entries()
    assert any(str(item.get("id")) == entry_id for item in pending)

    migrate_journal.finish_switch(entry_id)
    assert migrate_journal.pending_entries() == []


def test_recover_restores_original_path(tmp_path):
    src = tmp_path / "src"
    aside = tmp_path / "src 备用"
    dst = tmp_path / "dst"
    aside.mkdir()
    (aside / "a.txt").write_text("data", encoding="utf-8")

    entry_id = migrate_journal.begin_switch(str(src), str(aside), str(dst))
    result = migrate_journal.recover(entry_id, action="restore")

    assert result["ok"] is True
    assert src.is_dir()
    assert (src / "a.txt").is_file()
    assert not aside.exists()
    assert migrate_journal.pending_entries() == []


def test_recover_keep_leaves_backup(tmp_path):
    src = tmp_path / "src"
    aside = tmp_path / "src 备用"
    dst = tmp_path / "dst"
    aside.mkdir()

    entry_id = migrate_journal.begin_switch(str(src), str(aside), str(dst))
    result = migrate_journal.recover(entry_id, action="keep")

    assert result["ok"] is True
    assert aside.is_dir()
    assert migrate_journal.pending_entries() == []


def test_recover_refuses_when_original_path_exists(tmp_path):
    src = tmp_path / "src"
    aside = tmp_path / "src 备用"
    dst = tmp_path / "dst"
    src.mkdir()
    aside.mkdir()

    entry_id = migrate_journal.begin_switch(str(src), str(aside), str(dst))
    result = migrate_journal.recover(entry_id, action="restore")

    assert result["ok"] is False
    assert result["code"] == "src_exists"
    assert src.is_dir()
    assert aside.is_dir()


def test_pending_drops_finished_switch(tmp_path):
    """原路径已存在说明链接已建成，残留记录不再提示。"""
    src = tmp_path / "src"
    aside = tmp_path / "src 备用"
    dst = tmp_path / "dst"
    src.mkdir()

    migrate_journal.begin_switch(str(src), str(aside), str(dst))
    assert migrate_journal.pending_entries() == []
