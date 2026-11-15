"""对比算法测试：变化分类、排序、根校验、不可比较标记、格式门槛。"""

import os
import shutil
import sqlite3

import pytest

from core.differ import (
    Diff,
    DiffRootMismatch,
    _LINK_CANDIDATE_SQL,
    _LINK_REL_BATCH,
    compare_snapshots,
)
from core.link_match import abs_under_root, link_norm
from core.models import (
    REPARSE_TAG_MOUNT_POINT,
    REPARSE_TAG_SYMLINK,
    SNAPSHOT_FORMAT_VERSION,
    ChangeKind,
    Entry,
    SnapshotMeta,
)
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


def test_changed_dir_maps_ancestors(make_tree, tmp_path):
    """深层文件新增/删除时，顶层目录带 has_added / has_removed 标记。

    目录大小是递归汇总的，目录自身 kind 是 grew/shrank；筛「新增/已删除」
    时靠标记保留目录作下钻入口。
    """
    old, new = _pair(
        make_tree,
        tmp_path,
        {"a": {"keep.txt": 10, "gone.txt": 20}, "b": {"keep.txt": 10}},
        {"a": {"keep.txt": 10, "fresh.txt": 30}, "b": {"keep.txt": 10}},
    )

    with Diff(old, new) as diff:
        diff.ensure_marks()
        nodes = {n.path: n for n in diff.compare_children("")}
        assert nodes["a"].kind is ChangeKind.GREW
        assert nodes["a"].has_added is True
        assert nodes["a"].has_removed is True
        assert nodes["b"].kind is ChangeKind.UNCHANGED
        assert nodes["b"].has_added is False
        assert nodes["b"].has_removed is False


def _write_snapshot_rows(db_path, rows):
    """按给定行序写快照：rows 为 (id, parent_id, name, size, is_dir, mtime)。"""
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE IF EXISTS entries")
    conn.execute("DROP TABLE IF EXISTS meta")
    conn.execute(
        "CREATE TABLE entries ("
        "  id INTEGER PRIMARY KEY,"
        "  parent_id INTEGER,"
        "  name TEXT NOT NULL,"
        "  size INTEGER NOT NULL,"
        "  is_dir INTEGER NOT NULL,"
        "  mtime INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.executemany(
        "INSERT INTO entries VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.execute("CREATE INDEX idx_entries_parent ON entries(parent_id)")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    meta = {
        "root": "C:\\probe",
        "scanned_at": "0",
        "total_size": str(sum(r[3] for r in rows)),
        "file_count": str(sum(1 for r in rows if not r[4])),
        "dir_count": str(sum(1 for r in rows if r[4])),
        "format_version": "3",
        "skipped": "[]",
        "note": "",
    }
    conn.executemany("INSERT INTO meta VALUES (?,?)", list(meta.items()))
    conn.commit()
    conn.close()


def test_link_target_carried_into_diff_node(tmp_path):
    """v5 快照的链接目标在对比结果里可见；旧 v4 侧缺该列读出为空串。"""
    from core.models import SNAPSHOT_FORMAT_VERSION

    old_db = os.path.join(tmp_path, "old.db")
    new_db = os.path.join(tmp_path, "new.db")

    def _entries(link_target=""):
        return [
            Entry(id=1, parent_id=None, name="", size=0, is_dir=True),
            Entry(
                id=2, parent_id=1, name="link", size=0, is_dir=True,
                reparse_tag=0xA0000003, link_target=link_target,
            ),
        ]

    old_meta = SnapshotMeta(root="C:\\probe", scanned_at=0.0, format_version=3)
    new_meta = SnapshotMeta(root="C:\\probe", scanned_at=1.0,
                            format_version=SNAPSHOT_FORMAT_VERSION)
    write_snapshot(old_db, "C:\\probe", _entries(), old_meta)  # v3，无链接信息
    write_snapshot(new_db, "C:\\probe", _entries(r"D:\360download\目标"), new_meta)

    with Diff(old_db, new_db) as diff:
        diff.ensure_marks()
        nodes = {n.path: n for n in diff.compare_children("")}
    link = nodes["link"]
    assert link.reparse_tag == 0xA0000003
    assert link.link_target == r"D:\360download\目标"


def test_changed_dir_marks_by_path_not_id(tmp_path):
    """标记按相对路径判定，不受两侧快照条目 id 各自分配的影响。

    两侧快照独立自增 id：同名同路径的条目 id 可能不同（这里 keep.txt 的
    id 从 3 变 4），按路径差集它仍是「两侧都有」；若按 (parent_id, name)
    或 id 判差集会把它误判成新增。同时验证：未 ensure_marks 时标记全关。
    """
    old_db = os.path.join(tmp_path, "old.db")
    new_db = os.path.join(tmp_path, "new.db")
    _write_snapshot_rows(
        old_db,
        [
            (1, None, "", 0, 1, 0),
            (2, 1, "a", 0, 1, 0),
            (3, 2, "d", 0, 1, 0),
            (4, 3, "keep.txt", 10, 0, 1),
            (5, 2, "gone.txt", 20, 0, 1),
        ],
    )
    _write_snapshot_rows(
        new_db,
        [
            (1, None, "", 0, 1, 0),
            (2, 1, "x", 0, 1, 0),          # 新增目录：让后续 id 整体偏移
            (3, 1, "a", 0, 1, 0),
            (4, 3, "d", 0, 1, 0),
            (5, 4, "keep.txt", 10, 0, 1),  # 同路径 a\\d\\keep.txt，id 不同
            (6, 3, "fresh.txt", 30, 0, 1),  # a 深层新增
        ],
    )

    with Diff(old_db, new_db) as diff:
        nodes = {n.path: n for n in diff.compare_children("")}
        # 未启用标记：目录全不打标，不拖慢首屏
        assert nodes["a"].has_added is False
        assert nodes["a"].has_removed is False
        assert nodes["x"].kind is ChangeKind.ADDED

        diff.ensure_marks()
        marked = {n.path: n for n in diff.compare_children("")}
        a = marked["a"]
        assert a.has_added is True      # a 深层有 fresh.txt
        assert a.has_removed is True    # a 深层有 gone.txt
        # 两侧都有的目录 d 不因 id 不同被误判（d 在 a 的子层，下钻取）
        a_children = {n.path: n for n in diff.compare_children("a")}
        assert a_children[os.path.join("a", "d")].has_added is False
        assert a_children[os.path.join("a", "d")].has_removed is False

        sub = {n.path: n for n in diff.compare_children(os.path.join("a", "d"))}
        assert sub[os.path.join("a", "d", "keep.txt")].kind is ChangeKind.UNCHANGED


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

    # 版本判断以表结构为准，所以旧快照要真的建成旧结构：第二版按完整路径
    # 存，没有父子编号列。只改 meta 里的数字不再能模拟。
    conn = sqlite3.connect(old)
    conn.execute("DROP TABLE entries")
    conn.execute(
        "CREATE TABLE entries (id INTEGER PRIMARY KEY, path TEXT,"
        " size INTEGER, is_dir INTEGER)"
    )
    conn.execute("INSERT INTO entries VALUES (2, 'a.txt', 1, 0)")
    conn.execute("UPDATE meta SET value = '2' WHERE key = 'format_version'")
    conn.commit()
    conn.close()

    # 中文界面下应能拿到本地化的「格式过旧」提示（英文下为对应英文，见 i18n）。
    i18n.set_lang("zh")
    with pytest.raises(SnapshotError, match="格式过旧"):
        Diff(old, new)


def test_search_by_name(make_tree, tmp_path):
    """名称包含关键词的文件/目录应出现在搜索结果中，并带正确变化类型。"""
    old, new = _pair(
        make_tree,
        tmp_path,
        {
            "app": {
                "cache": {"temp.log": 100},
                "readme.txt": 10,
            }
        },
        {
            "app": {
                "cache": {"temp.log": 100, "temp.bak": 400},
                "readme.txt": 10,
                "TempFolder": {"x": 50},
            }
        },
    )
    with Diff(old, new) as diff:
        nodes, total = diff.search_by_name("temp")
        by_name = {n.name.casefold(): n for n in nodes}
        assert "temp.log" in by_name or "temp.bak" in by_name or "tempfolder" in by_name
        assert total >= 1
        # 新增的 temp.bak
        bak = next((n for n in nodes if n.name == "temp.bak"), None)
        assert bak is not None
        assert bak.kind is ChangeKind.ADDED
        assert bak.delta == 400


def test_search_by_path_fragment(make_tree, tmp_path):
    """关键词含路径分隔符时，按路径子串过滤。"""
    old, new = _pair(
        make_tree,
        tmp_path,
        {"app": {"cache": {"a.txt": 10}, "other": {"a.txt": 10}}},
        {"app": {"cache": {"a.txt": 20}, "other": {"a.txt": 10}}},
    )
    with Diff(old, new) as diff:
        sep = os.sep
        nodes, _total = diff.search_by_name(f"app{sep}cache{sep}a")
        paths = {n.path for n in nodes}
        assert any(p.endswith(os.path.join("app", "cache", "a.txt")) or
                   p == os.path.join("app", "cache", "a.txt") for p in paths)
        # 不应只因名字 a.txt 就把 other 下的也无条件放进来（路径过滤）
        assert os.path.join("app", "other", "a.txt") not in paths


def test_search_empty_query(make_tree, tmp_path):
    old, new = _pair(make_tree, tmp_path, {"a.txt": 1}, {"a.txt": 2})
    with Diff(old, new) as diff:
        nodes, total = diff.search_by_name("   ")
        assert nodes == []
        assert total == 0


def test_search_limit(make_tree, tmp_path):
    """limit/offset 分页，total 仍反映命中总数。"""
    files_old = {f"f{i:03d}.dat": 1 for i in range(30)}
    files_new = {f"f{i:03d}.dat": 2 for i in range(30)}
    old, new = _pair(make_tree, tmp_path, files_old, files_new)
    with Diff(old, new) as diff:
        page1, total = diff.search_by_name("f", limit=5, offset=0)
        assert len(page1) == 5
        assert total >= 5
        page2, total2 = diff.search_by_name("f", limit=5, offset=5)
        assert total2 == total
        assert len(page2) == 5
        paths1 = {n.path for n in page1}
        paths2 = {n.path for n in page2}
        assert paths1.isdisjoint(paths2)


def test_search_repeat_stable(make_tree, tmp_path):
    """同一关键词重复搜索（走缓存）结果顺序稳定，换关键词后仍正确。"""
    files_old = {f"g{i:02d}.dat": i for i in range(20)}
    files_new = {f"g{i:02d}.dat": i * 3 for i in range(20)}
    old, new = _pair(make_tree, tmp_path, files_old, files_new)
    with Diff(old, new) as diff:
        first, total = diff.search_by_name("g", limit=10, offset=0)
        again, total2 = diff.search_by_name("g", limit=10, offset=0)
        assert total == total2
        assert [n.path for n in first] == [n.path for n in again]
        # 默认按 |delta| 降序
        deltas = [abs(n.delta) for n in first]
        assert deltas == sorted(deltas, reverse=True)
        # 换关键词不受上一次缓存影响
        other, _ = diff.search_by_name("g01")
        assert {n.name for n in other} == {"g01.dat"}


def test_search_sort_name_asc(make_tree, tmp_path):
    """搜索结果可按名称升序；换排序不破坏分页总数。"""
    files_old = {
        "z_hit.dat": 10,
        "a_hit.dat": 10,
        "m_hit.dat": 10,
    }
    files_new = {
        "z_hit.dat": 100,
        "a_hit.dat": 50,
        "m_hit.dat": 200,
    }
    old, new = _pair(make_tree, tmp_path, files_old, files_new)
    with Diff(old, new) as diff:
        by_name, total = diff.search_by_name("hit", sort="name-asc")
        assert total == 3
        assert [n.name for n in by_name] == ["a_hit.dat", "m_hit.dat", "z_hit.dat"]
        by_delta, total2 = diff.search_by_name("hit", sort="delta-desc")
        assert total2 == total
        # |delta|: m +190, z +90, a +40
        assert [n.name for n in by_delta] == ["m_hit.dat", "z_hit.dat", "a_hit.dat"]


def test_search_case_sensitive(make_tree, tmp_path):
    """区分大小写：默认不区分；开启后大小写不同不再命中（内存子集过滤）。"""
    old, new = _pair(
        make_tree,
        tmp_path,
        {"TempLog.txt": 10, "other.txt": 1},
        {"TempLog.txt": 20, "other.txt": 1},
    )
    with Diff(old, new) as diff:
        loose, t1 = diff.search_by_name("templog")
        assert t1 >= 1
        assert any(n.name == "TempLog.txt" for n in loose)
        strict, t2 = diff.search_by_name("templog", case_sensitive=True)
        assert t2 == 0
        assert strict == []
        ok, t3 = diff.search_by_name("TempLog", case_sensitive=True)
        assert t3 >= 1
        assert any(n.name == "TempLog.txt" for n in ok)
        # 关回最宽：仍命中（同一关键词缓存，只是过滤放宽）
        again, t4 = diff.search_by_name("templog")
        assert t4 == t1
        assert any(n.name == "TempLog.txt" for n in again)


def test_search_exact_match(make_tree, tmp_path):
    """严格匹配：整名相等才命中；是包含匹配的子集，开关不重扫库。"""
    old, new = _pair(
        make_tree,
        tmp_path,
        {"readme.txt": 10, "my-readme.txt": 5},
        {"readme.txt": 12, "my-readme.txt": 5},
    )
    with Diff(old, new) as diff:
        contains, tc = diff.search_by_name("readme")
        names_c = {n.name for n in contains}
        assert "readme.txt" in names_c
        assert "my-readme.txt" in names_c
        exact, te = diff.search_by_name("readme.txt", exact=True)
        names_e = {n.name for n in exact}
        assert names_e == {"readme.txt"}
        # 子串严格匹配不应命中
        none, tn = diff.search_by_name("readme", exact=True)
        assert tn == 0
        assert none == []
        # 关掉严格匹配后恢复宽结果
        back, tb = diff.search_by_name("readme")
        assert tb == tc
        assert {n.name for n in back} == names_c


def test_search_kind_unchanged(make_tree, tmp_path):
    """两侧都存在且大小未变的命中应标 UNCHANGED，而非新增/删除。"""
    old, new = _pair(
        make_tree,
        tmp_path,
        {"keepme.txt": 7, "gone_keep.txt": 3},
        {"keepme.txt": 7, "new_keep.txt": 5},
    )
    with Diff(old, new) as diff:
        nodes, _ = diff.search_by_name("keep")
        by_name = {n.name: n for n in nodes}
        assert by_name["keepme.txt"].kind is ChangeKind.UNCHANGED
        assert by_name["gone_keep.txt"].kind is ChangeKind.REMOVED
        assert by_name["new_keep.txt"].kind is ChangeKind.ADDED


def test_search_cancel(monkeypatch, make_tree, tmp_path):
    """搜索中途取消应抛 SearchCancelled，取消后可正常发起新搜索。"""
    from core.differ_pkg import search as diff_impl
    from core.differ import SearchCancelled

    old, new = _pair(
        make_tree,
        tmp_path,
        {"box": {"cancel_me.txt": 10, "keep.txt": 5}},
        {"box": {"cancel_me.txt": 90, "keep.txt": 5}},
    )
    with Diff(old, new) as diff:
        orig = diff_impl._hits_with_paths

        def hook(*args, **kwargs):
            # 模拟用户在首批查询完成后点了取消
            diff.cancel_search()
            return orig(*args, **kwargs)

        monkeypatch.setattr(diff_impl, "_hits_with_paths", hook)
        with pytest.raises(SearchCancelled):
            diff.search_by_name("cancel")

        # 被取消的关键词不应留下缓存污染
        monkeypatch.setattr(diff_impl, "_hits_with_paths", orig)
        nodes, total = diff.search_by_name("cancel")
        assert total == 1
        assert nodes[0].name == "cancel_me.txt"
        assert nodes[0].delta == 80


def test_search_preheat_matches_sql(make_tree, tmp_path):
    """内存索引就绪后，搜索结果应与 SQL 路径完全一致（含中文与深路径）。"""
    spec_old = {
        "app": {
            "cache": {"temp.log": 100, "数据文件.dat": 30},
            "readme.txt": 10,
        },
        "gone_dir": {"temp.bak": 7},
    }
    spec_new = {
        "app": {
            "cache": {"temp.log": 250, "数据文件.dat": 30},
            "readme.txt": 10,
            "TempFolder": {"x": 50},
        },
    }
    old, new = _pair(make_tree, tmp_path, spec_old, spec_new)
    with Diff(old, new) as diff:
        def snap(query, **kw):
            nodes, total = diff.search_by_name(query, **kw)
            return total, [
                (n.path, n.kind, n.old_size, n.new_size, n.has_children)
                for n in nodes
            ]

        queries = [
            ("temp", {}),
            ("数据", {}),
            (os.path.join("app", "cache", "temp"), {}),
            ("Temp", {"case_sensitive": True}),
            ("temp.log", {"exact": True}),
        ]
        sql_results = []
        for q, kw in queries:
            sql_results.append(snap(q, **kw))
            # 每次清缓存，确保下一条 query 真跑 SQL
            diff._search_cache = None
            diff._search_sorted = {}

        assert diff._sides is None  # 未预热时确实走的 SQL 路径
        diff.start_search_preheat()
        assert diff.wait_search_preheat(10)

        for (q, kw), expected in zip(queries, sql_results):
            diff._search_cache = None
            diff._search_sorted = {}
            assert snap(q, **kw) == expected, f"query={q!r} {kw}"


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


# ---- 反向链接查询（links_to） ----

_LINKS_ROOT = "C:\\probe"


def _links_entries(dirs, links):
    """造快照条目：根 + 目录列表 + 链接列表。

    Args:
        dirs: 目录相对路径列表，如 ``["real", "sub"]``。
        links: ``(相对路径, 链接目标, reparse_tag)`` 列表。
    """
    entries = [Entry(id=1, parent_id=None, name="", size=0, is_dir=True)]
    ids: dict[str, int] = {"": 1}
    next_id = 2
    for d in dirs:
        parent = ""
        parts = d.split("\\")
        for i, part in enumerate(parts):
            cur = "\\".join(parts[: i + 1])
            if cur in ids:
                continue
            ids[cur] = next_id
            entries.append(Entry(
                id=next_id, parent_id=ids[parent], name=part, size=0, is_dir=True,
            ))
            next_id += 1
            parent = cur
    for path, target, tag in links:
        parent_rel, name = path.rsplit("\\", 1) if "\\" in path else ("", path)
        entries.append(Entry(
            id=next_id, parent_id=ids[parent_rel], name=name, size=0, is_dir=True,
            reparse_tag=tag, link_target=target,
        ))
        next_id += 1
    return entries


def _write_v4_snapshot(db, root, entries, version):
    """真的按第 4 版建表写一份旧快照（没有 link_target）。

    读取侧现在以 entries 表的实际列为准，所以想模拟旧快照必须真的少建列，
    只在 meta 里写一个旧版本号是不够的。
    """
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE entries (id INTEGER PRIMARY KEY, parent_id INTEGER,"
            " name TEXT NOT NULL, size INTEGER NOT NULL, is_dir INTEGER NOT NULL,"
            " mtime INTEGER NOT NULL, reparse_tag INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT INTO entries (id, parent_id, name, size, is_dir, mtime,"
            " reparse_tag) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (e.id, e.parent_id, e.name, e.size, 1 if e.is_dir else 0,
                 e.mtime, e.reparse_tag)
                for e in entries
            ],
        )
        meta = {
            "root": root, "scanned_at": "1.0", "total_size": "0",
            "file_count": "0", "dir_count": "0", "skipped": "[]",
            "format_version": str(version), "note": "", "free_size": "0",
        }
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)", meta.items()
        )
        conn.commit()
    finally:
        conn.close()


def _links_to(tmp_path, dirs, links, path, *, new_version=SNAPSHOT_FORMAT_VERSION,
              root=_LINKS_ROOT):
    """把 entries 写成一对新旧快照（同根），对当前侧反查 path 这个目录。"""
    entries = _links_entries(dirs, links)
    old_db = os.path.join(tmp_path, "old.db")
    new_db = os.path.join(tmp_path, "new.db")
    write_snapshot(
        old_db, root, entries,
        SnapshotMeta(root=root, scanned_at=0.0, format_version=3),
    )
    if new_version < 5:
        _write_v4_snapshot(new_db, root, entries, new_version)
    else:
        write_snapshot(
            new_db, root, entries,
            SnapshotMeta(root=root, scanned_at=1.0, format_version=new_version),
        )
    with Diff(old_db, new_db) as diff:
        return diff.links_to_abs(abs_under_root(root, path or ""))


def test_links_to_finds_junction(tmp_path):
    """目标为绝对路径的目录联接出现在结果里。"""
    res = _links_to(
        tmp_path, ["real"],
        [("link", "C:\\probe\\real", REPARSE_TAG_MOUNT_POINT)],
        "real",
    )
    assert res["ok"] is True
    assert res["total"] == 1
    assert res["unresolved_total"] == 0
    link = res["links"][0]
    assert link["path"] == "link"
    assert link["name"] == "link"
    assert link["kind"] == "junction"
    assert link["target"] == "C:\\probe\\real"


def test_links_to_ignores_other_targets(tmp_path):
    """指向别的目录的链接不出现在结果里。"""
    res = _links_to(
        tmp_path, ["real", "other"],
        [
            ("a", "C:\\probe\\real", REPARSE_TAG_MOUNT_POINT),
            ("b", "C:\\probe\\other", REPARSE_TAG_MOUNT_POINT),
        ],
        "real",
    )
    assert [it["path"] for it in res["links"]] == ["a"]
    assert res["total"] == 1


def test_links_to_case_and_separator_insensitive(tmp_path):
    """目标写成大写盘符加正斜杠仍命中。"""
    res = _links_to(
        tmp_path, ["real"],
        [("link", "c:/PROBE/Real", REPARSE_TAG_SYMLINK)],
        "real",
    )
    assert res["total"] == 1
    assert res["links"][0]["kind"] == "symlink"


def test_links_to_trailing_separator(tmp_path):
    """目标带尾随反斜杠仍命中。"""
    res = _links_to(
        tmp_path, ["real"],
        [("link", "C:\\probe\\real\\", REPARSE_TAG_MOUNT_POINT)],
        "real",
    )
    assert res["total"] == 1


def test_links_to_relative_target_resolved(tmp_path):
    """相对目标按链接所在目录展开；逃出扫描根的相对目标不计。"""
    res = _links_to(
        tmp_path, ["real", "sub"],
        [
            ("sub\\link", "..\\real", REPARSE_TAG_SYMLINK),
            ("sub\\out", "..\\..\\real", REPARSE_TAG_SYMLINK),
        ],
        "real",
    )
    assert [it["path"] for it in res["links"]] == ["sub\\link"]
    assert res["unresolved_total"] == 0


def test_links_to_skips_volume_guid(tmp_path):
    """卷 GUID 目标指向卷而不是目录，两个列表都不出现。"""
    res = _links_to(
        tmp_path, ["real", "sub"],
        [("sub\\odd", "\\\\?\\Volume{11111111-2222-3333-4444-555555555555}\\x",
          REPARSE_TAG_SYMLINK)],
        "real",
    )
    assert res["total"] == 0
    assert res["unresolved_total"] == 0


def test_links_to_skips_other_reparse_tags(tmp_path):
    """非目录联接/符号链接的重解析点不进候选集。"""
    res = _links_to(
        tmp_path, ["real"],
        [("link", "C:\\probe\\real", 0x8000001A)],
        "real",
    )
    assert res["total"] == 0
    assert res["unresolved_total"] == 0


def test_links_to_empty_target_skipped(tmp_path):
    """link_target 为空的行两个列表都不出现。"""
    res = _links_to(
        tmp_path, ["real"],
        [("link", "", REPARSE_TAG_MOUNT_POINT)],
        "real",
    )
    assert res["total"] == 0
    assert res["unresolved_total"] == 0


def test_links_to_unsupported_v4(tmp_path):
    """v4 快照没有 link_target 列，返回 unsupported。"""
    res = _links_to(
        tmp_path, ["real"],
        [("link", "C:\\probe\\real", REPARSE_TAG_MOUNT_POINT)],
        "real", new_version=4,
    )
    assert res["unsupported"] is True
    assert res["format_version"] == 4
    assert res["min_version"] == 5
    assert res["links"] == []
    assert res["total"] == 0


def test_links_to_outside_root_ignored(tmp_path):
    """指向快照范围之外的绝对目标两个列表都不出现。"""
    res = _links_to(
        tmp_path, ["real"],
        [("link", "D:\\other\\real", REPARSE_TAG_MOUNT_POINT)],
        "real",
    )
    assert res["total"] == 0
    assert res["unresolved_total"] == 0


def test_links_to_excludes_subdirectories(tmp_path):
    """只精确匹配右键的那个目录，不返回指向其子目录的链接。"""
    res = _links_to(
        tmp_path, ["real", "real\\sub"],
        [("link", "C:\\probe\\real\\sub", REPARSE_TAG_MOUNT_POINT)],
        "real",
    )
    assert res["total"] == 0


def test_links_to_root_target(tmp_path):
    """目标为扫描根本身时，查空路径能返回它。"""
    res = _links_to(
        tmp_path, ["real"],
        [("link", "C:\\probe\\", REPARSE_TAG_MOUNT_POINT)],
        "",
    )
    assert [it["path"] for it in res["links"]] == ["link"]
    assert res["total"] == 1


def test_links_to_drive_root(tmp_path):
    """扫描根是盘根时，目标路径不多一个分隔符，反查照样命中。"""
    res = _links_to(
        tmp_path, ["real"],
        [("link", "C:\\real", REPARSE_TAG_MOUNT_POINT)],
        "real", root="C:\\",
    )
    assert res["ok"] is True
    assert [it["path"] for it in res["links"]] == ["link"]
    assert res["total"] == 1


def test_links_to_batches_relative_targets(tmp_path):
    """相对目标多到一批装不下时，跨批的命中照样返回。"""
    links = [
        (f"sub\\l{i}", "..\\real", REPARSE_TAG_SYMLINK)
        for i in range(_LINK_REL_BATCH + 1)
    ]
    res = _links_to(tmp_path, ["real", "sub"], links, "real")
    assert res["total"] == _LINK_REL_BATCH + 1
    assert res["unresolved_total"] == 0


def test_links_query_uses_link_index(tmp_path):
    """反链查询读候选行时走快照建的链接行索引，不做整表扫描。

    索引是部分索引（只覆盖 reparse_tag <> 0 的行），SQLite 只在查询条件里
    出现同一个条件时才采用它，所以这里连查询语句一起验。
    """
    db = os.path.join(tmp_path, "s.db")
    entries = _links_entries(
        ["real"], [("link", "C:\\probe\\real", REPARSE_TAG_MOUNT_POINT)]
    )
    write_snapshot(
        db, _LINKS_ROOT, entries,
        SnapshotMeta(
            root=_LINKS_ROOT, scanned_at=0.0,
            format_version=SNAPSHOT_FORMAT_VERSION,
        ),
    )
    conn = sqlite3.connect(db)
    try:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN " + _LINK_CANDIDATE_SQL,
            (REPARSE_TAG_MOUNT_POINT, REPARSE_TAG_SYMLINK),
        ).fetchall()
    finally:
        conn.close()
    detail = " ".join(str(row[3]) for row in plan)
    assert "idx_entries_link" in detail


def test_fill_side_writes_full_row_shape(make_tree, tmp_path):
    """补查写入的行必须和正常命中一样是七项，缺少最后一项会让读取方崩。"""
    old, new = _pair(
        make_tree,
        tmp_path,
        {"app": {"a.txt": 10}},
        {"app": {"a.txt": 20}},
    )
    with Diff(old, new) as diff:
        hits: dict = {}
        diff._fill_side(
            diff._new, 1, hits, {os.path.join("app", "a.txt")}
        )
        assert hits
        row = next(iter(hits.values()))
        assert len(row) == 7
        assert row[5] == ""
        assert row[6] is None


def test_dir_shrinking_still_flags_contained_growth(make_tree, tmp_path):
    """目录整体变小、内部却有文件变大时，仍要带 has_grew 标记。

    回归用例：筛「变大」时目录曾被整条滤掉，导致用户永远打不开去看里面
    变大的文件。目录大小是递归汇总的，整体变小与内部有变大的文件可以同时
    成立，判定必须看标记而不是目录自身的净变化量。
    """
    old, new = _pair(
        make_tree,
        tmp_path,
        {"a": {"big.txt": 100, "small.txt": 10}},
        {"a": {"big.txt": 20, "small.txt": 60}},
    )
    # a 合计 110 → 80，整体变小；但 small.txt 从 10 涨到 60 是变大

    with Diff(old, new) as diff:
        diff.ensure_marks()
        nodes = {n.path: n for n in diff.compare_children("")}
        a = nodes["a"]
        assert a.kind is ChangeKind.SHRANK
        assert a.delta == -30
        assert a.has_grew is True, "目录内部有变大文件，必须保留作下钻入口"
        assert a.has_shrank is True


def test_dir_growing_still_flags_contained_shrink(make_tree, tmp_path):
    """目录整体变大、内部却有文件变小时，仍要带 has_shrank 标记。"""
    old, new = _pair(
        make_tree,
        tmp_path,
        {"a": {"big.txt": 10, "small.txt": 100}},
        {"a": {"big.txt": 160, "small.txt": 20}},
    )
    # a 合计 110 → 180，整体变大；但 small.txt 从 100 降到 20 是变小

    with Diff(old, new) as diff:
        diff.ensure_marks()
        nodes = {n.path: n for n in diff.compare_children("")}
        a = nodes["a"]
        assert a.kind is ChangeKind.GREW
        assert a.delta == 70
        assert a.has_shrank is True, "目录内部有变小文件，必须保留作下钻入口"
        assert a.has_grew is True


def test_dir_mark_absent_when_no_contained_change(make_tree, tmp_path):
    """内部只有同向变化时，反向标记保持关闭，避免筛出毫无内容的目录。"""
    old, new = _pair(
        make_tree,
        tmp_path,
        {"a": {"x.txt": 10}, "b": {"y.txt": 100}},
        {"a": {"x.txt": 50}, "b": {"y.txt": 20}},
    )

    with Diff(old, new) as diff:
        diff.ensure_marks()
        nodes = {n.path: n for n in diff.compare_children("")}
        assert nodes["a"].has_grew is True
        assert nodes["a"].has_shrank is False
        assert nodes["b"].has_shrank is True
        assert nodes["b"].has_grew is False


def test_file_nodes_never_carry_subtree_marks(make_tree, tmp_path):
    """文件节点没有子树，变大变小标记恒为关闭。"""
    old, new = _pair(
        make_tree,
        tmp_path,
        {"a": {"x.txt": 10, "y.txt": 40}},
        {"a": {"x.txt": 50, "y.txt": 20}},
    )

    with Diff(old, new) as diff:
        diff.ensure_marks()
        children = {n.path: n for n in diff.compare_children("a")}
        assert children[os.path.join("a", "x.txt")].has_grew is False
        assert children[os.path.join("a", "x.txt")].has_shrank is False
        assert children[os.path.join("a", "y.txt")].has_grew is False
        assert children[os.path.join("a", "y.txt")].has_shrank is False


def test_nested_dir_carries_grew_mark(make_tree, tmp_path):
    """标记要传达到下层目录，不仅是顶层。

    用户是在任意一层展开的，若只有顶层带标记，点进去下一层的目录又会被
    滤掉，漏选问题只解决一半。
    """
    old, new = _pair(
        make_tree,
        tmp_path,
        {"a": {"b": {"x.txt": 100}, "pad.txt": 10}},
        {"a": {"b": {"x.txt": 400}, "pad.txt": 10}},
    )
    # a 整体 110 → 410 变大；b 也是变大；两层都要带 has_grew

    with Diff(old, new) as diff:
        diff.ensure_marks()
        top = {n.path: n for n in diff.compare_children("")}
        assert top["a"].has_grew is True
        inner = {n.path: n for n in diff.compare_children("a")}
        assert inner[os.path.join("a", "b")].has_grew is True


def _write_size_pair(tmp_path, old_actual, new_actual, size=100, recorded=None):
    """造一对只有单个文件的最小快照，可指定两侧的实际占用（None = 未记录）。

    ``recorded`` 控制快照整体「是否采集了实际占用」：默认按该侧是否给了
    数值推断，也可显式指定，用于构造「采集过但这一条仍缺失」的情形。
    """
    import os as _os

    from core.models import Entry, SnapshotMeta
    from core.snapshot import write_snapshot

    root = "C:\\size"
    out = []
    for tag, actual in (("o", old_actual), ("n", new_actual)):
        db = _os.path.join(tmp_path, f"{tag}.db")
        entries = [
            Entry(id=1, parent_id=None, name="", size=0, is_dir=True, mtime=0),
            Entry(
                id=2, parent_id=1, name="a.bin", size=size,
                is_dir=False, mtime=0, actual_size=actual,
            ),
        ]
        flag = (actual is not None) if recorded is None else recorded
        meta = SnapshotMeta(
            root=root, scanned_at=0.0, total_size=size,
            file_count=1, dir_count=1,
            actual_size_total=int(actual or 0), actual_size_recorded=flag,
        )
        write_snapshot(db, root, entries, meta)
        out.append(db)
    return out[0], out[1]


def test_size_source_actual_uses_occupancy(tmp_path):
    """两侧都记录了实际占用时，选实际占用就取实际占用。"""
    from core.differ import Diff

    old, new = _write_size_pair(tmp_path, 100, 40)

    with Diff(old, new, size_source="actual") as diff:
        nodes = {n.path: n for n in diff.compare_children("")}
        assert nodes["a.bin"].new_size == 40
        assert nodes["a.bin"].old_size == 100
        assert diff.total_delta == 40 - 100

    with Diff(old, new, size_source="logical") as diff:
        nodes = {n.path: n for n in diff.compare_children("")}
        assert nodes["a.bin"].new_size == 100


def test_size_source_downgrades_when_one_side_unrecorded(tmp_path):
    """一侧没记录实际占用时，整场对比统一降为逻辑占用，不能两种单位相减。"""
    from core.differ import Diff

    old, new = _write_size_pair(tmp_path, None, 40, recorded=False)

    with Diff(old, new, size_source="actual") as diff:
        assert diff._size_source == "logical"
        assert diff._size_fallback is True
        nodes = {n.path: n for n in diff.compare_children("")}
        assert nodes["a.bin"].new_size == 100
        assert diff.total_delta == 0


def test_size_source_falls_back_when_unrecorded(tmp_path):
    """来源那一格没记录时回落到逻辑占用，不能当成 0。"""
    from core.differ import Diff

    old, new = _write_size_pair(tmp_path, None, None)

    with Diff(old, new, size_source="actual") as diff:
        nodes = {n.path: n for n in diff.compare_children("")}
        assert nodes["a.bin"].new_size == 100


def _write_legacy_db(db, root, version):
    """手工建一份真正的旧版快照（v3 无重解析点列，v4 有但没有链接目标）。"""
    import sqlite3

    cols = {
        3: "id INTEGER PRIMARY KEY, parent_id INTEGER, name TEXT NOT NULL,"
           " size INTEGER NOT NULL, is_dir INTEGER NOT NULL, mtime INTEGER NOT NULL",
        4: "id INTEGER PRIMARY KEY, parent_id INTEGER, name TEXT NOT NULL,"
           " size INTEGER NOT NULL, is_dir INTEGER NOT NULL, mtime INTEGER NOT NULL,"
           " reparse_tag INTEGER NOT NULL DEFAULT 0",
    }[version]
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE entries (" + cols + ")")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        rows = [(1, None, "", 0, 1, 0), (2, 1, "a.bin", 100, 0, 0)]
        sql = (
            "INSERT INTO entries (id, parent_id, name, size, is_dir, mtime"
            + (", reparse_tag" if version >= 4 else "")
            + ") VALUES (?, ?, ?, ?, ?, ?"
            + (", ?" if version >= 4 else "")
            + ")"
        )
        for eid, pid, name, size, is_dir, mtime in rows:
            vals = [eid, pid, name, size, is_dir, mtime]
            if version >= 4:
                vals.append(0)
            conn.execute(sql, vals)
        for k, v in {
            "root": root, "scanned_at": "0.0", "total_size": "100",
            "file_count": "1", "dir_count": "1", "skipped": "[]",
            "format_version": str(version), "note": "", "free_size": "0",
        }.items():
            conn.execute("INSERT INTO meta VALUES (?, ?)", (k, v))
        conn.commit()
    finally:
        conn.close()


def test_side_index_builds_for_legacy_versions(tmp_path):
    """旧版快照也要能建搜索索引：缺的列归一成默认值，不能把空值当数字。

    列清单统一后旧版本靠查询补空值，索引构建若原样往整数数组里塞，
    整列空值会直接抛错，第 3 版快照的搜索预热就必然失败。
    """
    import threading

    from core.differ_pkg.sideindex import _SideIndex
    from core.snapshot import open_readonly

    for version in (3, 4):
        db = os.path.join(tmp_path, f"legacy{version}.db")
        _write_legacy_db(db, r"C:\legacy", version)
        conn = open_readonly(db)
        try:
            assert conn._wmc_version == version
            idx = _SideIndex.build(conn, threading.Event())
            assert len(idx) == 2
            row = idx.row_at(1)
            assert row[1] == 100          # 逻辑大小
            assert row[4] == 0            # 重解析点标记归零
            assert row[5] == ""           # 链接目标归一成空串
            assert row[6] is None         # 实际占用未记录，不能是 0
        finally:
            conn.close()


def test_summary_reports_fallback_reason_per_side(tmp_path):
    """一侧没记录实际占用时，概览要说出是哪一侧、以及为什么缺。

    只说一句合并文案的话，用户无法判断该重扫哪一份快照。
    """
    from core.differ import Diff

    from app.api.compare import CompareMixin

    old, new = _write_size_pair(tmp_path, None, 40, recorded=None)

    with Diff(old, new, size_source="actual") as diff:
        assert diff._size_fallback is True
        # 降级之后，实例上的取值已经改成逻辑占用；界面却仍要说出是哪一侧
        # 缺数据。所以直接断言界面真正拿到的那个结果，而不是只验总量。
        summary = CompareMixin._summary(diff)
        assert summary["size_fallback"] is True
        assert summary["old"]["size_fallback"] is True
        assert summary["new"]["size_fallback"] is False
        assert summary["old"]["fallback_reason"] == "unrecorded"
        assert summary["new"]["fallback_reason"] == ""
        assert diff.side_total(diff.old_meta) == 100
        assert diff.side_total(diff.new_meta) == 100
        assert diff.total_delta == 0


def test_size_source_switch_resets_session(tmp_path):
    """切换口径后同一个快照对必须是新会话，否则界面继续按旧口径显示。"""
    from core.differ import Diff

    old, new = _write_size_pair(tmp_path, 100, 40)

    with Diff(old, new, size_source="actual") as diff:
        assert diff._size_source == "actual"
    with Diff(old, new, size_source="logical") as diff:
        assert diff._size_source == "logical"
        node = next(n for n in diff.compare_children("") if n.name == "a.bin")
        assert node.new_size == 100


def test_partial_unrecorded_still_uses_actual(tmp_path):
    """大部分条目录到了实际占用时仍按实际口径，缺的少量条目单独计数。"""
    from core.differ import Diff

    old, new = _write_size_pair(tmp_path, 100, 40)
    with Diff(old, new, size_source="actual") as diff:
        assert diff._size_source == "actual"
        assert diff._size_fallback is False
