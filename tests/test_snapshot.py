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


def test_link_target_roundtrip_v5(tmp_path):
    from core.models import clean_link_target, reparse_kind

    db = os.path.join(tmp_path, "s.db")
    meta = SnapshotMeta(root="C:\\test", scanned_at=0.0)
    entries = _sample_entries() + [
        Entry(
            id=5, parent_id=1, name="link", size=0, is_dir=True,
            reparse_tag=0xA0000003,
            link_target=r"D:\360download\目标",
        )
    ]
    write_snapshot(db, "C:\\test", entries, meta)

    conn = open_readonly(db)
    try:
        assert conn._wmc_version == SNAPSHOT_FORMAT_VERSION
        top = {e.name: e for e in children_of(conn, 1)}
        assert top["link"].link_target == r"D:\360download\目标"
        assert reparse_kind(top["link"].reparse_tag) == "junction"
    finally:
        conn.close()


def _write_old_version(db, version, entries):
    """手工建一张旧版 entries 表（缺 link_target / reparse_tag 列）写入快照。"""
    import sqlite3

    cols = {
        3: "id INTEGER PRIMARY KEY, parent_id INTEGER, name TEXT NOT NULL,"
           " size INTEGER NOT NULL, is_dir INTEGER NOT NULL, mtime INTEGER NOT NULL",
        4: "id INTEGER PRIMARY KEY, parent_id INTEGER, name TEXT NOT NULL,"
           " size INTEGER NOT NULL, is_dir INTEGER NOT NULL, mtime INTEGER NOT NULL,"
           " reparse_tag INTEGER NOT NULL DEFAULT 0",
    }
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE entries (" + cols[version] + ")")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        row = (
            "INSERT INTO entries (id, parent_id, name, size, is_dir, mtime"
            + (", reparse_tag" if version >= 4 else "")
            + ") VALUES (?, ?, ?, ?, ?, ?"
            + (", ?" if version >= 4 else "")
            + ")"
        )
        for e in entries:
            vals = [e.id, e.parent_id, e.name, e.size, 1 if e.is_dir else 0, e.mtime]
            if version >= 4:
                vals.append(e.reparse_tag)
            conn.execute(row, vals)
        meta = {"root": "C:\\test", "scanned_at": "0.0", "total_size": "150",
                "file_count": "0", "dir_count": "0", "skipped": "[]",
                "format_version": str(version), "note": "", "free_size": "0"}
        conn.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            list(meta.items()),
        )
        conn.commit()
    finally:
        conn.close()


def test_old_v4_snapshot_reads_link_target_empty(tmp_path):
    """v4 快照可读，但 link_target 恒为空串（该列不存在）。"""
    from core.snapshot import children_of, open_readonly

    db = os.path.join(tmp_path, "s4.db")
    _write_old_version(
        db, 4, [Entry(id=1, parent_id=None, name="", size=0, is_dir=True)]
    )
    conn = open_readonly(db)
    try:
        root = next(iter(children_of(conn, None)))
        assert root.link_target == ""
        # 旧快照没有实际占用这一列，读出来必须是"未记录"，不能是 0
        assert root.actual_size is None
    finally:
        conn.close()


def test_old_v3_snapshot_reads_defaults(tmp_path):
    """v3 快照可读，reparse_tag 为 0、link_target 为空串。"""
    from core.snapshot import children_of, open_readonly

    db = os.path.join(tmp_path, "s3.db")
    _write_old_version(
        db, 3, [Entry(id=1, parent_id=None, name="", size=0, is_dir=True)]
    )
    conn = open_readonly(db)
    try:
        root = next(iter(children_of(conn, None)))
        assert root.reparse_tag == 0
        assert root.link_target == ""
        assert root.actual_size is None
    finally:
        conn.close()


def test_snapshot_without_version_meta_probes_columns(tmp_path):
    """meta 里没有版本号时，按表里实际有的列推断，不能当成最新版。"""
    import sqlite3

    from core.snapshot import children_of, open_readonly

    db = os.path.join(tmp_path, "nometa.db")
    conn = sqlite3.connect(db)
    # 建一个 v5 形状的表：有 link_target，没有 actual_size
    conn.execute(
        "CREATE TABLE entries (id INTEGER PRIMARY KEY, parent_id INTEGER, "
        "name TEXT, size INTEGER, is_dir INTEGER, mtime INTEGER, "
        "reparse_tag INTEGER, link_target TEXT)"
    )
    conn.execute(
        "INSERT INTO entries VALUES (1, NULL, '', 0, 1, 0, 0, '')"
    )
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()

    conn = open_readonly(db)
    try:
        assert conn._wmc_version == 5
        root = next(iter(children_of(conn, None)))
        assert root.actual_size is None
    finally:
        conn.close()


def test_read_meta_and_open_readonly_agree_on_version(tmp_path):
    """读元信息与打开连接取到同一个版本号，否则徽章和读列会互相矛盾。"""
    import sqlite3

    from core.snapshot import open_readonly, read_meta

    db = os.path.join(tmp_path, "v6.db")
    write_snapshot(db, r"C:	", _sample_entries(),
                   SnapshotMeta(root=r"C:	", scanned_at=0.0))
    # 把 meta 里的版本号写成偏低的 5：表结构仍是六版，两处都应报 6
    conn = sqlite3.connect(db)
    conn.execute("UPDATE meta SET value = '5' WHERE key = 'format_version'")
    conn.commit()
    conn.close()

    meta = read_meta(db)
    ro = open_readonly(db)
    try:
        assert meta.format_version == 6
        assert ro._wmc_version == 6
    finally:
        ro.close()


def test_read_meta_rejects_newer_format(tmp_path):
    """声明比程序还新的快照必须拒绝：新列认不出来，硬读会得到错误数据。"""
    from core.snapshot import read_meta

    db = os.path.join(tmp_path, "future.db")
    meta = SnapshotMeta(root=r"C:	", scanned_at=0.0)
    meta.format_version = SNAPSHOT_FORMAT_VERSION + 1
    write_snapshot(db, r"C:	", _sample_entries(), meta)

    with pytest.raises(SnapshotError):
        read_meta(db)


def test_probe_rejects_pre_v3_structure(tmp_path):
    """按完整路径存的老结构不能判成第三版，否则会以「缺少某列」的技术错误收场。"""
    import sqlite3

    from core.snapshot import _probe_version

    db = os.path.join(tmp_path, "v2.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE entries (id INTEGER PRIMARY KEY, path TEXT,"
        " size INTEGER, is_dir INTEGER)"
    )
    conn.commit()
    try:
        assert _probe_version(conn) == 0
    finally:
        conn.close()


def test_both_version_entry_points_agree_when_structure_unknown(tmp_path):
    """表结构认不出来时，读元信息与打开连接要给出同一个版本号。

    否则同一份文件会在徽标上显示一个版本、在选列时按另一个版本处理。
    """
    import sqlite3

    from core.snapshot import open_readonly, read_meta

    db = os.path.join(tmp_path, "odd.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE entries (id INTEGER PRIMARY KEY, path TEXT,"
        " size INTEGER, is_dir INTEGER)"
    )
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('root', 'C:\\t')")
    conn.execute("INSERT INTO meta VALUES ('scanned_at', '0')")
    conn.execute("INSERT INTO meta VALUES ('format_version', '4')")
    conn.commit()
    conn.close()

    ro = open_readonly(db)
    try:
        assert ro._wmc_version == read_meta(db).format_version
    finally:
        ro.close()
