"""安全删除与黑名单匹配。"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from core import fs_delete


def test_is_drive_root():
    if os.name == "nt":
        assert fs_delete.is_drive_root("C:\\") is True
        assert fs_delete.is_drive_root("C:/") is True
        assert fs_delete.is_drive_root("C:\\Windows") is False
        assert fs_delete.is_drive_root("D:\\foo") is False
    else:
        assert fs_delete.is_drive_root("/") is True
        assert fs_delete.is_drive_root("/tmp") is False


def test_is_under_root(tmp_path: Path):
    root = str(tmp_path)
    child = str(tmp_path / "a" / "b.txt")
    os.makedirs(tmp_path / "a", exist_ok=True)
    (tmp_path / "a" / "b.txt").write_text("x", encoding="utf-8")
    assert fs_delete.is_under_root(root, child) is True
    assert fs_delete.is_under_root(root, root) is True
    # 兄弟前缀不应命中：WindowsOld vs Windows
    if os.name == "nt":
        assert fs_delete.is_under_root(r"C:\Win", r"C:\Windows") is False
    outside = str(tmp_path.parent / "other")
    assert fs_delete.is_under_root(root, outside) is False


def test_resolve_and_assert_blocks_root(tmp_path: Path):
    root = str(tmp_path)
    with pytest.raises(fs_delete.DeleteError) as ei:
        fs_delete.assert_deletable(root, "", [])
    assert ei.value.message == "root"

    # 子文件可解析
    f = tmp_path / "f.txt"
    f.write_text("1", encoding="utf-8")
    full = fs_delete.assert_deletable(root, "f.txt", [])
    assert os.path.normcase(full) == os.path.normcase(str(f))


def test_assert_outside_via_dotdot(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    # .. 逃出 root
    with pytest.raises(fs_delete.DeleteError) as ei:
        fs_delete.assert_deletable(str(root), "..", [])
    assert ei.value.message in ("outside", "root", "drive_root")


def test_blacklist_exact_prefix_regex(tmp_path: Path):
    root = str(tmp_path)
    keep = tmp_path / "keep"
    keep.mkdir()
    nested = keep / "x.txt"
    nested.write_text("z", encoding="utf-8")
    other = tmp_path / "other.txt"
    other.write_text("o", encoding="utf-8")

    bl_exact = [{"path": str(keep), "mode": "exact"}]
    assert fs_delete.path_matches_blacklist(str(keep), bl_exact) is True
    assert fs_delete.path_matches_blacklist(str(nested), bl_exact) is False

    bl_pref = [{"path": str(keep), "mode": "prefix"}]
    assert fs_delete.path_matches_blacklist(str(keep), bl_pref) is True
    assert fs_delete.path_matches_blacklist(str(nested), bl_pref) is True
    assert fs_delete.path_matches_blacklist(str(other), bl_pref) is False

    # prefix 不误伤 Windows vs WindowsOld 风格
    if os.name == "nt":
        bl_win = [{"path": r"C:\Windows", "mode": "prefix"}]
        assert fs_delete.path_matches_blacklist(r"C:\Windows\System32", bl_win)
        assert not fs_delete.path_matches_blacklist(r"C:\WindowsOld\x", bl_win)

    bl_re = [{"path": r".*other\.txt$", "mode": "regex"}]
    assert fs_delete.path_matches_blacklist(str(other), bl_re) is True
    assert fs_delete.path_matches_blacklist(str(nested), bl_re) is False


def test_blacklist_case_insensitive_on_windows():
    if os.name != "nt":
        pytest.skip("Windows only")
    bl = [{"path": r"C:\Windows", "mode": "prefix"}]
    assert fs_delete.path_matches_blacklist(r"c:\windows\system32", bl)


def test_normalize_delete_blacklist():
    raw = [
        {"path": r"C:\Windows", "mode": "prefix"},
        {"path": r"C:\Windows", "mode": "prefix"},  # dup
        {"path": "", "mode": "exact"},
        {"path": r"C:\Temp", "mode": "exact"},
        "D:\\Games",  # legacy string → prefix
        {"path": "(", "mode": "regex"},  # invalid regex drop
    ]
    out = fs_delete.normalize_delete_blacklist(raw)
    paths = [(e["path"], e["mode"]) for e in out]
    assert (r"C:\Windows", "prefix") in paths
    assert (r"C:\Temp", "exact") in paths
    assert any(p.endswith("Games") or "Games" in p for p, m in paths)
    assert not any(p == "(" for p, _ in paths)


def test_assert_blacklist(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("1", encoding="utf-8")
    bl = [{"path": str(tmp_path), "mode": "prefix"}]
    with pytest.raises(fs_delete.DeleteError) as ei:
        fs_delete.assert_deletable(str(tmp_path), "a.txt", bl)
    assert ei.value.message == "blacklist"


def test_evaluate_pending_candidate_allows_missing(tmp_path: Path):
    """入队预检默认不因 missing 拒绝。"""
    root = str(tmp_path)
    (tmp_path / "a.txt").write_text("1", encoding="utf-8")
    ok = fs_delete.evaluate_pending_candidate(root, "a.txt", [])
    assert ok["ok"] is True
    assert ok["code"] == "ok"
    assert ok["exists"] is True

    miss = fs_delete.evaluate_pending_candidate(root, "gone.txt", [])
    assert miss["ok"] is True
    assert miss["code"] == "ok"
    assert miss["exists"] is False

    miss_req = fs_delete.evaluate_pending_candidate(
        root, "gone.txt", [], require_exists=True
    )
    assert miss_req["ok"] is False
    assert miss_req["code"] == "missing"


def test_evaluate_pending_candidate_blacklist(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("1", encoding="utf-8")
    bl = [{"path": str(tmp_path), "mode": "prefix"}]
    ev = fs_delete.evaluate_pending_candidate(str(tmp_path), "a.txt", bl)
    assert ev["ok"] is False
    assert ev["code"] == "blacklist"


def test_check_pending_paths_api(tmp_path: Path, monkeypatch):
    """Api.check_pending_paths：白名单拒绝、合法放行、不删文件。"""
    from app import Api
    from core import store

    # set_delete_blacklist → _persist → settings_path → _app_base_dir/_path
    # 必须隔离这两处；只 patch app_data_dir 会把黑名单写进真实 LOCALAPPDATA。
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store, "_app_base_dir_path", lambda: str(data_root))
    monkeypatch.setattr(store, "_app_base_dir", lambda: str(data_root))
    monkeypatch.setattr(store, "app_data_dir", lambda: str(data_root))
    store._data_wiped = False
    store._delete_blacklist = []

    keep = tmp_path / "keep"
    keep.mkdir()
    ok_file = tmp_path / "ok.txt"
    ok_file.write_text("x", encoding="utf-8")

    try:
        store.set_delete_blacklist([{"path": str(keep), "mode": "prefix"}])
        assert store.settings_path().startswith(str(data_root))

        api = Api.__new__(Api)
        res = api.check_pending_paths(
            [
                {"root": str(tmp_path), "rel": "ok.txt", "name": "ok"},
                {"root": str(tmp_path), "rel": "keep", "name": "keep"},
                {"root": str(tmp_path), "rel": "missing.txt", "name": "miss"},
            ]
        )
        assert res.get("ok") is True
        allowed_rels = {r["rel"] for r in res["allowed"]}
        rejected_rels = {r["rel"] for r in res["rejected"]}
        assert "ok.txt" in allowed_rels
        assert "missing.txt" in allowed_rels  # 入队允许 missing
        assert "keep" in rejected_rels
        assert any(r.get("code") == "blacklist" for r in res["rejected"])
        # 未真删
        assert ok_file.is_file()
        assert keep.is_dir()
    finally:
        # 清内存，避免后续未隔离用例 _persist 时带上本测黑名单
        store._delete_blacklist = []


def test_assert_missing(tmp_path: Path):
    with pytest.raises(fs_delete.DeleteError) as ei:
        fs_delete.assert_deletable(str(tmp_path), "nope.txt", [])
    assert ei.value.message == "missing"


def test_delete_permanent_file_and_dir(tmp_path: Path):
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    d = tmp_path / "d"
    d.mkdir()
    (d / "c.txt").write_text("y", encoding="utf-8")

    fs_delete.delete_permanent(str(f))
    assert not f.exists()
    fs_delete.delete_permanent(str(d))
    assert not d.exists()


def test_delete_to_recycle_mocked():
    if os.name != "nt":
        with pytest.raises(fs_delete.DeleteError) as ei:
            fs_delete.delete_to_recycle("C:\\tmp\\x")
        assert ei.value.message == "recycle_unsupported"
        return
    with patch.object(fs_delete, "_shfile_delete") as m:
        fs_delete.delete_to_recycle(r"C:\tmp\x")
        m.assert_called_once()
        args, kwargs = m.call_args
        assert kwargs.get("allow_undo") is True
