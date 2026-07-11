"""快照读写测试（v3 邻接表结构）。"""

import os

import pytest

from core.models import Entry, SnapshotMeta
from core.snapshot import (
    SNAPSHOT_FORMAT_VERSION,
    SnapshotError,
    children_of,
    open_readonly,
    read_meta,
    write_snapshot,
)


def _sample_entries():
    """根(1) ├─ sub(2) ─ a.txt(3)  └─ b.txt(4)"""
    return [
        Entry(id=1, parent_id=None, name="", size=150, is_dir=True),
        Entry(id=2, parent_id=1, name="sub", size=100, is_dir=True),
        Entry(id=3, parent_id=2, name="a.txt", size=100, is_dir=False, mtime=42),
        Entry(id=4, parent_id=1, name="b.txt", size=50, is_dir=False),
    ]


def test_write_and_read_meta_roundtrip(tmp_path):
    db = os.path.join(tmp_path, "s.db")
    meta = SnapshotMeta(
        root="C:\\test",
        scanned_at=42.5,
        total_size=150,
        file_count=2,
        dir_count=2,
        skipped=["locked"],
    )
    write_snapshot(db, "C:\\test", _sample_entries(), meta)

    loaded = read_meta(db)
    assert loaded.root == "C:\\test"
    assert loaded.scanned_at == 42.5
    assert loaded.total_size == 150
    assert loaded.file_count == 2
    assert loaded.skipped == ["locked"]
    assert loaded.format_version == SNAPSHOT_FORMAT_VERSION


def test_children_of_by_parent_id(tmp_path):
    db = os.path.join(tmp_path, "s.db")
    meta = SnapshotMeta(root="r", scanned_at=0.0)
    write_snapshot(db, "r", _sample_entries(), meta)

    conn = open_readonly(db)
    try:
        top = {e.name: e for e in children_of(conn, 1)}
        assert set(top) == {"sub", "b.txt"}
        assert top["sub"].is_dir is True
        assert top["b.txt"].size == 50

        sub = list(children_of(conn, 2))
        assert len(sub) == 1
        assert sub[0].name == "a.txt"
        assert sub[0].mtime == 42
    finally:
        conn.close()


def test_root_entry_has_null_parent(tmp_path):
    db = os.path.join(tmp_path, "s.db")
    meta = SnapshotMeta(root="r", scanned_at=0.0)
    write_snapshot(db, "r", _sample_entries(), meta)

    conn = open_readonly(db)
    try:
        roots = list(children_of(conn, None))
        assert len(roots) == 1
        assert roots[0].name == ""
        assert roots[0].id == 1
    finally:
        conn.close()


def test_read_meta_on_garbage_file_raises(tmp_path):
    bad = os.path.join(tmp_path, "bad.db")
    with open(bad, "wb") as f:
        f.write(b"not a database at all")
    with pytest.raises(SnapshotError):
        read_meta(bad)


def test_read_meta_version_too_new_raises(tmp_path):
    db = os.path.join(tmp_path, "s.db")
    meta = SnapshotMeta(root="r", scanned_at=0.0)
    meta.format_version = SNAPSHOT_FORMAT_VERSION + 5
    write_snapshot(db, "r", _sample_entries(), meta)

    with pytest.raises(SnapshotError):
        read_meta(db)
