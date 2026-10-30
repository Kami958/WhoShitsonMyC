"""安全删除与黑名单匹配。"""

from __future__ import annotations

import os
import stat
import subprocess
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


@pytest.mark.skipif(os.name != "nt", reason="junction 仅 Windows")
def test_delete_permanent_keeps_junction_target(tmp_path: Path):
    """彻底删除不能穿过目录联接：联接本身删掉，它指向的数据一个都不能少。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    root = tmp_path / "root"
    (root / "inside").mkdir(parents=True)
    (root / "inside" / "gone.txt").write_text("y", encoding="utf-8")
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(root / "j"), str(outside)],
        check=True,
        capture_output=True,
    )

    fs_delete.delete_permanent(str(root))

    assert root.exists() is False
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "x"


@pytest.mark.skipif(os.name != "nt", reason="只读属性只影响 Windows 的删除")
def test_delete_permanent_readonly_file(tmp_path: Path):
    f = tmp_path / "03_docx.zip"
    f.write_bytes(b"x")
    os.chmod(f, stat.S_IREAD)

    fs_delete.delete_permanent(str(f))
    assert not f.exists()


@pytest.mark.skipif(os.name != "nt", reason="只读属性只影响 Windows 的删除")
def test_delete_permanent_readonly_tree(tmp_path: Path):
    d = tmp_path / "d"
    (d / "sub").mkdir(parents=True)
    f = d / "03_docx.zip"
    f.write_bytes(b"x")
    os.chmod(f, stat.S_IREAD)
    os.chmod(d / "sub", stat.S_IREAD)
    os.chmod(d, stat.S_IREAD)

    fs_delete.delete_permanent(str(d))
    assert not d.exists()


@pytest.mark.skipif(os.name != "nt", reason="只读属性只影响 Windows 的删除")
def test_delete_permanent_readonly_still_denied(tmp_path: Path, monkeypatch):
    """清属性也压不下去时仍报错，不静默带过。"""
    f = tmp_path / "03_docx.zip"
    f.write_bytes(b"x")
    os.chmod(f, stat.S_IREAD)
    monkeypatch.setattr(os, "chmod", lambda *a, **kw: None)

    with pytest.raises(fs_delete.DeleteError) as ei:
        fs_delete.delete_permanent(str(f))
    assert str(ei.value.message).startswith("denied")
    assert f.exists()


def _patch_recycle_probe(
    monkeypatch,
    *,
    unc: bool = False,
    drive_type: int = 3,
    policy: bool = False,
    nuke: bool = False,
) -> None:
    """把回收站可用性探测换掉：UNC、盘类型、组策略与每卷开关都归测试控制。"""
    monkeypatch.setattr(fs_delete, "_recycle_is_unc", lambda _path: unc)
    monkeypatch.setattr(fs_delete, "_recycle_drive_type", lambda _path: drive_type)
    monkeypatch.setattr(fs_delete, "_recycle_policy_blocks", lambda: policy)
    monkeypatch.setattr(fs_delete, "_recycle_volume_nukes", lambda _path: nuke)


def test_recycle_is_unc_path_forms():
    """共享盘路径与扩展长度写法都算 UNC，本地盘扩展写法不算。"""
    assert fs_delete._recycle_is_unc("\\\\server\\share\\dir") is True
    assert fs_delete._recycle_is_unc("\\\\?\\UNC\\server\\share\\dir") is True
    assert fs_delete._recycle_is_unc("//server/share/dir") is True
    assert fs_delete._recycle_is_unc("C:\\tmp\\x") is False
    assert fs_delete._recycle_is_unc("\\\\?\\C:\\tmp\\x") is False


def test_delete_to_recycle_blocks_unc_or_non_fixed_drive(monkeypatch):
    """共享盘、可移动盘、光驱、内存盘、未知盘上都不得调用 Shell 删除。"""
    if os.name != "nt":
        pytest.skip("回收站只在 Windows 上可用")
    with patch.object(fs_delete, "_shfile_delete") as shell:
        for drive_type in (0, 2, 4, 5, 6):
            _patch_recycle_probe(monkeypatch, drive_type=drive_type)
            with pytest.raises(fs_delete.DeleteError) as ei:
                fs_delete.delete_to_recycle(r"C:\tmp\x")
            assert ei.value.message == "recycle_unavailable"

        _patch_recycle_probe(monkeypatch, unc=True)
        with pytest.raises(fs_delete.DeleteError) as ei:
            fs_delete.delete_to_recycle(r"\\server\share\x")
        assert ei.value.message == "recycle_unavailable"

        shell.assert_not_called()


def test_delete_to_recycle_blocks_policy_disabled(monkeypatch):
    """组策略禁止回收站时同样拒绝，不降级为永久删除。"""
    if os.name != "nt":
        pytest.skip("回收站只在 Windows 上可用")
    with patch.object(fs_delete, "_shfile_delete") as shell:
        _patch_recycle_probe(monkeypatch, drive_type=3, policy=True)
        with pytest.raises(fs_delete.DeleteError) as ei:
            fs_delete.delete_to_recycle(r"C:\tmp\x")
        assert ei.value.message == "recycle_unavailable"
        shell.assert_not_called()


def test_delete_to_recycle_allows_fixed_drive(monkeypatch):
    """固定盘且未被策略禁用时仍走 SHFileOperation，并允许撤销。"""
    if os.name != "nt":
        pytest.skip("回收站只在 Windows 上可用")
    _patch_recycle_probe(monkeypatch, drive_type=3, policy=False)
    with patch.object(fs_delete, "_shfile_delete") as shell:
        fs_delete.delete_to_recycle(r"C:\tmp\x")
        shell.assert_called_once()
        _args, kwargs = shell.call_args
        assert kwargs.get("allow_undo") is True


def test_recycle_unavailable_message_both_languages():
    """recycle_unavailable 的提示说清删除没有执行，并给出彻底删除的做法。"""
    from app import winmsgs
    from core import i18n

    en = winmsgs._delete_error_message("recycle_unavailable")  # noqa: SLF001
    assert "Recycle Bin cannot be used" in en
    assert "nothing was deleted" in en
    assert "permanent delete" in en

    i18n.set_lang("zh")
    zh = winmsgs._delete_error_message("recycle_unavailable")  # noqa: SLF001
    assert zh == "该位置无法使用回收站，删除没有执行。需要彻底删除时，请勾选彻底删除后重试。"


def test_delete_to_recycle_blocks_nuke_on_delete(monkeypatch):
    """卷设置为直接永久删除时，回收站删除必须先拒绝。"""
    if os.name != "nt":
        pytest.skip("回收站只在 Windows 上可用")
    _patch_recycle_probe(monkeypatch, drive_type=3, policy=False, nuke=True)
    with patch.object(fs_delete, "_shfile_delete") as shell:
        with pytest.raises(fs_delete.DeleteError) as ei:
            fs_delete.delete_to_recycle(r"C:\tmp\x")
        assert ei.value.message == "recycle_unavailable"
        shell.assert_not_called()


def test_delete_to_recycle_mocked(monkeypatch):
    if os.name != "nt":
        with pytest.raises(fs_delete.DeleteError) as ei:
            fs_delete.delete_to_recycle("C:\\tmp\\x")
        assert ei.value.message == "recycle_unsupported"
        return
    # 盘类型与组策略在这里固定成可用，用例结果不随跑测试的机器变化
    _patch_recycle_probe(monkeypatch, drive_type=3, policy=False)
    with patch.object(fs_delete, "_shfile_delete") as m:
        fs_delete.delete_to_recycle(r"C:\tmp\x")
        m.assert_called_once()
        args, kwargs = m.call_args
        assert kwargs.get("allow_undo") is True


@pytest.mark.skipif(os.name != "nt", reason="junction 仅 Windows")
def test_delete_permanent_junction_root_keeps_target(tmp_path: Path):
    """根节点就是目录联接时只删联接，不进入目标目录。"""
    real = tmp_path / "real-data"
    real.mkdir()
    (real / "keep.txt").write_text("safe", encoding="utf-8")
    (real / "sub").mkdir()
    (real / "sub" / "nested.txt").write_text("nested", encoding="utf-8")
    link = tmp_path / "junction-root"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(real)],
        check=True,
        capture_output=True,
    )

    fs_delete.delete_permanent(str(link))

    assert not os.path.lexists(str(link))
    assert (real / "keep.txt").read_text(encoding="utf-8") == "safe"
    assert (real / "sub" / "nested.txt").read_text(encoding="utf-8") == "nested"


@pytest.mark.skipif(os.name != "nt", reason="符号链接测试仅 Windows")
def test_delete_permanent_dir_symlink_root_keeps_target(tmp_path: Path):
    """目录符号链接作为根节点时也只删链接。"""
    real = tmp_path / "real-dir"
    real.mkdir()
    (real / "keep.txt").write_text("safe", encoding="utf-8")
    link = tmp_path / "dir-link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"无法创建目录符号链接：{exc}")

    fs_delete.delete_permanent(str(link))

    assert not os.path.lexists(str(link))
    assert (real / "keep.txt").read_text(encoding="utf-8") == "safe"


@pytest.mark.skipif(os.name != "nt", reason="符号链接测试仅 Windows")
def test_delete_permanent_file_symlink_root_keeps_target(tmp_path: Path):
    """文件符号链接作为根节点时只删链接。"""
    target = tmp_path / "real-file.txt"
    target.write_text("safe", encoding="utf-8")
    link = tmp_path / "file-link.txt"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"无法创建文件符号链接：{exc}")

    fs_delete.delete_permanent(str(link))

    assert not os.path.lexists(str(link))
    assert target.read_text(encoding="utf-8") == "safe"


@pytest.mark.skipif(os.name != "nt", reason="重解析点属性仅 Windows")
def test_is_reparse_path_detects_mount_point_tag(monkeypatch):
    """卷挂载点与目录联接同为重解析点，按链接自身属性识别。"""
    class _Stat:
        st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT
        st_reparse_tag = 0xA0000003

    monkeypatch.setattr(fs_delete.os, "lstat", lambda _path: _Stat())
    assert fs_delete._is_reparse_path(r"C:\mounted-volume") is True


def test_delete_permanent_root_reparse_does_not_enter_tree(tmp_path, monkeypatch):
    """根节点被判定为重解析点时走删单项路径，不递归进目录树。"""
    target = tmp_path / "mount-point"
    target.mkdir()
    entered: list[str] = []

    monkeypatch.setattr(fs_delete, "_is_reparse_path", lambda _path: True)
    monkeypatch.setattr(
        fs_delete,
        "_delete_tree",
        lambda *_args, **_kwargs: entered.append("tree"),
    )
    monkeypatch.setattr(
        fs_delete,
        "_unlink_permanent",
        lambda _path: entered.append("link"),
    )

    fs_delete.delete_permanent(str(target), on_progress=None)

    assert entered == ["link"]
    assert target.is_dir()


def _make_long_dir(base_plain: str, min_len: int = 300) -> str:
    """逐层建目录直到总长超过 min_len，返回带扩展长度前缀的路径。

    创建时只能用带前缀的路径，否则 Windows 在 260 字符处就拒绝建目录。
    """
    current = fs_delete._extended_path(base_plain)  # noqa: SLF001
    os.makedirs(current, exist_ok=True)
    while len(current) < min_len:
        current = os.path.join(current, "d" * 30)
        os.makedirs(current, exist_ok=True)
    return current


def _strip_extended(path: str) -> str:
    """去掉扩展长度前缀，得到界面和接口层会传的普通路径。"""
    prefix = "\\\\?\\"
    return path[len(prefix):] if path.startswith(prefix) else path


@pytest.mark.skipif(os.name != "nt", reason="长路径仅 Windows")
def test_extended_path_prefixes_local_and_unc():
    """本地盘加扩展前缀，共享盘换成 UNC 形式，重复调用结果不变。"""
    local = fs_delete._extended_path(r"C:\a\b")  # noqa: SLF001
    assert local == "\\\\?\\C:\\a\\b"
    assert fs_delete._extended_path(local) == local  # noqa: SLF001
    unc = fs_delete._extended_path(r"\\server\share\dir")  # noqa: SLF001
    assert unc == "\\\\?\\UNC\\server\\share\\dir"
    assert fs_delete._extended_path(unc) == unc  # noqa: SLF001


@pytest.mark.skipif(os.name != "nt", reason="长路径仅 Windows")
def test_delete_permanent_long_path_tree(tmp_path):
    """超过 260 字符的深层目录用普通路径调用，整棵树都能删掉。"""
    ext_root = _make_long_dir(str(tmp_path / "deep"))
    leaf = os.path.join(ext_root, "leaf.txt")
    with open(leaf, "w", encoding="utf-8") as fh:
        fh.write("x")
    plain_root = _strip_extended(ext_root)
    assert len(plain_root) > 260

    fs_delete.delete_permanent(plain_root)

    assert not os.path.lexists(ext_root)
    assert not os.path.lexists(plain_root)


@pytest.mark.skipif(os.name != "nt", reason="长路径仅 Windows")
def test_delete_permanent_long_path_link_root_keeps_target(tmp_path):
    """超长路径上的目录链接作为根节点时只删链接，目标内容保留。"""
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("safe", encoding="utf-8")
    ext_root = _make_long_dir(str(tmp_path / "deep"))
    ext_link = os.path.join(ext_root, "link")
    try:
        os.symlink(str(target), ext_link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"无法创建目录符号链接：{exc}")
    plain_link = _strip_extended(ext_link)
    assert len(plain_link) > 260

    fs_delete.delete_permanent(plain_link)

    assert not os.path.lexists(ext_link)
    assert (target / "keep.txt").read_text(encoding="utf-8") == "safe"


def test_recycle_volume_nuke_reads_registry(monkeypatch):
    """每卷 NukeOnDelete 读取缺失值按可用，值为 1 按不可用。"""
    if os.name != "nt":
        pytest.skip("注册表只在 Windows 上可用")
    import ctypes
    import types
    import winreg

    class _VolumeFn:
        argtypes = None
        restype = None

        def __call__(self, root, buf, size):
            buf.value = r"\\?\Volume{test-guid}" + "\\"
            return True

    class FakeKernel32:
        GetVolumeNameForVolumeMountPointW = _VolumeFn()

    monkeypatch.setattr(
        ctypes, "windll", types.SimpleNamespace(kernel32=FakeKernel32()), raising=False
    )

    def missing_key(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(winreg, "OpenKey", missing_key)
    assert fs_delete._recycle_volume_nukes(r"C:\tmp\x") is False

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(winreg, "OpenKey", lambda *_args, **_kwargs: Key())
    monkeypatch.setattr(winreg, "QueryValueEx", lambda *_args, **_kwargs: (1, 4))
    assert fs_delete._recycle_volume_nukes(r"C:\tmp\x") is True


def test_recycle_shell_flags_include_nuke_warning(monkeypatch):
    """回收站调用必须带 FOF_WANTNUKEWARNING，不能静默永久删除。"""
    if os.name != "nt":
        pytest.skip("回收站只在 Windows 上可用")
    import ctypes
    import types

    flags: list[int] = []

    class FakeShell32:
        def SHFileOperationW(self, op):
            flags.append(int(op._obj.fFlags))
            return 0

    monkeypatch.setattr(
        ctypes, "windll", types.SimpleNamespace(shell32=FakeShell32()), raising=False
    )
    fs_delete._shfile_delete(r"C:\tmp\x", allow_undo=True)
    assert flags and (flags[0] & 0x4000)


@pytest.mark.skipif(os.name != "nt", reason="文件占用行为只在 Windows 上可复现")
def test_delete_permanent_busy_file_reports_busy(tmp_path: Path):
    """文件被其他程序占用时，报出占用原因，而不是目录名称无效。"""
    d = tmp_path / "backup"
    d.mkdir()
    f = d / "daemon-memory-diag.log"
    f.write_bytes(b"x")
    holder = open(f, "rb")
    try:
        with pytest.raises(fs_delete.DeleteError) as ei:
            fs_delete.delete_permanent(str(d))
    finally:
        holder.close()
    message = str(ei.value.message)
    assert message.startswith("busy")
    assert "daemon-memory-diag.log" in message
    assert "267" not in message
    assert f.exists()


def test_delete_error_message_names_busy_file():
    """占用与权限失败都给可读文案，并带上出错文件名。"""
    from app import winmsgs

    busy = winmsgs._delete_error_message("busy:daemon-memory-diag.log")  # noqa: SLF001
    assert "daemon-memory-diag.log" in busy
    assert "in use" in busy
    assert "WinError" not in busy

    denied = winmsgs._delete_error_message("denied:03_docx.zip")  # noqa: SLF001
    assert "03_docx.zip" in denied
    assert "WinError" not in denied
