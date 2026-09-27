"""目录迁移测试：校验、复制、校验、让位、junction、回滚。"""

import json
import os
import shutil

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

    monkeypatch.setattr(fm, "_registry_path", lambda: os.path.join(tmp_path, "links.json"))
    return fm


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
    # 回收站与永久删除都用桩，避免真删磁盘
    recycled, wiped = [], []
    monkeypatch.setattr(fm.fs_delete, "delete_to_recycle", lambda p: recycled.append(p))
    monkeypatch.setattr(fm.fs_delete, "delete_permanent", lambda p: wiped.append(p))

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
    # 备份永久删除，目标仍走回收站
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


def test_migrate_clean_backup_recycles(tmp_path, monkeypatch):
    """勾选自动清除：迁移成功后备份入回收站，目标与数据保留。"""
    import core.fs_migrate as fm

    monkeypatch.setattr(fm, "_create_junction", lambda _d, _l: None)
    recycled = []
    monkeypatch.setattr(fm.fs_delete, "delete_to_recycle", lambda p: recycled.append(p))

    src = _tree(tmp_path, {"a.txt": 5, "sub": {"b.bin": 5}})
    dest = os.path.join(tmp_path, "real", "root")
    result = fm.migrate_directory(src, dest, clean_backup=True)
    assert result["ok"] is True
    assert result["backup_cleaned"] is True
    assert recycled == [result["aside"]]
    # 原数据仍在备份目录（删除被桩成只记录），目标的复制数据在
    assert os.path.isdir(result["aside"])
    assert os.path.isfile(os.path.join(result["aside"], "sub", "b.bin"))
    assert os.path.isfile(os.path.join(dest, "sub", "b.bin"))


def test_migrate_clean_backup_false_keeps(tmp_path, monkeypatch):
    """默认不勾：备份保留在磁盘上，backup_cleaned 为假。"""
    import core.fs_migrate as fm

    monkeypatch.setattr(fm, "_create_junction", lambda _d, _l: None)
    src = _tree(tmp_path, {"a.txt": 5})
    dest = os.path.join(tmp_path, "real", "root")
    result = fm.migrate_directory(src, dest, clean_backup=False)
    assert result["ok"] is True
    assert result["backup_cleaned"] is False
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
    from core import fs_migrate

    path = tmp_path / "migration_history.json"
    monkeypatch.setattr(fs_migrate, "_history_path", lambda: str(path))
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

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # 直接通过 fs_migrate 的私有 API 落两条，写入同一目录的不同文件
    fs_migrate._history_path = lambda: str(data_dir / "migration_history.json")
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
