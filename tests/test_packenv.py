"""打包环境自检：源码运行恒通过，打包运行按展开目录里的压缩包判断。

自检只做「能不能打开」一件事，因此测试也直接针对这个判断，
不构造真实的打包产物。
"""

from __future__ import annotations

import sys
import zipfile

from core import packenv


def _fake_frozen(monkeypatch, tmp_path, *, create=True):
    """把当前进程装成打包运行，展开目录指向临时目录。

    压缩包必须是结构的真实 zip：检查会按压缩包结构打开一次，只为「能读
    开头」造出来的假文件头会被如实判成坏包。
    """
    root = tmp_path / "_MEI123456"
    root.mkdir()
    if create:
        with zipfile.ZipFile(root / "base_library.zip", "w") as zf:
            zf.writestr("encodings/__init__.py", "placeholder")
    monkeypatch.setattr(sys, "_MEIPASS", str(root), raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    return root


def test_source_run_always_ok(monkeypatch):
    """源码运行没有展开目录，检查必须直接通过。"""
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert packenv.check_pack_env() == (True, "")
    assert packenv.diagnostics()["frozen"] is False


def test_frozen_with_archive_ok(monkeypatch, tmp_path):
    _fake_frozen(monkeypatch, tmp_path)
    assert packenv.check_pack_env() == (True, "")


def test_frozen_missing_archive_fails(monkeypatch, tmp_path):
    """压缩包不在时返回失败并带上原因，调用方据此降级。"""
    _fake_frozen(monkeypatch, tmp_path, create=False)
    ok, detail = packenv.check_pack_env(retry=False)
    assert ok is False
    assert "FileNotFoundError" in detail


def test_retry_recovers_when_second_attempt_works(monkeypatch, tmp_path):
    """首次读不到、隔一下能读到，重试要把它救回来。"""
    _fake_frozen(monkeypatch, tmp_path)
    calls = {"n": 0}
    real_probe = packenv._probe

    def flaky(path):
        calls["n"] += 1
        if calls["n"] == 1:
            return False, "OSError: busy"
        return real_probe(path)

    monkeypatch.setattr(packenv, "_probe", flaky)
    ok, detail = packenv.check_pack_env(retry=True, retry_delay=0)
    assert ok is True
    assert detail == ""
    assert calls["n"] == 2


def test_retry_gives_up_after_second_failure(monkeypatch, tmp_path):
    """两次都失败才判定不可读，且只尝试两次。"""
    _fake_frozen(monkeypatch, tmp_path)
    calls = {"n": 0}

    def always_fail(path):
        calls["n"] += 1
        return False, "OSError: gone"

    monkeypatch.setattr(packenv, "_probe", always_fail)
    ok, detail = packenv.check_pack_env(retry=True, retry_delay=0)
    assert ok is False
    assert detail == "OSError: gone"
    assert calls["n"] == 2


def test_diagnostics_reports_free_space_and_status(monkeypatch, tmp_path):
    """诊断信息要能回答「目录在哪、盘还剩多少、文件能不能读」。"""
    root = _fake_frozen(monkeypatch, tmp_path)
    info = packenv.diagnostics()
    assert info["frozen"] is True
    assert info["pack_root"] == str(root)
    assert info["archive_ok"] is True
    assert isinstance(info["free_bytes"], int)
    assert info["free_bytes"] > 0
    text = packenv.diagnostics_text()
    assert "archive_ok=True" in text
    assert "free=" in text

def test_frozen_empty_archive_fails(monkeypatch, tmp_path):
    """压缩包是空文件时判失败。

    展开过程中磁盘写满会留下 0 字节文件，读前几字节不报错，旧实现会放行，
    结果工作进程用它导入标准库时直接起不来。
    """
    root = tmp_path / "_MEI123456"
    root.mkdir()
    (root / "base_library.zip").write_bytes(b"")
    monkeypatch.setattr(sys, "_MEIPASS", str(root), raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    ok, detail = packenv.check_pack_env(retry=False)
    assert ok is False
    assert detail


def test_frozen_truncated_archive_fails(monkeypatch, tmp_path):
    """压缩包被截断时判失败。

    截断文件的头部仍然可读，只有按压缩包结构打开才会暴露，因此这条用例
    专门守住「不只是能读开头」这个要求。
    """
    root = tmp_path / "_MEI123456"
    root.mkdir()
    archive = root / "base_library.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("encodings/__init__.py", "x" * 4000)
    data = archive.read_bytes()
    archive.write_bytes(data[: len(data) // 2])
    monkeypatch.setattr(sys, "_MEIPASS", str(root), raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    ok, detail = packenv.check_pack_env(retry=False)
    assert ok is False
    assert "BadZipFile" in detail
