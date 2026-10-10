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

    # 把其中一份的版本号改回 2，模拟旧程序生成的快照。
    conn = sqlite3.connect(old)
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
