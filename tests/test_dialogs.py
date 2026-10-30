"""原生对话框统一入口：初始目录回退与异常兜底。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest


def test_preferred_missing_falls_back_to_existing(tmp_path, monkeypatch):
    from app import dialogs

    captured: dict[str, str] = {}

    class _FakeWindow:
        def create_file_dialog(self, dialog_type, directory="", **kwargs):
            captured["directory"] = directory
            return (directory,)

    monkeypatch.setattr(dialogs.store, "get_snapshot_dir", lambda: str(tmp_path))
    missing = str(tmp_path / "missing-dir")
    paths = dialogs.create_file_dialog(
        _FakeWindow(), "folder", preferred_dir=missing
    )

    assert captured["directory"] != missing
    assert os.path.isdir(captured["directory"])
    assert paths and os.path.isdir(paths[0])


def test_choose_folder_returns_error_instead_of_raw_exception(tmp_path, monkeypatch):
    from app import dialogs
    from app.api.snapshots import SnapshotsMixin

    class _FakeWindow:
        def create_file_dialog(self, *args, **kwargs):
            raise KeyError("HOMEPATH")

    monkeypatch.setattr(dialogs.store, "get_snapshot_dir", lambda: str(tmp_path))
    api = SnapshotsMixin()
    api._window = _FakeWindow()

    result = api.choose_folder()

    assert result.get("error")
    assert "KeyError" not in result["error"]
    assert "HOMEPATH" not in result["error"]


def test_choose_folder_in_subprocess_without_homepath(tmp_path):
    """删除 HOMEPATH 后文件夹选择接口仍传入有效初始目录，不抛 KeyError。"""
    if os.name != "nt":
        pytest.skip("Windows 环境变量场景")
    env = os.environ.copy()
    env.pop("HOMEPATH", None)
    env["LOCALAPPDATA"] = str(tmp_path / "appdata")
    script = textwrap.dedent(
        """
        import json
        import os
        from app.api.snapshots import SnapshotsMixin

        class FakeWindow:
            def create_file_dialog(self, dialog_type, directory="", **kwargs):
                if not directory:
                    raise KeyError("HOMEPATH")
                return (directory,)

        class ApiForDialog(SnapshotsMixin):
            def __init__(self):
                self._window = FakeWindow()

        result = ApiForDialog().choose_folder()
        assert not result.get("error"), result
        assert os.path.isdir(result.get("path") or ""), result
        print(json.dumps(result, ensure_ascii=False))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert os.path.isdir(result["path"])
