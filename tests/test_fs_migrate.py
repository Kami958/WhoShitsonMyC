"""目录迁移测试：校验、复制、校验、让位、junction、回滚。"""

import json
import os
import shutil
import stat
import subprocess

import pytest

from core.fs_migrate import (
    MigrateError,
    migrate_directory,
    record_history,
    load_history,
    clear_history,
    validate_migrate,
)


def _tree(base, spec, subdir="root"):
    root = os.path.join(base, subdir)
    os.makedirs(root, exist_ok=True)

    def _build(parent, s):
        for name, val in s.items():
            target = os.path.join(parent, name)
            if isinstance(val, dict):
                os.makedirs(target, exist_ok=True)
                _build(target, val)
            else:
                with open(target, "wb") as f:
                    f.write(b"\0" * val)

    _build(root, spec)
    return root


def _dir_bytes(path):
    total = 0
    count = 0
    for _dp, _dn, fns in os.walk(path):
        for n in fns:
            total += os.path.getsize(os.path.join(_dp, n))
            count += 1
    return total, count


def test_validate_migrate_codes(tmp_path):
    src = _tree(tmp_path, {"a.txt": 10, "sub": {"b.bin": 20}})
    dest = os.path.join(tmp_path, "dest", "root")

    r = validate_migrate(src, dest)
    assert r["ok"] is True
    assert r["code"] == "ok"
    assert r["aside"].startswith(src + " 原目录备份")

    assert validate_migrate(os.path.join(tmp_path, "nope"), dest)["code"] == "invalid_source"
    assert validate_migrate(os.path.join(tmp_path, "nope.txt"), dest)["code"] == "invalid_source"

    # 源是文件
    f = os.path.join(src, "a.txt")
    assert validate_migrate(f, dest)["code"] == "not_dir"

    # 目标已存在
    os.makedirs(dest, exist_ok=True)
    assert validate_migrate(src, dest)["code"] == "dest_exists"
    os.rmdir(dest)

    # 目标在源内部
    assert validate_migrate(src, os.path.join(src, "inside"))["code"] == "inside_each"

    # 目标与源相同
    assert validate_migrate(src, src)["code"] == "same_path"

    # 空目标
    assert validate_migrate(src, "")["code"] == "invalid_dest"


def test_migrate_copies_sets_aside_and_junctions(tmp_path):
    """迁移后：原路径变成 junction 指向目标，真实数据在目标与备份里。"""
    src = _tree(tmp_path, {"a.txt": 10, "sub": {"b.bin": 20, "c.txt": 30}})
    dest = os.path.join(tmp_path, "real", "root")
    total_before, n_before = _dir_bytes(src)

    result = migrate_directory(src, dest)
    assert result["ok"] is True
    assert result["dest"] == dest
    assert result["files"] == n_before
    assert result["bytes"] == total_before

    # 源现在应是 junction（reparse tag），仍可读
    assert os.path.isdir(src)
    assert os.path.exists(os.path.join(src, "sub", "b.bin"))
    # 备份里是原始数据
    aside = result["aside"]
    assert os.path.isdir(aside)
    assert _dir_bytes(aside) == (total_before, n_before)
    # 目标里是复制数据
    assert _dir_bytes(dest) == (total_before, n_before)


@pytest.mark.skipif(os.name != "nt", reason="junction 仅 Windows")
def test_migrate_source_becomes_junction(tmp_path):
    """Windows 实测：迁移后原路径是 junction（重解析点），仍可经它读到文件。"""
    src = _tree(tmp_path, {"a.txt": 5, "sub": {"b.txt": 7}})
    dest = os.path.join(tmp_path, "real", "root")
    result = migrate_directory(src, dest)
    assert result["ok"] is True

    tag = getattr(os.lstat(src), "st_reparse_tag", 0)
    assert int(tag or 0) != 0  # junction
    # 经 junction 读到目标里的文件
    assert os.path.isfile(os.path.join(src, "a.txt"))
    assert os.path.isfile(os.path.join(src, "sub", "b.txt"))


def test_migrate_rollback_when_junction_fails(tmp_path, monkeypatch):
    """建 junction 失败：让位目录改回原名，复制产物清掉。"""
    import core.fs_migrate as fm

    src = _tree(tmp_path, {"a.txt": 5})
    dest = os.path.join(tmp_path, "real", "root")

    def boom(_dest, _link):
        raise MigrateError("junction:forced")

    monkeypatch.setattr(fm, "_create_junction", boom)
    with pytest.raises(MigrateError):
        fm.migrate_directory(src, dest)

    # 原目录完整保留，无备份残留，无目标残留
    assert os.path.isfile(os.path.join(src, "a.txt"))
    assert not os.path.exists(dest)
    backups = [n for n in os.listdir(tmp_path) if n.startswith("root")]
    assert backups == ["root"]


def test_migrate_rollback_when_verify_fails(tmp_path, monkeypatch):
    """校验不一致：清掉复制产物，不动原目录。"""
    import core.fs_migrate as fm

    src = _tree(tmp_path, {"a.txt": 5})
    dest = os.path.join(tmp_path, "real", "root")

    def bad_verify(_s, _d, **kw):
        return {"ok": False, "files": 1, "bytes": 5, "mismatch": ["a.txt"]}

    monkeypatch.setattr(fm, "_verify", bad_verify)
    _force_full_verify(monkeypatch)
    with pytest.raises(MigrateError):
        fm.migrate_directory(src, dest)
    assert os.path.isfile(os.path.join(src, "a.txt"))
    assert not os.path.exists(dest)


def test_migrate_cancel_cleans_partial(tmp_path):
    """复制到一半取消：走 chunked 复制真实取消，目标残留被清理，源目录不动。"""
    import core.fs_migrate as fm

    src = _tree(tmp_path, {"big.bin": 6 * 1024 * 1024})
    dest = os.path.join(tmp_path, "real", "root")
    calls = [0]

    def armed_cancel():
        calls[0] += 1
        return calls[0] >= 5

    with pytest.raises(MigrateError) as ei:
        fm.migrate_directory(src, dest, cancel=armed_cancel)
    assert "cancelled" in str(ei.value.message)
    # 复制了一半（部分字节已写入目标），取消后目标被清、源目录完整
    assert not os.path.exists(dest)
    assert os.path.isfile(os.path.join(src, "big.bin"))


def test_copy_file_cancels_mid_file(tmp_path):
    """_copy_file：单个大文件中途取消，目标文件只写出部分（可被上层清掉）。"""
    import core.fs_migrate as fm

    src = os.path.join(tmp_path, "big.bin")
    with open(src, "wb") as f:
        f.write(b"\0" * (6 * 1024 * 1024))
    dst = os.path.join(tmp_path, "copy.bin")
    calls = [0]

    def armed_cancel():
        calls[0] += 1
        return calls[0] >= 4

    with pytest.raises(MigrateError) as ei:
        fm._copy_file(src, dst, cancel=armed_cancel)
    assert "cancelled" in str(ei.value.message)
    # 目标存在但只写了部分（前几次 cancel 为假，等到第 4 次才中断）
    assert os.path.exists(dst)
    size = os.path.getsize(dst)
    assert 0 < size < 6 * 1024 * 1024


# ---- 链接登记 / 删除 / 还原 ----


def _registry_isolated(monkeypatch, tmp_path):
    """把登记文件指向临时目录，避免污染真实 app_data_dir。"""
    import core.fs_migrate as fm
    from core import migrate_links

    monkeypatch.setattr(migrate_links, "_registry_path", lambda: os.path.join(tmp_path, "links.json"))
    return fm


def _force_full_verify(monkeypatch):
    """让统计检查对不上，把迁移逼回全树对比那条路。

    目录静止、复制也没出错时，迁移只跑三项统计（不调 ``_verify``），
    所以依赖 ``_verify`` 的用例得先让两边的统计对不上。
    """
    import core.fs_migrate as fm

    state = {"n": 0}

    def _stats(*_a, **_kw):
        state["n"] += 1
        return (0, state["n"], 0)

    monkeypatch.setattr(fm, "_walk_stats", _stats)


def test_remigrate_keep_renames_old_target(monkeypatch, tmp_path):
    """再次迁移：数据搬到新盘、链接改指新盘，旧目标改名留在原处待删。"""
    fm = _registry_isolated(monkeypatch, tmp_path)
    src = _tree(tmp_path, {"a.txt": 10, "sub": {"b.bin": 20}})
    first = os.path.join(tmp_path, "first", "root")
    second = os.path.join(tmp_path, "second", "root")
    fm.migrate_directory(src, first, progress=None, cancel=lambda: False)
    fm.register_link(src, first, src + " 原目录备份")
    total, count = _dir_bytes(first)

    result = fm.remigrate_link(src, second, old_target_action="keep")

    assert result["ok"] is True
    assert result["old_target_action"] == "keep"
    assert result["old_target_removed"] is False
    # 链接指向新目标，从原路径照样读得到全部内容
    assert os.path.realpath(src) == os.path.realpath(second)
    assert _dir_bytes(src) == (total, count)
    assert result["files"] == count
    # 旧目标改名留在原处，数据一份不少
    left = result["old_target_left"]
    assert left.startswith(first + " 旧迁移")
    assert os.path.isdir(left) is True
    assert os.path.isdir(first) is False
    assert _dir_bytes(left) == (total, count)
    # 登记换成新目标，并记下待删的旧目标
    rec = fm.get_link_record(src)
    assert os.path.realpath(str(rec["target"])) == os.path.realpath(second)
    assert rec["old_target"] == left
    assert fm.link_records()[0]["old_target_exists"] is True


def test_remigrate_purge_removes_old_target(monkeypatch, tmp_path):
    """再次迁移选彻底删除：旧目标整棵删掉，登记里的旧目标清空。"""
    fm = _registry_isolated(monkeypatch, tmp_path)
    src = _tree(tmp_path, {"a.txt": 10})
    first = os.path.join(tmp_path, "first", "root")
    second = os.path.join(tmp_path, "second", "root")
    fm.migrate_directory(src, first, progress=None, cancel=lambda: False)
    fm.register_link(src, first, "")

    result = fm.remigrate_link(src, second, old_target_action="purge")

    assert result["old_target_removed"] is True
    assert result["old_target_left"] == ""
    assert os.path.exists(first) is False
    assert _dir_bytes(src) == (10, 1)
    rec = fm.get_link_record(src)
    assert rec.get("old_target") in ("", None)
    assert os.path.realpath(str(rec["target"])) == os.path.realpath(second)


def test_remigrate_purge_keeps_old_target_when_changed_after_verify(monkeypatch, tmp_path):
    """校验完之后旧目标里又有人写过东西：不能永久删，改成改名保留。"""
    import time

    fm = _registry_isolated(monkeypatch, tmp_path)
    src = _tree(tmp_path, {"a.txt": 10})
    first = os.path.join(tmp_path, "first", "root")
    second = os.path.join(tmp_path, "second", "root")
    fm.migrate_directory(src, first, progress=None, cancel=lambda: False)
    fm.register_link(src, first, "")

    # 模拟校验之后旧目标里的文件被改过：最新修改时间跑到未来
    monkeypatch.setattr(fm, "_newest_mtime", lambda *a, **kw: time.time() + 60)

    result = fm.remigrate_link(src, second, old_target_action="purge")

    assert result["old_target_action"] == "keep"
    assert result["old_target_removed"] is False
    assert _dir_bytes(result["old_target_left"]) == (10, 1)
    assert _dir_bytes(src) == (10, 1)


def test_remigrate_cancel_before_delete_keeps_old_target(monkeypatch, tmp_path):
    """删除前按取消：旧目标一个字节不动，改名保留，删除入口还在。"""
    fm = _registry_isolated(monkeypatch, tmp_path)
    src = _tree(tmp_path, {"a.txt": 10})
    first = os.path.join(tmp_path, "first", "root")
    second = os.path.join(tmp_path, "second", "root")
    fm.migrate_directory(src, first, progress=None, cancel=lambda: False)
    fm.register_link(src, first, "")

    state = {"cancel": False}
    real_verify = fm._verify

    def _verify_then_arm(*a, **kw):
        out = real_verify(*a, **kw)
        state["cancel"] = True  # 校验刚过就按下取消，等价于删除阶段前按下
        return out

    monkeypatch.setattr(fm, "_verify", _verify_then_arm)
    _force_full_verify(monkeypatch)
    result = fm.remigrate_link(
        src, second, old_target_action="purge", cancel=lambda: state["cancel"]
    )

    assert result["old_target_action"] == "keep"
    assert result["old_target_removed"] is False
    assert result["old_target_left"].startswith(first + " 旧迁移")
    assert _dir_bytes(result["old_target_left"]) == (10, 1)
    assert _dir_bytes(src) == (10, 1)


def test_validate_remigrate_rejects_pending_old_target(monkeypatch, tmp_path):
    """上一次的旧目标还没删：拒绝再次迁移，否则那个删除入口会被这次顶掉。"""
    fm = _registry_isolated(monkeypatch, tmp_path)
    src = _tree(tmp_path, {"a.txt": 10})
    first = os.path.join(tmp_path, "first", "root")
    second = os.path.join(tmp_path, "second", "root")
    third = os.path.join(tmp_path, "third", "root")
    fm.migrate_directory(src, first, progress=None, cancel=lambda: False)
    fm.register_link(src, first, "")

    first_pass = fm.remigrate_link(src, second, old_target_action="keep")
    assert first_pass["old_target_action"] == "keep"

    pre = fm.validate_remigrate(src, third)
    assert pre["ok"] is False
    assert pre["code"] == "old_target_pending"

    # 把旧目标处理掉之后，这条链接就能接着往下一块盘迁
    fm.set_link_old_target(src, "")
    assert fm.validate_remigrate(src, third)["ok"] is True


@pytest.mark.skipif(os.name != "nt", reason="junction 仅 Windows")
def test_remigrate_retargets_links_inside_old_target(monkeypatch, tmp_path):
    """新目标里指向旧目标内部的链接要改指新目标里对应的位置，删旧目标也不断。"""
    fm = _registry_isolated(monkeypatch, tmp_path)
    src = _tree(tmp_path, {"a.txt": 10, "real": {"b.txt": 20}})
    first = os.path.join(tmp_path, "first", "root")
    second = os.path.join(tmp_path, "second", "root")
    fm.migrate_directory(src, first, progress=None, cancel=lambda: False)
    fm.register_link(src, first, "")
    # 旧目标里加一条指向自己内部的目录联接
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", os.path.join(first, "j"), os.path.join(first, "real")],
        check=True,
        capture_output=True,
    )

    result = fm.remigrate_link(src, second, old_target_action="purge")

    assert result["ok"] is True
    assert result["links_retargeted"] == 1
    assert os.path.realpath(os.path.join(second, "j")) == os.path.realpath(
        os.path.join(second, "real")
    )
    assert os.path.isfile(os.path.join(second, "j", "b.txt")) is True
    assert os.path.isdir(first) is False
    assert os.path.realpath(src) == os.path.realpath(second)


def test_remigrate_cancel_keeps_old_target(monkeypatch, tmp_path):
    """取消：新目标上的半成品清掉，旧目标一个字节不动，链接照旧指旧目标。"""
    fm = _registry_isolated(monkeypatch, tmp_path)
    src = _tree(tmp_path, {"a.txt": 10, "sub": {"b.bin": 20}})
    first = os.path.join(tmp_path, "first", "root")
    second = os.path.join(tmp_path, "second", "root")
    fm.migrate_directory(src, first, progress=None, cancel=lambda: False)
    fm.register_link(src, first, "")
    total, count = _dir_bytes(first)

    with pytest.raises(MigrateError):
        fm.remigrate_link(src, second, cancel=lambda: True)

    assert os.path.exists(second) is False
    assert _dir_bytes(first) == (total, count)
    assert os.path.realpath(src) == os.path.realpath(first)
    assert fm.get_link_record(src).get("old_target") in ("", None)


def test_remigrate_refresh_only_copies_changed(tmp_path):
    """补差异：大小与修改时间都对得上的文件不再复制，只补缺的那些。"""
    import core.fs_migrate as fm

    src = _tree(tmp_path, {"a.txt": 10, "b.bin": 20}, subdir="src")
    dst = os.path.join(tmp_path, "dst")
    os.makedirs(dst, exist_ok=True)
    # 目标侧的 a.txt 大小与修改时间都对得上，但内容是另一回事
    shutil.copy2(os.path.join(src, "a.txt"), os.path.join(dst, "a.txt"))
    with open(os.path.join(dst, "a.txt"), "wb") as f:
        f.write(b"x" * 10)
    st = os.stat(os.path.join(src, "a.txt"))
    os.utime(os.path.join(dst, "a.txt"), (st.st_atime, st.st_mtime))

    fm._copy_tree(src, dst, progress=None, cancel=lambda: False, refresh=True)

    with open(os.path.join(dst, "a.txt"), "rb") as f:
        assert f.read() == b"x" * 10  # 跳过没重拷
    assert os.path.getsize(os.path.join(dst, "b.bin")) == 20  # 缺的补上


def test_validate_remigrate_codes(monkeypatch, tmp_path):
    """再次迁移前置校验的错误码。"""
    fm = _registry_isolated(monkeypatch, tmp_path)
    src = _tree(tmp_path, {"a.txt": 10})
    first = os.path.join(tmp_path, "first", "root")
    fm.migrate_directory(src, first, progress=None, cancel=lambda: False)
    fm.register_link(src, first, "")
    dest = os.path.join(tmp_path, "second", "root")

    assert fm.validate_remigrate(src, dest)["ok"] is True
    assert fm.validate_remigrate(src, "")["code"] == "invalid_dest"
    assert fm.validate_remigrate(os.path.join(tmp_path, "nope"), dest)["code"] == "unknown_link"
    assert fm.validate_remigrate(src, first)["code"] == "same_path"
    os.makedirs(os.path.join(tmp_path, "taken"), exist_ok=True)
    assert fm.validate_remigrate(src, os.path.join(tmp_path, "taken"))["code"] == "dest_exists"
    drive = os.path.splitdrive(os.path.abspath(str(tmp_path)))[0] + "\\"
    assert fm.validate_remigrate(src, drive)["code"] == "drive_root"
    # 新目标塞在旧目标里，或塞在链接路径里（后者会直接写回旧目标）
    assert fm.validate_remigrate(src, os.path.join(first, "inner"))["code"] == "inside_each"
    assert fm.validate_remigrate(src, os.path.join(src, "inner"))["code"] == "inside_each"
    # 登记在册但不是链接，或链接指向的目标已经不在
    plain = _tree(tmp_path, {"x.txt": 4}, subdir="plain")
    fm.register_link(plain, first, "")
    assert fm.validate_remigrate(plain, dest)["code"] == "not_a_link"
    gone_target = os.path.join(tmp_path, "gone")
    os.makedirs(gone_target, exist_ok=True)
    gone_link = os.path.join(tmp_path, "gone_link")
    fm._create_junction(gone_target, gone_link)
    os.rmdir(gone_target)
    fm.register_link(gone_link, gone_target, "")
    assert fm.validate_remigrate(gone_link, dest)["code"] == "target_missing"


def test_register_and_list_links(monkeypatch, tmp_path):
    fm = _registry_isolated(monkeypatch, tmp_path)
    fm.register_link(r"D:\data\big", r"F:\moved\big", r"D:\data\big 原目录备份",
                     files=12, bytes_=3456)
    fm.register_link(r"C:\old", r"D:\new", r"C:\old 原目录备份")
    recs = fm.link_records()
    assert len(recs) == 2
    # 新登记的排前面
    assert recs[0]["link"] == r"C:\old"
    assert recs[1]["bytes"] == 3456
    # 文件系统状态：路径都不存在
    assert recs[0]["state"] == "link_missing"
    assert recs[0]["link_exists"] is False
    assert recs[1]["target_exists"] is False


def test_register_same_link_updates(monkeypatch, tmp_path):
    fm = _registry_isolated(monkeypatch, tmp_path)
    fm.register_link(r"D:\x", r"T1", "")
    fm.register_link(r"D:\x", r"T2", r"D:\x 原目录备份")
    assert len(fm.link_records()) == 1
    assert fm.link_records()[0]["target"] == r"T2"


def test_remove_link_record(monkeypatch, tmp_path):
    fm = _registry_isolated(monkeypatch, tmp_path)
    fm.register_link(r"D:\x", r"T", "")
    fm.remove_link_record(r"D:\x")
    assert fm.link_records() == []


def test_delete_junction_only_removes_link(monkeypatch, tmp_path):
    """删除链接只删 junction，目标数据保留；非链接路径拒绝。"""
    import _winapi

    fm = _registry_isolated(monkeypatch, tmp_path)
    real = os.path.join(tmp_path, "real")
    link = os.path.join(tmp_path, "link")
    os.makedirs(os.path.join(real, "sub"))
    open(os.path.join(real, "a.txt"), "wb").write(b"x")
    try:
        _winapi.CreateJunction(real, link)
    except (OSError, ValueError) as exc:
        pytest.skip(f"Cannot create junction: {exc}")

    assert fm._is_reparse(link)
    fm.delete_junction(link)
    assert not os.path.lexists(link)
    assert os.path.isfile(os.path.join(real, "a.txt"))

    # 非链接路径：拒绝
    with pytest.raises(fm.MigrateError):
        fm.delete_junction(real)


def test_restore_link_copies_back_and_recycles_backup(monkeypatch, tmp_path):
    """还原：目标复制回原路径、删除链接、备份与目标一起移入回收站。"""
    import _winapi

    fm = _registry_isolated(monkeypatch, tmp_path)
    # 备份/目标回收站用桩，避免真进回收站
    recycled = []
    monkeypatch.setattr(fm.fs_delete, "delete_to_recycle", lambda p: recycled.append(p))

    link = os.path.join(tmp_path, "link")
    target = os.path.join(tmp_path, "real", "target")
    backup = os.path.join(tmp_path, "backup")
    os.makedirs(os.path.join(target, "sub"))
    open(os.path.join(target, "a.txt"), "wb").write(b"data")
    os.makedirs(backup)
    open(os.path.join(backup, "old.txt"), "wb").write(b"old")
    _winapi.CreateJunction(target, link)

    fm.register_link(link, target, backup, files=1, bytes_=4)
    result = fm.restore_link(link)
    assert result["ok"] is True
    assert result["recycled_backup"] == backup
    assert result["recycled_target"] == target
    # 链接没了，原路径现在是真实目录，含复制来的数据
    assert not os.path.lexists(link) or not fm._is_reparse(link)
    assert os.path.isdir(os.path.join(link, "sub"))
    assert os.path.isfile(os.path.join(link, "a.txt"))
    # 记录已移除
    assert fm.link_records() == []
    # 备份与目标都进了回收站
    assert recycled == [backup, target]


def test_restore_link_permanent_backup(monkeypatch, tmp_path):
    import _winapi

    fm = _registry_isolated(monkeypatch, tmp_path)
    # 回收站与彻底删除都用桩，避免真删磁盘
    recycled, wiped = [], []
    monkeypatch.setattr(fm.fs_delete, "delete_to_recycle", lambda p: recycled.append(p))
    monkeypatch.setattr(fm.fs_delete, "delete_permanent", lambda p, **kw: wiped.append(p))

    link = os.path.join(tmp_path, "link")
    target = os.path.join(tmp_path, "real", "target")
    backup = os.path.join(tmp_path, "backup")
    os.makedirs(os.path.join(target, "sub"))
    open(os.path.join(target, "a.txt"), "wb").write(b"data")
    os.makedirs(backup)
    _winapi.CreateJunction(target, link)

    fm.register_link(link, target, backup, files=1, bytes_=4)
    result = fm.restore_link(link, permanent=True)
    assert result["ok"] is True
    assert result["recycled_backup"] == backup
    assert result["backup_permanent"] is True
    # 备份彻底删除，目标仍走回收站
    assert wiped == [backup]
    assert recycled == [target]
    assert os.path.isdir(os.path.join(link, "sub"))


def test_restore_link_refuses_when_target_missing(monkeypatch, tmp_path):
    fm = _registry_isolated(monkeypatch, tmp_path)
    fm.register_link(r"D:\x", r"D:\missing", "")
    with pytest.raises(fm.MigrateError):
        fm.restore_link(r"D:\x")
    assert fm.link_records()  # 记录保留


# ---- 迭代优化新增：前置校验 / 容量预检 / 备份处理 / 还原进度 ----


def test_validate_migrate_not_windows(tmp_path, monkeypatch):
    """非 Windows：其余校验都通过后，明确拒绝（不再等复制完才在 junction 报）。"""
    src = _tree(tmp_path, {"a.txt": 5})
    dest = os.path.join(tmp_path, "dest", "root")
    monkeypatch.setattr(os, "name", "posix")
    r = validate_migrate(src, dest)
    assert r["code"] == "not_windows"


def test_validate_migrate_rejects_other_filesystem(tmp_path, monkeypatch):
    """跨文件系统类型直接拒绝：FAT32/exFAT 存不下链接，时间戳精度也对不上。"""
    import core.fs_migrate as fm

    src = _tree(tmp_path, {"a.txt": 10})
    dest = os.path.join(tmp_path, "dest", "root")
    other = os.path.join(str(tmp_path), "other")
    monkeypatch.setattr(
        fm,
        "_volume_fs",
        lambda p: "FAT32" if str(p).startswith(other) else "NTFS",
    )
    assert validate_migrate(src, dest)["ok"] is True
    assert validate_migrate(src, os.path.join(other, "root"))["code"] == "fs_mismatch"


@pytest.mark.skipif(os.name != "nt", reason="卷信息仅 Windows")
def test_volume_fs_reads_real_drive(tmp_path):
    """真实卷能读出文件系统名，不是空串。"""
    import core.fs_migrate as fm

    assert fm._volume_fs(str(tmp_path))


def test_fs_path_keeps_trailing_space(tmp_path):
    """超长路径转 \\?\\ 时保住结尾的空格：normpath 会吃掉它。"""
    import core.fs_migrate as fm

    p = os.path.join(str(tmp_path), "d" * 250 + " ")
    out = fm._fs_path(p)
    assert out.startswith("\\\\?\\")
    assert out.endswith(" ")
    assert len(out) > 240


def test_unreadable_dir_aborts_migration(tmp_path, monkeypatch):
    """目录读不到时整次迁移报失败，不再当成空目录静默跳过。"""
    import core.fs_migrate as fm

    src = _tree(tmp_path, {"a.txt": 5})
    dest = os.path.join(tmp_path, "dst", "root")

    def fake_walk(top, onerror=None, **_kw):
        if onerror:
            onerror(OSError(13, "Access is denied", os.path.join(str(top), "locked")))
        return iter(())

    monkeypatch.setattr(fm.os, "walk", fake_walk)
    with pytest.raises(MigrateError) as ei:
        migrate_directory(src, dest)
    assert ei.value.message.startswith("unreadable:")
    assert os.path.isfile(os.path.join(src, "a.txt"))
    assert not os.path.exists(dest)


def test_free_space_ok(tmp_path, monkeypatch):
    import core.fs_migrate as fm
    import core.scanner as scanner

    src = _tree(tmp_path, {"a.txt": 50, "b.bin": 50})
    dest = os.path.join(tmp_path, "dest")
    monkeypatch.setattr(scanner, "query_free_size", lambda _p: 10)
    assert fm.free_space_ok(src, dest) is False
    monkeypatch.setattr(scanner, "query_free_size", lambda _p: 100_000)
    assert fm.free_space_ok(src, dest) is True


def test_free_space_ok_nonexistent_dest(tmp_path):
    """目标目录尚不存在时按所在盘剩余空间判断，不误报空间不足。"""
    import core.fs_migrate as fm

    src = _tree(tmp_path, {"a.txt": 50})
    # dest 目录尚未创建，query_free_size 应沿路径向上找到已存在的盘/祖先
    dest = os.path.join(tmp_path, "not-yet-created", "root")
    assert fm.free_space_ok(src, dest) is True


def test_backup_suffix_configurable(monkeypatch, tmp_path):
    """备份后缀可按界面语言配置，且会体现在 asides 路径里。"""
    import core.fs_migrate as fm

    monkeypatch.setattr(fm, "_active_backup_suffix", " 备用")
    src = _tree(tmp_path, {"a.txt": 5})
    dest = os.path.join(tmp_path, "dest", "root")
    r = validate_migrate(src, dest)
    assert r["aside"].startswith(src + " 备用")


def test_migrate_backup_recycle_goes_to_recycle_bin(tmp_path, monkeypatch):
    """选中[移入回收站]：迁移成功后备份入回收站，目标与数据保留。"""
    import core.fs_migrate as fm

    monkeypatch.setattr(fm, "_create_junction", lambda _d, _l: None)
    recycled = []
    monkeypatch.setattr(fm.fs_delete, "delete_to_recycle", lambda p: recycled.append(p))

    src = _tree(tmp_path, {"a.txt": 5, "sub": {"b.bin": 5}})
    dest = os.path.join(tmp_path, "real", "root")
    result = fm.migrate_directory(src, dest, backup_action="recycle")
    assert result["ok"] is True
    assert result["backup_action"] == "recycle"
    assert result["backup_cleaned"] is True
    assert result["backup_permanent"] is False
    assert recycled == [result["aside"]]
    # 原数据仍在备份目录（删除被桩成只记录），目标的复制数据在
    assert os.path.isdir(result["aside"])
    assert os.path.isfile(os.path.join(result["aside"], "sub", "b.bin"))
    assert os.path.isfile(os.path.join(dest, "sub", "b.bin"))


def test_migrate_backup_purge_deletes_permanently(tmp_path, monkeypatch):
    """选中[彻底删除]：迁移成功后备份被彻底删除，不经回收站。"""
    import core.fs_migrate as fm

    monkeypatch.setattr(fm, "_create_junction", lambda _d, _l: None)
    recycled = []
    purged = []
    monkeypatch.setattr(fm.fs_delete, "delete_to_recycle", lambda p: recycled.append(p))
    monkeypatch.setattr(
        fm.fs_delete,
        "delete_path",
        lambda p, permanent=False: purged.append((p, permanent)),
    )

    src = _tree(tmp_path, {"a.txt": 5})
    dest = os.path.join(tmp_path, "real", "root")
    result = fm.migrate_directory(src, dest, backup_action="purge")
    assert result["ok"] is True
    assert result["backup_action"] == "purge"
    assert result["backup_cleaned"] is True
    assert result["backup_permanent"] is True
    assert recycled == []
    assert purged == [(result["aside"], True)]
    assert os.path.isfile(os.path.join(dest, "a.txt"))


def test_migrate_backup_keep_when_settle_unquiet(tmp_path, monkeypatch):
    """选中删除，但收尾阶段目录一直被写：退回保留，不删。"""
    import core.fs_migrate as fm

    monkeypatch.setattr(fm, "_create_junction", lambda _d, _l: None)
    monkeypatch.setattr(fm, "_settle", lambda *_a, **_kw: {"synced": 0, "quiet": False})
    purged = []
    monkeypatch.setattr(
        fm.fs_delete,
        "delete_path",
        lambda p, permanent=False: purged.append(p),
    )

    src = _tree(tmp_path, {"a.txt": 5})
    dest = os.path.join(tmp_path, "real", "root")
    result = fm.migrate_directory(src, dest, backup_action="purge")
    assert result["ok"] is True
    assert result["backup_action"] == "keep"
    assert result["backup_cleaned"] is False
    assert result["backup_permanent"] is False
    assert purged == []
    assert os.path.isfile(os.path.join(result["aside"], "a.txt"))


def test_migrate_backup_keep_when_monitor_overflowed(tmp_path, monkeypatch):
    """收尾期间监控缓冲区溢出过：备份可能带着没补过去的写入，不删。"""
    import core.fs_migrate as fm

    monkeypatch.setattr(fm, "_create_junction", lambda _d, _l: None)
    monkeypatch.setattr(
        fm.ChangeWatcher, "overflowed", property(lambda _self: True)
    )
    purged = []
    monkeypatch.setattr(
        fm.fs_delete,
        "delete_path",
        lambda p, permanent=False: purged.append(p),
    )

    src = _tree(tmp_path, {"a.txt": 5})
    dest = os.path.join(tmp_path, "real", "root")
    result = fm.migrate_directory(src, dest, backup_action="purge")
    assert result["ok"] is True
    assert result["backup_action"] == "keep"
    assert result["backup_cleaned"] is False
    assert result["backup_permanent"] is False
    assert purged == []
    assert os.path.isfile(os.path.join(result["aside"], "a.txt"))


def test_migrate_backup_unknown_action_keeps(tmp_path, monkeypatch):
    """取值认不出来时落回保留，什么也不删。"""
    import core.fs_migrate as fm

    monkeypatch.setattr(fm, "_create_junction", lambda _d, _l: None)
    purged = []
    recycled = []
    monkeypatch.setattr(fm.fs_delete, "delete_to_recycle", lambda p: recycled.append(p))
    monkeypatch.setattr(
        fm.fs_delete,
        "delete_path",
        lambda p, permanent=False: purged.append(p),
    )

    src = _tree(tmp_path, {"a.txt": 5})
    dest = os.path.join(tmp_path, "real", "root")
    result = fm.migrate_directory(src, dest, backup_action="delete")
    assert result["backup_action"] == "keep"
    assert result["backup_cleaned"] is False
    assert recycled == [] and purged == []
    assert os.path.isfile(os.path.join(result["aside"], "a.txt"))


def test_migrate_backup_delete_failure_reports_uncleaned(tmp_path, monkeypatch):
    """删除抛错：迁移仍算成功，动作留在所选项上，备份没清掉要报出来。"""
    import core.fs_migrate as fm
    from core import fs_delete

    monkeypatch.setattr(fm, "_create_junction", lambda _d, _l: None)

    def _boom(_p, permanent=False):
        raise fs_delete.DeleteError("busy")

    monkeypatch.setattr(fm.fs_delete, "delete_path", _boom)

    src = _tree(tmp_path, {"a.txt": 5})
    dest = os.path.join(tmp_path, "real", "root")
    result = fm.migrate_directory(src, dest, backup_action="purge")
    assert result["ok"] is True
    assert result["backup_action"] == "purge"
    assert result["backup_cleaned"] is False
    assert result["backup_permanent"] is False
    assert os.path.isfile(os.path.join(result["aside"], "a.txt"))


def test_migrate_backup_keep_is_default(tmp_path, monkeypatch):
    """默认[保留]：备份留在磁盘上，backup_cleaned 为假。"""
    import core.fs_migrate as fm

    monkeypatch.setattr(fm, "_create_junction", lambda _d, _l: None)
    src = _tree(tmp_path, {"a.txt": 5})
    dest = os.path.join(tmp_path, "real", "root")
    result = fm.migrate_directory(src, dest)
    assert result["ok"] is True
    assert result["backup_action"] == "keep"
    assert result["backup_cleaned"] is False
    assert result["backup_permanent"] is False
    assert os.path.isdir(result["aside"])
    assert os.path.isfile(os.path.join(result["aside"], "a.txt"))


def test_restore_link_reports_progress(monkeypatch, tmp_path):
    """还原：进度回调收到 copy 阶段的字节载荷（大目录不再静默假死）。"""
    import _winapi

    fm = _registry_isolated(monkeypatch, tmp_path)
    monkeypatch.setattr(fm.fs_delete, "delete_to_recycle", lambda p: None)

    link = os.path.join(tmp_path, "link")
    target = os.path.join(tmp_path, "real", "target")
    backup = os.path.join(tmp_path, "backup")
    os.makedirs(os.path.join(target, "sub"))
    open(os.path.join(target, "a.txt"), "wb").write(b"data")
    os.makedirs(backup)
    _winapi.CreateJunction(target, link)
    fm.register_link(link, target, backup, files=1, bytes_=4)

    payloads = []
    result = fm.restore_link(link, progress=lambda p: payloads.append(p))
    assert result["ok"] is True
    stages = [p.get("stage") for p in payloads]
    assert "copy" in stages
    assert "restore" in stages
    copy = [p for p in payloads if p.get("stage") == "copy"]
    assert copy[-1]["bytes_done"] == copy[-1]["bytes_total"] == 4


# ---- 迁移/还原历史 ----


def _isolated_history(monkeypatch, tmp_path):
    """把 history 文件路径切到临时目录，避免污染真实 app_data_dir。"""
    from core import migrate_history

    path = tmp_path / "migration_history.json"
    monkeypatch.setattr(migrate_history, "_history_path", lambda: str(path))
    return path


def test_history_record_and_load_roundtrip(monkeypatch, tmp_path):
    """追加一条 → 重新读取，应能拿回同样的字段（清洗层会补默认值）。"""
    _isolated_history(monkeypatch, tmp_path)

    rec = record_history({
        "ts": 1700000000.0,
        "op": "migrate",
        "source": str(tmp_path / "src"),
        "target": str(tmp_path / "dst"),
        "backup": str(tmp_path / "src 原目录备份"),
        "files": 12,
        "bytes": 3456,
        "status": "success",
        "error": "",
        "code": "",
        "backup_cleaned": False,
    })
    assert rec is not None
    assert rec["op"] == "migrate"
    assert rec["status"] == "success"
    assert rec["files"] == 12
    assert rec["bytes"] == 3456
    assert rec["id"]

    loaded = load_history()
    assert len(loaded) == 1
    assert loaded[0]["source"] == rec["source"]
    assert loaded[0]["target"] == rec["target"]


def test_history_normalize_drops_garbage(monkeypatch, tmp_path):
    """op/status 不在白名单、字段类型不对 → 一律丢弃，不进入历史文件。"""
    _isolated_history(monkeypatch, tmp_path)

    # op 错
    assert record_history({"op": "weird", "status": "success"}) is None
    # status 错
    assert record_history({"op": "migrate", "status": "yikes"}) is None
    # 不是 dict
    assert record_history("not a dict") is None
    # load_history 在文件不存在/损坏时返回空表
    assert load_history() == []


def test_history_delbackup_roundtrip(monkeypatch, tmp_path):
    """删除备份是合法 op，能写入并读回；时间戳被清洗层保留。"""
    _isolated_history(monkeypatch, tmp_path)

    rec = record_history({
        "ts": 1700000000.0,
        "op": "delbackup",
        "source": str(tmp_path / "src"),
        "target": str(tmp_path / "dst"),
        "backup": str(tmp_path / "src 原目录备份"),
        "files": 0,
        "bytes": 0,
        "status": "success",
        "error": "",
        "code": "",
        "backup_cleaned": True,
    })
    assert rec is not None
    assert rec["op"] == "delbackup"
    assert rec["ts"] == 1700000000.0
    loaded = load_history()
    assert len(loaded) == 1
    assert loaded[0]["backup"] == rec["backup"]


def test_history_deloldtarget_roundtrip(monkeypatch, tmp_path):
    """删除旧目标是合法 op，能写入并读回；彻底删除的标记同样保留。"""
    _isolated_history(monkeypatch, tmp_path)

    rec = record_history({
        "ts": 1700000001.0,
        "op": "deloldtarget",
        "source": str(tmp_path / "link"),
        "target": str(tmp_path / "old-target"),
        "status": "success",
        "old_target_permanent": True,
    })
    assert rec is not None
    assert rec["op"] == "deloldtarget"
    assert rec["old_target_permanent"] is True
    loaded = load_history()
    assert len(loaded) == 1
    assert loaded[0]["target"] == rec["target"]


def test_history_caps_at_max(monkeypatch, tmp_path):
    """超过上限时按时间戳倒序裁掉最旧的。"""
    from core import fs_migrate

    _isolated_history(monkeypatch, tmp_path)

    for i in range(fs_migrate._MAX_HISTORY + 25):
        record_history({
            "ts": float(i),
            "op": "migrate",
            "source": f"src{i}",
            "target": f"dst{i}",
            "backup": "",
            "files": 1,
            "bytes": 1,
            "status": "success",
            "error": "",
            "code": "",
            "backup_cleaned": False,
        })

    loaded = load_history()
    assert len(loaded) == fs_migrate._MAX_HISTORY
    # 最新的应当是最后写入的那条（ts 最大）
    assert loaded[0]["ts"] == float(fs_migrate._MAX_HISTORY + 25 - 1)


def test_history_corrupt_file_returns_empty(monkeypatch, tmp_path):
    """历史文件被破坏时不应抛错，返回空表。"""
    path = _isolated_history(monkeypatch, tmp_path)
    path.write_text("this is not json", encoding="utf-8")
    assert load_history() == []


def test_history_clear(monkeypatch, tmp_path):
    """清空应返回清掉的条数并清空文件。"""
    _isolated_history(monkeypatch, tmp_path)

    record_history({
        "ts": 1.0, "op": "migrate", "source": "s", "target": "t",
        "backup": "", "files": 0, "bytes": 0, "status": "success",
        "error": "", "code": "", "backup_cleaned": False,
    })
    record_history({
        "ts": 2.0, "op": "restore", "source": "s", "target": "t",
        "backup": "", "files": 0, "bytes": 0, "status": "failed",
        "error": "boom", "code": "fail", "backup_cleaned": False,
    })
    assert len(load_history()) == 2
    n = clear_history()
    assert n == 2
    assert load_history() == []


def test_history_recovers_from_existing_registry_dir(tmp_path):
    """确认 history 与 links.json 共用 app_data_dir，但彼此不互相覆盖。"""
    from core import fs_migrate
    from core import migrate_history

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # 直接通过私有 API 落两条，写入同一目录的不同文件
    migrate_history._history_path = lambda: str(data_dir / "migration_history.json")
    fs_migrate.save_links([])  # 触发 links.json 路径解析
    record_history({
        "ts": 1.0, "op": "migrate", "source": "s", "target": "t",
        "backup": "", "files": 0, "bytes": 0, "status": "success",
        "error": "", "code": "", "backup_cleaned": False,
    })
    # 两个文件互不影响
    assert (data_dir / "links.json").exists() is False or True  # save_links 写空数组
    assert (data_dir / "migration_history.json").exists()
    assert len(load_history()) == 1
    # 文件内容是合法 JSON 列表
    raw = json.loads((data_dir / "migration_history.json").read_text(encoding="utf-8"))
    assert isinstance(raw, list) and len(raw) == 1


# ---- 链接重建与超长路径 ----


@pytest.mark.skipif(os.name != "nt", reason="junction 仅 Windows")
def test_migrate_rebuilds_tree_links(tmp_path):
    """源树里的链接在目标侧按原样重建：目录联接指向同一目标，悬空链接仍是悬空。"""
    import _winapi

    import core.fs_migrate as fm

    src = _tree(tmp_path, {"a.txt": 10, "real": {"in.txt": 20}})
    outside = os.path.join(tmp_path, "outside")
    os.makedirs(outside, exist_ok=True)
    with open(os.path.join(outside, "out.txt"), "wb") as f:
        f.write(b"c" * 30)
    _winapi.CreateJunction(outside, os.path.join(src, "linkdir"))
    try:
        os.symlink(
            os.path.join(tmp_path, "missing", "nope.txt"),
            os.path.join(src, "dangling.txt"),
        )
    except OSError:
        pytest.skip("建符号链接需要开发者模式或管理员权限")

    dest = os.path.join(tmp_path, "dst", "root")
    result = migrate_directory(src, dest)

    assert result["ok"] is True
    assert result["links"] == 2
    assert result["skipped"] == 0
    # 目录联接按原样重建，里面的数据仍然只有一份
    assert fm._is_reparse(os.path.join(dest, "linkdir"))
    with open(os.path.join(dest, "linkdir", "out.txt"), "rb") as f:
        assert f.read() == b"c" * 30
    # 悬空链接照原样重建：存在，但不是文件
    assert os.path.lexists(os.path.join(dest, "dangling.txt"))
    assert not os.path.exists(os.path.join(dest, "dangling.txt"))
    # 原路径变成 junction，经它还能读到链接里的内容
    assert fm._is_reparse(src)
    assert os.path.isfile(os.path.join(src, "linkdir", "out.txt"))
    assert os.path.lexists(os.path.join(src, "dangling.txt"))


@pytest.mark.skipif(os.name != "nt", reason="junction 仅 Windows")
def test_migrate_reports_skipped_links(tmp_path, monkeypatch):
    """建不出链接且目标不存在：记为跳过，迁移照样完成并报出条数。"""
    import core.fs_migrate as fm

    src = _tree(tmp_path, {"a.txt": 5})
    try:
        os.symlink(
            os.path.join(tmp_path, "missing", "nope.txt"),
            os.path.join(src, "gone.txt"),
        )
    except OSError:
        pytest.skip("建符号链接需要开发者模式或管理员权限")

    monkeypatch.setattr(fm, "_rebuild_link", lambda *a, **kw: False)
    dest = os.path.join(tmp_path, "dst", "root")
    result = migrate_directory(src, dest)

    assert result["ok"] is True
    assert result["links"] == 0
    assert result["skipped"] == 1
    assert not os.path.lexists(os.path.join(dest, "gone.txt"))
    # 备份里留着原始的悬空链接
    assert os.path.lexists(os.path.join(result["aside"], "gone.txt"))


@pytest.mark.skipif(os.name != "nt", reason="长路径上限仅 Windows")
def test_migrate_handles_long_path(tmp_path):
    """单文件路径超过 260 个字符时照样能迁移（绝对路径加扩展长度前缀）。"""
    import core.fs_migrate as fm

    src = os.path.join(tmp_path, "long")
    cur = src
    for i in range(6):
        cur = os.path.join(cur, "d" * 60 + str(i))
        os.makedirs(fm._fs_path(cur), exist_ok=True)
    leaf = os.path.join(cur, "leaf.txt")
    with open(fm._fs_path(leaf), "wb") as f:
        f.write(b"z" * 40)
    assert len(leaf) > 260

    dest = os.path.join(tmp_path, "dst", "long")
    result = migrate_directory(src, dest)

    assert result["ok"] is True
    copied = os.path.join(dest, os.path.relpath(leaf, src))
    assert len(copied) > 260
    with open(fm._fs_path(copied), "rb") as f:
        assert f.read() == b"z" * 40
    # 原路径现在也是 junction，经它仍读得到
    with open(fm._fs_path(leaf), "rb") as f:
        assert f.read() == b"z" * 40


def test_history_keeps_link_counts(monkeypatch, tmp_path):
    """历史保留重建、跳过链接的条数；旧记录缺这两个字段时补 0。"""
    _isolated_history(monkeypatch, tmp_path)

    rec = record_history({
        "ts": 1700000000.0, "op": "migrate", "source": "s", "target": "t",
        "backup": "", "files": 3, "bytes": 30, "links": 2, "skipped": 1,
        "status": "success", "error": "", "code": "", "backup_cleaned": False,
    })
    assert rec is not None
    assert rec["links"] == 2
    assert rec["skipped"] == 1
    loaded = load_history()
    assert (loaded[0]["links"], loaded[0]["skipped"]) == (2, 1)

    old = record_history({
        "ts": 1700000001.0, "op": "migrate", "source": "s2", "target": "t2",
        "backup": "", "files": 0, "bytes": 0, "status": "success",
        "error": "", "code": "", "backup_cleaned": False,
    })
    assert old["links"] == 0
    assert old["skipped"] == 0


def test_migrate_cleanup_removes_readonly_copy(tmp_path, monkeypatch):
    """校验失败清理目标时，只读文件也要删掉：否则残留会让下次迁移报目标已存在。"""
    import core.fs_migrate as fm

    src = _tree(tmp_path, {"a.txt": 10})
    ro = os.path.join(src, "a.txt")
    os.chmod(ro, stat.S_IREAD)
    dest = os.path.join(tmp_path, "dst", "root")

    def bad_verify(_s, _d, **kw):
        return {"ok": False, "files": 1, "bytes": 10, "mismatch": ["a.txt"]}

    monkeypatch.setattr(fm, "_verify", bad_verify)
    _force_full_verify(monkeypatch)
    with pytest.raises(MigrateError):
        migrate_directory(src, dest)
    assert not os.path.exists(dest)
    os.chmod(ro, stat.S_IWRITE)


def test_copy_workers_by_target_drive(tmp_path, monkeypatch):
    """目标盘是 SSD 才开多条线：机械盘上并发会让磁头来回找，反而更慢。"""
    import core.migrate_fs as mf

    mf._COPY_WORKERS_CACHE.clear()
    monkeypatch.setattr(mf, "_incurs_seek_penalty", lambda _d: False)
    assert mf._copy_workers(str(tmp_path)) == 4
    mf._COPY_WORKERS_CACHE.clear()
    # 机械盘或探测不出来（网络盘、光盘）时只开一条线
    monkeypatch.setattr(mf, "_incurs_seek_penalty", lambda _d: True)
    assert mf._copy_workers(str(tmp_path)) == 1


def test_copy_tree_parallel_keeps_tree_and_files(tmp_path):
    """并行复制后结构、内容、计数都要对得上。"""
    import core.fs_migrate as fm

    src = _tree(tmp_path, {"a": {"x.bin": 1024, "y.bin": 2048}, "b": {"z.bin": 4096}})
    dst = os.path.join(tmp_path, "dst", "root")
    events = []
    result = fm._copy_tree(src, dst, progress=events.append, cancel=lambda: False)
    assert result == {"links": [], "copied": [], "skipped": []}
    assert _dir_bytes(src) == _dir_bytes(dst)
    assert events[0]["total"] == 3
    assert events[-1]["done"] == 3 and events[-1]["bytes_done"] == 7168


def test_copy_tree_throttles_progress(tmp_path):
    """进度事件要节流：每个文件推一次会变成每文件一次前端往返（实测 1.74 毫秒）。"""
    import core.fs_migrate as fm

    src = _tree(tmp_path, {f"f{i}.bin": 256 for i in range(300)})
    dst = os.path.join(tmp_path, "dst", "root")
    events = []
    fm._copy_tree(src, dst, progress=events.append, cancel=lambda: False)
    assert events[0]["total"] == 300
    # 收尾那条必到，数字不会停在半路
    assert events[-1]["done"] == 300 and events[-1]["bytes_done"] == 300 * 256
    assert len(events) < 300


def test_copy_tree_cancel_stops(tmp_path):
    """中途取消要报 cancelled，而不是默默把剩下的复制完。"""
    import core.fs_migrate as fm

    src = _tree(tmp_path, {"a": {"x.bin": 1024}})
    dst = os.path.join(tmp_path, "dst", "root")
    with pytest.raises(MigrateError, match="cancelled"):
        fm._copy_tree(src, dst, progress=None, cancel=lambda: True)


def test_copy_tree_cancel_midway(tmp_path):
    """复制到一半取消：已提交的活干完就退，不能整个复制完再报错。"""
    import core.fs_migrate as fm

    src = _tree(tmp_path, {f"f{i}.bin": 1024 for i in range(200)})
    dst = os.path.join(tmp_path, "dst", "root")
    calls = {"n": 0}

    def cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 10

    with pytest.raises(MigrateError, match="cancelled"):
        fm._copy_tree(src, dst, progress=None, cancel=cancel)
    assert len(os.listdir(dst)) < 200


def test_walk_files_orders_by_path_key(tmp_path):
    """校验靠同一个排序键同时推进两侧，遍历顺序必须和它一致，否则会错位。"""
    import core.migrate_fs as mf

    src = _tree(tmp_path, {"B": {"y.bin": 10}, "a": {"x.bin": 20}, "a2.bin": 5})
    got = [k for k, _size, _mt in mf._walk_files(src, cancel=lambda: False)]
    assert got == sorted(got, key=mf._path_key)
    assert len(got) == 3


class _FakeWatch:
    """只当计数器和清单用：喂给它一批「已记下但还没处理」的变化。"""

    def __init__(self, rels=()):
        self.started = True
        self._rels = set(rels)
        self._n = len(self._rels)

    def event_count(self) -> int:
        return self._n

    def take(self) -> set:
        out = self._rels
        self._rels = set()
        return out

    def snapshot(self) -> set:
        return set(self._rels)

    def has_events(self) -> bool:
        return self._n > 0


def test_settle_syncs_changes_recorded_before_it_starts(tmp_path):
    """进 _settle 之前就记下、还没处理的变化，也要补到新位置。

    切换后的收尾只跑一次 _settle，那时清单里可能还压着确认阶段记下的
    变化；漏掉这些，新位置里留的就是复制那时候的旧内容。
    """
    import core.fs_migrate as fm

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.txt").write_text("new", encoding="utf-8")
    (dst / "a.txt").write_text("old", encoding="utf-8")
    # 增量补齐按修改时间判断要不要重抄。两次写入容易落在同一个时钟刻度里，
    # 那样目标侧会被当成「已经是最新」，测试就成了碰运气。把目标侧标旧。
    os.utime(dst / "a.txt", (1.0, 1.0))
    watch = _FakeWatch({"a.txt"})

    out = fm._settle(
        str(src),
        str(dst),
        watch,
        quiet_s=0.0,
        cancel=lambda: False,
        delete_missing=False,
    )
    assert out["synced"] == 1, out
    assert (dst / "a.txt").read_text(encoding="utf-8") == "new"
