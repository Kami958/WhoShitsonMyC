"""快照压缩 / 解压测试。"""

from __future__ import annotations

import os

import pytest

from core.compress import (
    compress_db,
    ensure_db_path,
    is_compressed_path,
    read_meta_any,
)
from core.differ import Diff
from core.models import Entry, SnapshotMeta
from core.snapshot import write_snapshot
from core.store import (
    delete_snapshot,
    get_compress_snapshots,
    list_snapshots,
    set_compress_snapshots,
)


def _write_sample(db: str, root: str = r"C:\test", when: float = 10.0) -> SnapshotMeta:
    entries = [
        Entry(id=1, parent_id=None, name="", size=150, is_dir=True),
        Entry(id=2, parent_id=1, name="sub", size=100, is_dir=True),
        Entry(id=3, parent_id=2, name="a.txt", size=100, is_dir=False, mtime=42),
        Entry(id=4, parent_id=1, name="b.txt", size=50, is_dir=False),
    ]
    meta = SnapshotMeta(
        root=root,
        scanned_at=when,
        total_size=150,
        file_count=2,
        dir_count=2,
        skipped=["locked"],
    )
    write_snapshot(db, root, entries, meta)
    return meta


def test_compress_and_read_meta_without_full_extract(tmp_path):
    db = os.path.join(tmp_path, "s.db")
    meta = _write_sample(db)
    dbz = compress_db(db, meta)

    assert is_compressed_path(dbz)
    assert dbz.endswith(".dbz")
    assert not os.path.exists(db)  # 源 .db 应被删掉
    assert os.path.isfile(dbz)

    loaded = read_meta_any(dbz)
    assert loaded.root == meta.root
    assert loaded.scanned_at == meta.scanned_at
    assert loaded.total_size == 150
    assert loaded.file_count == 2
    assert loaded.skipped == ["locked"]


def test_ensure_db_path_extracts_on_demand(tmp_path):
    db = os.path.join(tmp_path, "s.db")
    meta = _write_sample(db)
    dbz = compress_db(db, meta)

    out1 = ensure_db_path(dbz)
    assert out1.endswith(".db")
    assert os.path.isfile(out1)
    # 不落应用 cache 目录；二次调用应命中本进程会话登记
    out2 = ensure_db_path(dbz)
    assert out1 == out2
    assert "WhoShitsOnMyC" not in out1.replace("\\", "/").split("/") or "cache" not in out1.lower()

    # 解出的库可读
    from core.snapshot import children_of, open_readonly

    conn = open_readonly(out1)
    try:
        root = list(children_of(conn, None))
        assert len(root) == 1
        kids = {e.name for e in children_of(conn, 1)}
        assert kids == {"sub", "b.txt"}
    finally:
        conn.close()


def test_list_snapshots_includes_dbz(tmp_path):
    plain = os.path.join(tmp_path, "a.db")
    _write_sample(plain, when=10.0)
    db = os.path.join(tmp_path, "b.db")
    meta = _write_sample(db, when=50.0)
    dbz = compress_db(db, meta)

    infos = list_snapshots(str(tmp_path))
    paths = {i.path for i in infos}
    assert plain in paths
    assert dbz in paths
    # 按时间新→旧
    assert infos[0].path == dbz
    assert infos[0].compressed is True
    assert infos[1].compressed is False


def test_diff_via_ensure_db_path(tmp_path):
    old_db = os.path.join(tmp_path, "old.db")
    new_db = os.path.join(tmp_path, "new.db")
    _write_sample(old_db, when=1.0)
    # 新侧：多一个文件
    entries = [
        Entry(id=1, parent_id=None, name="", size=200, is_dir=True),
        Entry(id=2, parent_id=1, name="sub", size=100, is_dir=True),
        Entry(id=3, parent_id=2, name="a.txt", size=100, is_dir=False, mtime=42),
        Entry(id=4, parent_id=1, name="b.txt", size=50, is_dir=False),
        Entry(id=5, parent_id=1, name="c.txt", size=50, is_dir=False),
    ]
    meta_new = SnapshotMeta(
        root=r"C:\test", scanned_at=2.0, total_size=200, file_count=3, dir_count=2
    )
    write_snapshot(new_db, r"C:\test", entries, meta_new)

    old_dbz = compress_db(old_db)
    new_dbz = compress_db(new_db)

    with Diff(ensure_db_path(old_dbz), ensure_db_path(new_dbz)) as diff:
        nodes = {n.name: n for n in diff.compare_children("")}
        assert "c.txt" in nodes
        assert nodes["c.txt"].kind.value == "added"


def test_delete_snapshot_removes_dbz(tmp_path):
    db = os.path.join(tmp_path, "x.db")
    meta = _write_sample(db)
    dbz = compress_db(db, meta)
    assert os.path.exists(dbz)
    delete_snapshot(dbz)
    assert not os.path.exists(dbz)


def test_note_roundtrip_db_and_dbz(tmp_path):
    """备注进文件：.db meta 与 .dbz meta.json 均可读写。"""
    from core.compress import write_snapshot_note
    from core.store import set_note, snapshot_info

    db = os.path.join(tmp_path, "n.db")
    meta = _write_sample(db)
    assert write_snapshot_note(db, "hello") == "hello"
    assert read_meta_any(db).note == "hello"
    assert snapshot_info(db).note == "hello"

    dbz = compress_db(db, read_meta_any(db))
    assert read_meta_any(dbz).note == "hello"
    assert set_note(dbz, "  after zip  ") == "after zip"
    assert snapshot_info(dbz).note == "after zip"
    assert read_meta_any(dbz).note == "after zip"


def test_compress_setting_roundtrip():
    assert get_compress_snapshots() is False
    assert set_compress_snapshots(True) is True
    assert get_compress_snapshots() is True
    assert set_compress_snapshots(False) is False
