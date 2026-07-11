"""扫描器测试：大小聚合、遍历、进度、取消、错误处理、多线程一致性。

v3 起扫描器是多线程的且不再暴露迭代器，测试统一走
「scan_to_snapshot 扫进临时库 → snapshot_map 展开 → 断言」的路径。
"""

import os
import time

import pytest

from core.scanner import ScanCancelled, scan_to_snapshot
from core.snapshot import read_meta


def _scan(root, tmp_path, **kwargs):
    """扫描一棵树到临时快照，返回 (db_path, meta)。"""
    db = os.path.join(tmp_path, "snap.db")
    meta = scan_to_snapshot(root, db, **kwargs)
    return db, meta


def test_file_sizes_recorded(make_tree, tmp_path, snapshot_map):
    root = make_tree({"a.txt": 100, "b.txt": 250})
    db, meta = _scan(root, tmp_path)

    entries = snapshot_map(db)
    assert entries["a.txt"].size == 100
    assert entries["a.txt"].is_dir is False
    assert entries["b.txt"].size == 250
    assert meta.file_count == 2


def test_directory_size_is_aggregated(make_tree, tmp_path, snapshot_map):
    root = make_tree({"sub": {"a.txt": 100, "b.txt": 50}, "c.txt": 25})
    db, meta = _scan(root, tmp_path)

    entries = snapshot_map(db)
    # 子目录聚合 = 100 + 50
    assert entries["sub"].size == 150
    assert entries["sub"].is_dir is True
    # 根聚合 = 150 + 25
    assert meta.total_size == 175
    assert entries[""].size == 175


def test_mtime_recorded(make_tree, tmp_path, snapshot_map):
    """文件与目录都应带上真实的修改时间戳（刚创建，接近当前时间）。"""
    root = make_tree({"sub": {"a.txt": 100}})
    db, _ = _scan(root, tmp_path)

    entries = snapshot_map(db)
    now = time.time()
    assert abs(entries[os.path.join("sub", "a.txt")].mtime - now) < 300
    assert abs(entries["sub"].mtime - now) < 300
    assert abs(entries[""].mtime - now) < 300


def test_nested_aggregation(make_tree, tmp_path, snapshot_map):
    root = make_tree({"a": {"b": {"c": {"deep.txt": 999}}}})
    db, meta = _scan(root, tmp_path)

    entries = snapshot_map(db)
    assert entries[os.path.join("a", "b", "c", "deep.txt")].size == 999
    assert entries[os.path.join("a", "b", "c")].size == 999
    assert entries[os.path.join("a", "b")].size == 999
    assert entries["a"].size == 999
    assert meta.total_size == 999


def test_root_entry_present_with_none_parent(make_tree, tmp_path, snapshot_map):
    root = make_tree({"a.txt": 10})
    db, _ = _scan(root, tmp_path)

    entries = snapshot_map(db)
    assert "" in entries
    assert entries[""].is_dir is True
    assert entries[""].parent_id is None
    assert entries[""].size == 10


def test_parent_id_pointers(make_tree, tmp_path, snapshot_map):
    root = make_tree({"sub": {"a.txt": 10}})
    db, _ = _scan(root, tmp_path)

    entries = snapshot_map(db)
    assert entries["sub"].parent_id == entries[""].id
    assert entries[os.path.join("sub", "a.txt")].parent_id == entries["sub"].id


def test_empty_directory_has_zero_size(make_tree, tmp_path, snapshot_map):
    root = make_tree({"empty": {}, "a.txt": 5})
    db, meta = _scan(root, tmp_path)

    entries = snapshot_map(db)
    assert entries["empty"].size == 0
    assert entries["empty"].is_dir is True
    assert meta.dir_count == 2  # root + empty


def test_progress_callback_invoked(make_tree, tmp_path):
    root = make_tree({"a.txt": 1, "b.txt": 1})
    calls = []
    _scan(root, tmp_path, progress=lambda n, d: calls.append((n, d)))
    assert calls  # 至少回报过一次（收尾的最终计数）
    assert calls[-1][0] == 2


def test_cancel_raises(make_tree, tmp_path):
    root = make_tree({"a": {"b": {"c.txt": 1}}})
    with pytest.raises(ScanCancelled):
        _scan(root, tmp_path, cancel=lambda: True)


def test_workers_give_identical_result(make_tree, tmp_path, snapshot_map):
    """多 worker 与单 worker 扫描结果必须完全一致（对拍）。"""
    spec = {
        f"dir{i}": {f"f{j}.bin": (i * 7 + j) % 50 + 1 for j in range(8)}
        for i in range(6)
    }
    spec["top.txt"] = 33
    root = make_tree(spec)

    db1 = os.path.join(tmp_path, "one.db")
    db8 = os.path.join(tmp_path, "eight.db")
    meta1 = scan_to_snapshot(root, db1, now=1.0, workers=1)
    meta8 = scan_to_snapshot(root, db8, now=1.0, workers=8)

    assert meta1.total_size == meta8.total_size
    assert meta1.file_count == meta8.file_count
    assert meta1.dir_count == meta8.dir_count

    m1 = {p: (e.size, e.is_dir) for p, e in snapshot_map(db1).items()}
    m8 = {p: (e.size, e.is_dir) for p, e in snapshot_map(db8).items()}
    assert m1 == m8


def test_scan_to_snapshot_writes_readable_meta(make_tree, tmp_path):
    root = make_tree({"sub": {"a.txt": 100}, "b.txt": 50})
    db = os.path.join(tmp_path, "snap.db")

    meta = scan_to_snapshot(root, db, now=123.0)

    assert meta.total_size == 150
    assert meta.scanned_at == 123.0

    reloaded = read_meta(db)
    assert reloaded.total_size == 150
    assert reloaded.root == os.path.abspath(root)
    assert reloaded.file_count == 2


def test_scan_missing_root_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        scan_to_snapshot(os.path.join(tmp_path, "nope"), os.path.join(tmp_path, "x.db"))


def test_scan_file_as_root_raises(make_tree, tmp_path):
    root = make_tree({"a.txt": 10})
    file_path = os.path.join(root, "a.txt")
    with pytest.raises(NotADirectoryError):
        scan_to_snapshot(file_path, os.path.join(tmp_path, "x.db"))
