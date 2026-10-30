"""应用数据目录不可创建时的启动与扫描兜底。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest


def test_storage_failure_does_not_crash_import_or_scan(tmp_path):
    scan_root = tmp_path / "scan-root"
    scan_root.mkdir()
    env = os.environ.copy()
    script = textwrap.dedent(
        """
        import json
        import sys
        import threading
        import time

        from core import store
        from app.api.scan import ScanMixin
        from app.api.settings import SettingsMixin

        store.initialize_storage = lambda: {
            "ok": False,
            "path": r"C:locked",
            "reason": "permission",
        }
        store._storage_initialized = True

        class FakeWindow:
            def __init__(self):
                self.events = []

            def evaluate_js(self, script):
                self.events.append(script)

        class ApiForStorage(SettingsMixin, ScanMixin):
            def __init__(self):
                self._window = FakeWindow()
                self._scan_thread = None
                self._settings_thread = None
                self._cancel = threading.Event()

            def _emit(self, event, payload):
                self._window.evaluate_js(
                    json.dumps(
                        {"event": event, "payload": payload},
                        ensure_ascii=False,
                    )
                )

        status = store.storage_status()
        settings = store.settings_dict()
        api = ApiForStorage()
        current = api.get_settings()
        started = api.start_scan(sys.argv[1])

        print(json.dumps({
            "status": status,
            "settings": settings,
            "current": current,
            "started": started,
        }, ensure_ascii=False))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(scan_root)],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout.strip().splitlines()[-1])

    assert data["status"]["ok"] is False
    assert data["status"]["reason"]
    assert data["settings"].get("error") == "storage_unavailable"
    assert data["current"].get("error")
    assert data["current"].get("error") != "storage_unavailable"
    assert "reason" not in data["current"]
    assert "KeyError" not in data["current"]["error"]
    assert r"C:locked" in data["current"]["error"]
    assert data["started"].get("error")
    assert "storage_unavailable" not in data["started"]["error"]
    assert r"C:locked" in data["started"]["error"]


def _block_builtin_snapshot_dir() -> str:
    """把内置快照目录换成同名文件，模拟被文件或权限卡住的位置。"""
    from core import store

    builtin = os.path.join(store.app_data_dir(), "snapshots")
    if os.path.isdir(builtin):
        os.rmdir(builtin)
    elif os.path.exists(builtin):
        os.remove(builtin)
    with open(builtin, "w", encoding="utf-8") as f:
        f.write("not a directory")
    return builtin


def test_storage_status_accepts_custom_snapshot_dir_when_builtin_is_file(tmp_path):
    """自定义快照目录可用时，内置目录不可用不能阻断启动检查。"""
    from core import store

    custom = tmp_path / "custom-snaps"
    custom.mkdir()
    assert store.set_snapshot_dir(str(custom)) == os.path.abspath(str(custom))
    _block_builtin_snapshot_dir()

    status = store.storage_status()
    assert status["ok"] is True
    assert status["snapshot_dir"] == os.path.abspath(str(custom))

    settings = store.settings_dict()
    assert "error" not in settings
    assert settings["snapshot_dir"] == os.path.abspath(str(custom))
    assert settings["snapshot_dir_builtin"] == ""


def test_snapshot_dir_pick_and_save_succeed_when_builtin_is_file(tmp_path):
    """内置目录不可用时，选择和保存已经可用的自定义目录仍应成功。"""
    from app.api.settings import SettingsMixin

    _block_builtin_snapshot_dir()
    picked_path = tmp_path / "picked-snaps"
    picked_path.mkdir()
    saved_path = tmp_path / "saved-snaps"
    saved_path.mkdir()

    class ApiForSnapshotDir(SettingsMixin):
        def choose_folder(self):
            return {"path": str(picked_path)}

    api = ApiForSnapshotDir()
    picked = api.pick_snapshot_dir()
    assert picked.get("ok") is True
    assert picked.get("snapshot_dir_builtin") == ""

    chosen = api.choose_snapshot_dir()
    assert chosen.get("ok") is True
    assert chosen.get("snapshot_dir") == os.path.abspath(str(picked_path))
    assert chosen.get("snapshot_dir_builtin") == ""

    saved = api.set_snapshot_dir(str(saved_path))
    assert saved.get("ok") is True
    assert saved.get("snapshot_dir") == os.path.abspath(str(saved_path))
    assert saved.get("snapshot_dir_builtin") == ""


def test_apply_settings_can_save_custom_dir_when_builtin_is_file(tmp_path):
    """原内置快照目录不可用时，设置页提交自定义目录仍应成功。"""
    from core import store

    _block_builtin_snapshot_dir()
    custom = tmp_path / "applied-snaps"
    custom.mkdir()

    result = store.apply_settings({"snapshot_dir": str(custom)})
    assert "error" not in result
    assert result["snapshot_dir"] == os.path.abspath(str(custom))
    assert result["snapshot_dir_builtin"] == ""


def test_snapshot_dir_apis_tolerate_blocked_builtin_dir(tmp_path, monkeypatch):
    from app.api.settings import SettingsMixin
    from core import store

    class ApiForSnapshotDir(SettingsMixin):
        def choose_folder(self):
            return {"path": str(tmp_path)}

    def _blocked():
        raise OSError("blocked")

    api = ApiForSnapshotDir()
    monkeypatch.setattr(store, "builtin_snapshot_dir", _blocked)
    monkeypatch.setattr(store, "default_snapshot_dir", _blocked)

    picked = api.pick_snapshot_dir()
    opened = api.open_snapshot_dir()
    chosen = api.choose_snapshot_dir()
    assert picked.get("ok") is True
    assert picked.get("snapshot_dir_builtin") == ""
    assert opened.get("error")
    assert chosen.get("ok") is True
    assert chosen.get("snapshot_dir_builtin") == ""

    class ErrorPicker(SettingsMixin):
        def choose_folder(self):
            return {"error": "folder dialog failed"}

    chosen = ErrorPicker().choose_snapshot_dir()
    assert chosen.get("error") == "folder dialog failed"


def test_apply_settings_storage_error_is_readable(monkeypatch):
    """应用设置遇到内部存储错误时只回可读文案，不泄漏机器键与路径。"""
    from app.api.settings import SettingsMixin
    from core import store

    class ApiForApply(SettingsMixin):
        def __init__(self):
            self.events = []

        def _emit(self, event, payload):
            self.events.append((event, payload))

    api = ApiForApply()
    internal = {
        "error": "storage_unavailable",
        "path": r"C:\private\appdata",
        "reason": "permission",
    }
    monkeypatch.setattr(
        store,
        "apply_settings",
        lambda payload, progress=None: dict(internal),
    )

    api._run_apply_settings({"scan_workers": 2})
    assert len(api.events) == 1
    event, result = api.events[0]
    assert event == "settings-applied"
    assert set(result) == {"ok", "error"}
    assert result["ok"] is False
    assert result["error"]
    assert "storage_unavailable" not in result["error"]
    assert r"C:\private\appdata" in result["error"]

    def _raise_storage(*args, **kwargs):
        raise OSError("blocked")

    monkeypatch.setattr(store, "apply_settings", _raise_storage)
    api._run_apply_settings({"scan_workers": 2})
    assert len(api.events) == 2
    event, result = api.events[1]
    assert event == "settings-applied"
    assert set(result) == {"ok", "error"}
    assert result["ok"] is False
    assert result["error"]
    assert "blocked" not in result["error"]


def test_reset_settings_storage_error_is_readable(monkeypatch):
    """恢复默认遇到内部存储错误时只回可读文案，不附带成功标志。"""
    from app.api.settings import SettingsMixin
    from core import store

    class ApiForReset(SettingsMixin):
        pass

    monkeypatch.setattr(
        store,
        "reset_settings_to_defaults",
        lambda **kwargs: {
            "error": "storage_unavailable",
            "path": r"C:\private\appdata",
            "reason": "permission",
        },
    )

    result = ApiForReset().reset_settings()
    assert set(result) == {"error"}
    assert result["error"]
    assert "storage_unavailable" not in result["error"]
    assert r"C:\private\appdata" in result["error"]

    def _raise_storage(**kwargs):
        raise OSError("blocked")

    monkeypatch.setattr(store, "reset_settings_to_defaults", _raise_storage)
    result = ApiForReset().reset_settings()
    assert set(result) == {"error"}
    assert result["error"]
    assert "blocked" not in result["error"]


def test_get_settings_storage_error_returns_readable_text_only(monkeypatch):
    """加载设置拿到错误对象时只返回可读文案，不混入正常设置字段。"""
    from app.api.settings import SettingsMixin
    from core import store

    class ApiForSettings(SettingsMixin):
        pass

    monkeypatch.setattr(
        store,
        "settings_dict",
        lambda: {
            "error": "storage_unavailable",
            "path": r"C:\private\appdata",
            "reason": "permission",
        },
    )

    result = ApiForSettings().get_settings()
    assert set(result) == {"error"}
    assert result["error"]
    assert "storage_unavailable" not in result["error"]
    assert r"C:\private\appdata" in result["error"]


def test_settings_dict_normal_structure_unchanged():
    """正常路径仍返回完整设置快照，不出现内部错误键。"""
    from core import store

    settings = store.settings_dict()
    assert "error" not in settings
    assert "reason" not in settings
    for key in (
        "scan_workers",
        "snapshot_dir",
        "snapshot_dir_configured",
        "snapshot_dir_builtin",
        "settings_path",
        "lang",
        "theme",
        "ai",
    ):
        assert key in settings
