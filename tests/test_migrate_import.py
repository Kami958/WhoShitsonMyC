"""导入已有迁移：把别处建好的目录联接收进登记表。

只收 mklink /J 建的目录联接，符号链接不收（它的删除和迁移走另一套处理）。
"""

import os
import shutil

import pytest

from core import migrate_links

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows junction only")


def _make_junction(link: str, target: str) -> None:
    """建一条目录联接。没有权限或盘不支持时跳过用例，不误报。"""
    import _winapi

    try:
        _winapi.CreateJunction(target, link)
    except (OSError, ValueError) as exc:
        pytest.skip(f"Cannot create junction: {exc}")


def test_import_link_registers_junction(make_tree, tmp_path):
    """导入成功后能在管理列表里查到，且没有备份目录。"""
    root = make_tree({"real": {"a.txt": 10}})
    link = os.path.join(str(tmp_path), "link")
    _make_junction(link, os.path.join(root, "real"))

    rec = migrate_links.import_link(link)

    assert rec["link"] == link
    assert rec["target"] == os.path.join(root, "real")
    assert rec["backup"] == ""
    assert [r["link"] for r in migrate_links.link_records()] == [link]


def test_import_link_twice_is_rejected(make_tree, tmp_path):
    root = make_tree({"real": {"a.txt": 10}})
    link = os.path.join(str(tmp_path), "link")
    _make_junction(link, os.path.join(root, "real"))
    migrate_links.import_link(link)

    with pytest.raises(migrate_links.MigrateError) as ei:
        migrate_links.import_link(link)

    assert str(ei.value) == "import_exists"


def test_import_link_rejects_plain_dir(make_tree, tmp_path):
    """普通目录不是联接，收进来也没法迁移。"""
    root = make_tree({"real": {"a.txt": 10}})

    with pytest.raises(migrate_links.MigrateError) as ei:
        migrate_links.import_link(os.path.join(root, "real"))

    assert str(ei.value) == "import_not_junction"


def test_import_link_rejects_missing_path(tmp_path):
    with pytest.raises(migrate_links.MigrateError) as ei:
        migrate_links.import_link(os.path.join(str(tmp_path), "nope"))

    assert str(ei.value) == "import_missing"


def test_import_link_rejects_broken_target(make_tree, tmp_path):
    """目标被删掉后这条联接导不进来：登记了也没东西可迁。"""
    root = make_tree({"real": {"a.txt": 10}})
    link = os.path.join(str(tmp_path), "link")
    _make_junction(link, os.path.join(root, "real"))
    shutil.rmtree(os.path.join(root, "real"))

    with pytest.raises(migrate_links.MigrateError) as ei:
        migrate_links.import_link(link)

    assert str(ei.value) == "import_target_missing"


def test_import_link_accepts_matching_target(make_tree, tmp_path):
    """用户填的实际目录和联接指向的一致，正常收进来。"""
    root = make_tree({"real": {"a.txt": 10}})
    link = os.path.join(str(tmp_path), "link")
    target = os.path.join(root, "real")
    _make_junction(link, target)

    rec = migrate_links.import_link(link, target)

    assert rec["target"] == target


def test_import_link_rejects_swapped_paths(make_tree, tmp_path):
    """两个框填反了报填反，用户照着换过来即可。"""
    root = make_tree({"real": {"a.txt": 10}})
    link = os.path.join(str(tmp_path), "link")
    target = os.path.join(root, "real")
    _make_junction(link, target)

    with pytest.raises(migrate_links.MigrateError) as ei:
        migrate_links.import_link(target, link)

    assert str(ei.value) == "import_reversed"


def test_import_link_rejects_mismatched_target(make_tree, tmp_path):
    """填的实际目录不是它指向的那个，拒绝导入。"""
    root = make_tree({"real": {"a.txt": 10}, "other": {"b.txt": 10}})
    link = os.path.join(str(tmp_path), "link")
    _make_junction(link, os.path.join(root, "real"))

    with pytest.raises(migrate_links.MigrateError) as ei:
        migrate_links.import_link(link, os.path.join(root, "other"))

    assert str(ei.value) == "import_pair_mismatch"


def test_scan_junctions_lists_first_level_only(make_tree):
    """只扫第一层，深一层的联接要另选目录才看得到。"""
    root = make_tree({"real": {"a.txt": 10}, "deep": {"b.txt": 10}})
    _make_junction(os.path.join(root, "top"), os.path.join(root, "real"))
    _make_junction(os.path.join(root, "deep", "inner"), os.path.join(root, "real"))

    items = migrate_links.scan_junctions(root)

    assert [it["link"] for it in items] == [os.path.join(root, "top")]
    assert items[0]["target"] == os.path.join(root, "real")
    assert items[0]["managed"] is False


def test_scan_junctions_marks_managed(make_tree):
    """已导入的那条打上标记，界面据此不让重复导入。"""
    root = make_tree({"real": {"a.txt": 10}})
    link = os.path.join(root, "link")
    _make_junction(link, os.path.join(root, "real"))
    migrate_links.import_link(link)

    items = migrate_links.scan_junctions(root)

    assert len(items) == 1
    assert items[0]["managed"] is True


def test_scan_junctions_rejects_missing_dir(tmp_path):
    with pytest.raises(migrate_links.MigrateError) as ei:
        migrate_links.scan_junctions(os.path.join(str(tmp_path), "nope"))

    assert str(ei.value) == "import_dir_missing"
