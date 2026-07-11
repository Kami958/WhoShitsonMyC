"""对比算法测试：变化分类、排序、根校验、不可比较标记、格式门槛。"""

import os
import shutil
import sqlite3

import pytest

from core.differ import Diff, DiffRootMismatch, compare_snapshots
from core.models import ChangeKind, Entry, SnapshotMeta
from core.scanner import scan_to_snapshot
from core.snapshot import SnapshotError, write_snapshot
from core import i18n


def _pair(make_tree, tmp_path, old_spec, new_spec):
    """在**同一个根路径**上先后造出旧/新两棵树并各扫一次快照。

    这模拟真实场景：同一目录在两个时间点的状态。先按 old_spec 建树扫成
    old.db，清空后按 new_spec 重建再扫成 new.db，两者根路径一致、可对比。
    """
    root = make_tree(old_spec, subdir="shared")
    old_db = os.path.join(tmp_path, "old.db")
    scan_to_snapshot(root, old_db, now=0.0)

    shutil.rmtree(root)
    make_tree(new_spec, subdir="shared")
    new_db = os.path.join(tmp_path, "new.db")
    scan_to_snapshot(root, new_db, now=1.0)
    return old_db, new_db


def test_grew_detected(make_tree, tmp_path):
    old, new = _pair(
        make_tree, tmp_path, {"logs": {"a.txt": 100}}, {"logs": {"a.txt": 100, "b.txt": 900}}
    )

    nodes = {n.path: n for n in compare_snapshots(old, new)}
    logs = nodes["logs"]
    assert logs.kind is ChangeKind.GREW
    assert logs.delta == 900
    assert logs.old_size == 100
    assert logs.new_size == 1000
    assert logs.has_children is True


def test_shrank_detected(make_tree, tmp_path):
    old, new = _pair(make_tree, tmp_path, {"dl": {"big.bin": 500}}, {"dl": {"big.bin": 100}})

    nodes = {n.path: n for n in compare_snapshots(old, new)}
    assert nodes["dl"].kind is ChangeKind.SHRANK
    assert nodes["dl"].delta == -400


def test_added_and_removed(make_tree, tmp_path):
    old, new = _pair(
        make_tree,
        tmp_path,
        {"keep.txt": 10, "gone.txt": 20},
        {"keep.txt": 10, "fresh.txt": 30},
    )

    nodes = {n.path: n for n in compare_snapshots(old, new)}
    assert nodes["fresh.txt"].kind is ChangeKind.ADDED
    assert nodes["fresh.txt"].delta == 30
    assert nodes["gone.txt"].kind is ChangeKind.REMOVED
    assert nodes["gone.txt"].delta == -20
    assert nodes["keep.txt"].kind is ChangeKind.UNCHANGED
    assert nodes["keep.txt"].delta == 0


def test_sorted_by_absolute_delta(make_tree, tmp_path):
    old, new = _pair(
        make_tree,
        tmp_path,
        {"a": {"x": 100}, "b": {"y": 100}, "c": {"z": 100}},
        {"a": {"x": 100, "big": 5000}, "b": {"y": 50}, "c": {"z": 100, "m": 800}},
    )

    nodes = compare_snapshots(old, new)
    deltas = [abs(n.delta) for n in nodes]
    assert deltas == sorted(deltas, reverse=True)
    assert nodes[0].path == "a"  # +5000，最大


def test_drill_down_children(make_tree, tmp_path):
    old, new = _pair(
        make_tree,
        tmp_path,
        {"app": {"cache": {"c.bin": 100}}},
        {"app": {"cache": {"c.bin": 100, "d.bin": 400}}},
    )

    with Diff(old, new) as diff:
        top = {n.path: n for n in diff.compare_children("")}
        assert top["app"].delta == 400

        cache = {n.path: n for n in diff.compare_children("app")}
        assert os.path.join("app", "cache") in cache

        files = {n.path: n for n in diff.compare_children(os.path.join("app", "cache"))}
        assert files[os.path.join("app", "cache", "d.bin")].kind is ChangeKind.ADDED


def test_total_delta(make_tree, tmp_path):
    old, new = _pair(make_tree, tmp_path, {"a.txt": 100}, {"a.txt": 100, "b.txt": 250})

    with Diff(old, new) as diff:
        assert diff.total_delta == 250


def test_mtime_carried_through(make_tree, tmp_path):
    """对比结果应带 mtime（真实扫描，值接近当前时间）。"""
    import time

    old, new = _pair(make_tree, tmp_path, {"a.txt": 100}, {"a.txt": 200})

    nodes = {n.path: n for n in compare_snapshots(old, new)}
    assert abs(nodes["a.txt"].mtime - time.time()) < 300
    assert nodes["a.txt"].to_dict()["mtime"] == nodes["a.txt"].mtime


def test_root_mismatch_raises(make_tree, tmp_path):
    # 故意扫两个不同目录 → 根不同 → 应拒绝对比
    root_a = make_tree({"a.txt": 1}, subdir="tree_a")
    root_b = make_tree({"a.txt": 1}, subdir="tree_b")
    old = os.path.join(tmp_path, "a.db")
    new = os.path.join(tmp_path, "b.db")
    scan_to_snapshot(root_a, old, now=0.0)
    scan_to_snapshot(root_b, new, now=0.0)
    with pytest.raises(DiffRootMismatch):
        Diff(old, new)


def test_skipped_dir_marked_incomparable(tmp_path):
    """一侧跳过、另一侧扫到的目录，应标为 INCOMPARABLE 而非暴涨。"""
    old_db = os.path.join(tmp_path, "old.db")
    new_db = os.path.join(tmp_path, "new.db")

    # 旧快照：secret 目录被跳过（size 记 0、无子节点），meta.skipped 含 "secret"
    old_entries = [
        Entry(id=1, parent_id=None, name="", size=0, is_dir=True),
        Entry(id=2, parent_id=1, name="secret", size=0, is_dir=True),
    ]
    old_meta = SnapshotMeta(root="C:\\d", scanned_at=0.0, total_size=0, skipped=["secret"])
    write_snapshot(old_db, "C:\\d", old_entries, old_meta)

    # 新快照：secret 正常扫到，很大
    new_entries = [
        Entry(id=1, parent_id=None, name="", size=9000, is_dir=True),
        Entry(id=2, parent_id=1, name="secret", size=9000, is_dir=True),
        Entry(id=3, parent_id=2, name="big.bin", size=9000, is_dir=False),
    ]
    new_meta = SnapshotMeta(root="C:\\d", scanned_at=0.0, total_size=9000)
    write_snapshot(new_db, "C:\\d", new_entries, new_meta)

    with Diff(old_db, new_db) as diff:
        nodes = {n.path: n for n in diff.compare_children("")}
        assert nodes["secret"].kind is ChangeKind.INCOMPARABLE


def test_old_format_rejected(make_tree, tmp_path):
    """v2 及更早的快照缺少邻接表结构，对比时应报友好错误。"""
    old, new = _pair(make_tree, tmp_path, {"a.txt": 1}, {"a.txt": 2})

    # 把其中一份的版本号改回 2，模拟旧程序生成的快照。
    conn = sqlite3.connect(old)
    conn.execute("UPDATE meta SET value = '2' WHERE key = 'format_version'")
    conn.commit()
    conn.close()

    # 中文界面下应能拿到本地化的「格式过旧」提示（英文下为对应英文，见 i18n）。
    i18n.set_lang("zh")
    with pytest.raises(SnapshotError, match="格式过旧"):
        Diff(old, new)


def test_close_releases_file_handles(make_tree, tmp_path):
    """close 后快照文件必须可删除（Windows 上句柄不释放会报 WinError 32）。"""
    old, new = _pair(make_tree, tmp_path, {"a.txt": 1}, {"a.txt": 2})

    diff = Diff(old, new)
    diff.compare_children("")
    if os.name == "nt":
        with pytest.raises(PermissionError):
            os.remove(old)  # 会话未关，Windows 拒绝删除
    diff.close()
    os.remove(old)  # 关掉后可删
    assert not os.path.exists(old)
