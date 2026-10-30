"""存储判定、损坏文件备份与迁移登记失败的回归测试。"""

from __future__ import annotations

import json
import os
import threading

import pytest

from core import store, store_paths


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _backups(path: str, prefix: str) -> list[str]:
    folder = os.path.dirname(path)
    name = os.path.basename(path)
    try:
        return [
            os.path.join(folder, item)
            for item in os.listdir(folder)
            if item.startswith(f"{name}.{prefix}-") and item.endswith(".bak")
        ]
    except OSError:
        return []


# ---- 快照目录可写性 ----


def test_probe_writable_dir_checks_write_and_remove(tmp_path, monkeypatch):
    target = tmp_path / "snaps"
    target.mkdir()
    assert store_paths.probe_writable_dir(str(target)) == ""
    assert os.listdir(target) == []

    def _denied(path):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(store_paths, "_write_probe_file", _denied)
    assert store_paths.probe_writable_dir(str(target)) == "permission"

    blocked = tmp_path / "not-a-dir"
    blocked.write_text("x", encoding="utf-8")
    assert store_paths.probe_writable_dir(str(blocked)) == "already_file"


def test_custom_snapshot_dir_unavailable_does_not_fall_back(tmp_path, monkeypatch):
    custom = tmp_path / "custom-snaps"
    custom.mkdir()
    assert store.set_snapshot_dir(str(custom)) == os.path.abspath(str(custom))
    builtin = store.builtin_snapshot_dir()

    real_probe = store_paths.probe_writable_dir

    def _probe(path):
        if _norm(path) == _norm(str(custom)):
            return "permission"
        return real_probe(path)

    monkeypatch.setattr(store_paths, "probe_writable_dir", _probe)
    with pytest.raises(store.SnapshotDirUnavailable) as info:
        store.get_snapshot_dir()
    assert info.value.path == os.path.abspath(str(custom))
    assert info.value.reason == "permission"

    status = store.storage_status()
    assert status["ok"] is True
    assert status["snapshot_dir_unavailable"] is True
    assert status["snapshot_dir"] == os.path.abspath(str(custom))

    settings = store.settings_dict()
    assert settings["snapshot_dir_unavailable"] is True
    assert settings["snapshot_dir_available"] is False
    assert settings["snapshot_dir"] == os.path.abspath(str(custom))
    assert settings["snapshot_dir"] != builtin

    from app.api.settings import SettingsMixin
    from app.api.snapshots import SnapshotsMixin

    current = SettingsMixin().get_settings()
    assert current.get("snapshot_dir_warning")
    assert current["snapshot_dir_unavailable"] is True

    listed = SnapshotsMixin().list_snapshots()
    assert isinstance(listed, dict)
    assert listed.get("error")
    assert "Traceback" not in listed["error"]
    folders = SnapshotsMixin().list_snapshot_folders()
    assert isinstance(folders, dict)
    assert folders.get("error")


def test_set_snapshot_dir_rejects_unwritable_and_keeps_old(tmp_path, monkeypatch):
    good = tmp_path / "good-snaps"
    bad = tmp_path / "bad-snaps"
    good.mkdir()
    bad.mkdir()
    assert store.set_snapshot_dir(str(good)) == os.path.abspath(str(good))

    real_probe = store_paths.probe_writable_dir

    def _probe(path):
        if _norm(path) == _norm(str(bad)):
            return "permission"
        return real_probe(path)

    monkeypatch.setattr(store_paths, "probe_writable_dir", _probe)
    with pytest.raises(store.SnapshotDirUnavailable):
        store.set_snapshot_dir(str(bad))
    assert store.get_snapshot_dir_configured() == os.path.abspath(str(good))


def test_pick_snapshot_dir_rejects_unwritable(tmp_path, monkeypatch):
    from app.api.settings import SettingsMixin

    bad = tmp_path / "picked-bad"
    bad.mkdir()

    class ApiForPick(SettingsMixin):
        def choose_folder(self):
            return {"path": str(bad)}

    monkeypatch.setattr(
        store_paths, "probe_writable_dir", lambda _p: "permission"
    )
    result = ApiForPick().pick_snapshot_dir()
    assert result.get("error")
    assert result.get("ok") is not True


def test_reset_to_builtin_fails_without_changing_config(tmp_path, monkeypatch):
    custom = tmp_path / "custom-snaps"
    custom.mkdir()
    assert store.set_snapshot_dir(str(custom)) == os.path.abspath(str(custom))
    builtin = store.builtin_snapshot_dir()

    real_probe = store_paths.probe_writable_dir

    def _probe(path):
        if _norm(path) == _norm(builtin):
            return "permission"
        return real_probe(path)

    monkeypatch.setattr(store_paths, "probe_writable_dir", _probe)
    with pytest.raises(store.SnapshotDirUnavailable):
        store.set_snapshot_dir("")
    assert store.get_snapshot_dir_configured() == os.path.abspath(str(custom))

    from app.api.settings import SettingsMixin

    result = SettingsMixin().reset_snapshot_dir()
    assert result.get("error")
    assert result.get("ok") is not True
    assert store.get_snapshot_dir_configured() == os.path.abspath(str(custom))

    reset_all = SettingsMixin().reset_settings()
    assert reset_all.get("error")
    assert reset_all.get("ok") is not True
    assert store.get_snapshot_dir_configured() == os.path.abspath(str(custom))
    assert os.path.isfile(store.settings_path())


# ---- settings.yaml 读取失败 ----


def test_corrupt_settings_is_backed_up_before_write(tmp_path):
    conf = store.settings_path()
    bad = b"common:\n  scan_workers: \xff\xfe\n"
    with open(conf, "wb") as fh:
        fh.write(bad)

    store.reload_settings_from_disk()
    assert store.settings_recovered() is True
    backups = _backups(conf, "corrupt")
    assert len(backups) == 1
    assert open(backups[0], "rb").read() == bad

    # 备份被人删掉后，写入设置前必须重新做一份，不能直接覆盖原件
    os.remove(backups[0])
    new_workers = 2 if store.get_scan_workers() != 2 else 4
    store.set_scan_workers(new_workers)
    backups = _backups(conf, "corrupt")
    assert backups
    assert open(backups[0], "rb").read() == bad
    assert store.get_scan_workers() == new_workers
    with open(conf, encoding="utf-8") as fh:
        text = fh.read()
    assert f"scan_workers: {new_workers}" in text


def test_truncated_settings_is_treated_as_read_failure(tmp_path):
    conf = store.settings_path()
    with open(conf, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            "# WhoShitsOnMyC settings — auto-written when settings change\n"
            "common:\n"
            "  scan_workers: 4\n"
            "  snapshot_dir: \"D:\\\\snaps\""
        )
    store.reload_settings_from_disk()
    assert store.settings_recovered() is True
    assert _backups(conf, "corrupt")


def test_non_utf8_settings_does_not_escape_module_import(tmp_path):
    import subprocess
    import sys

    appdata = tmp_path / "appdata"
    appdir = appdata / "WhoShitsOnMyC"
    appdir.mkdir(parents=True)
    (appdir / "settings.yaml").write_bytes(b"common:\n  scan_workers: \xff\n")
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(appdata)
    script = (
        "import json\n"
        "from core import store\n"
        f"store.set_data_root_for_tests({str(appdir)!r})\n"
        "store.reload_settings_from_disk()\n"
        "print(json.dumps({'recovered': store.settings_recovered(),"
        " 'workers': store.get_scan_workers()}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert data["recovered"] is True
    assert isinstance(data["workers"], int)
    assert _backups(str(appdir / "settings.yaml"), "corrupt")


def test_get_settings_reports_settings_recovery(tmp_path):
    conf = store.settings_path()
    with open(conf, "wb") as fh:
        fh.write(b"not utf8 \xff\n")
    store.reload_settings_from_disk()

    from app.api.settings import SettingsMixin

    current = SettingsMixin().get_settings()
    assert current.get("settings_warning")
    assert "scan_workers" not in current["settings_warning"]
    assert "Traceback" not in current["settings_warning"]


# ---- links.json / migration_history.json 读取失败 ----


def test_corrupt_links_registry_is_backed_up_and_write_guarded(tmp_path, monkeypatch):
    from core import migrate_links

    path = tmp_path / "links.json"
    monkeypatch.setattr(migrate_links, "_registry_path", lambda: str(path))
    bad = b'[{"link": "D:\\\\x"'
    path.write_bytes(bad)

    assert migrate_links.load_links() == []
    backups = _backups(str(path), "corrupt")
    assert len(backups) == 1
    assert open(backups[0], "rb").read() == bad
    assert migrate_links.link_registry_recovery() is not None

    # 备份被删掉时，写入前重新备份，旧内容不能被静默清空
    os.remove(backups[0])
    migrate_links.register_link(r"D:\x", r"E:\x", "")
    backups = _backups(str(path), "corrupt")
    assert backups
    assert open(backups[0], "rb").read() == bad
    assert len(migrate_links.load_links()) == 1

    # 做不出备份时必须拒绝写入，原文件保持原样
    path.write_bytes(bad)
    migrate_links.load_links()
    backups = _backups(str(path), "corrupt")
    for item in backups:
        os.remove(item)
    monkeypatch.setattr(migrate_links, "backup_corrupt_file", lambda _p: "")
    with pytest.raises(OSError):
        migrate_links.save_links([])
    assert path.read_bytes() == bad


def test_corrupt_history_is_backed_up_and_write_guarded(tmp_path, monkeypatch):
    from core import migrate_history

    path = tmp_path / "migration_history.json"
    monkeypatch.setattr(migrate_history, "_history_path", lambda: str(path))
    bad = b'[{"op": "migrate"'
    path.write_bytes(bad)

    assert migrate_history.load_history() == []
    backups = _backups(str(path), "corrupt")
    assert len(backups) == 1
    assert open(backups[0], "rb").read() == bad
    assert migrate_history.history_recovery() is not None

    os.remove(backups[0])
    rec = migrate_history.record_history({
        "ts": 1.0,
        "op": "migrate",
        "source": "s",
        "target": "t",
        "status": "success",
    })
    assert rec is not None
    assert _backups(str(path), "corrupt")
    assert len(migrate_history.load_history()) == 1

    # 备份做不出来时，追加历史失败，原文件不被覆盖
    path.write_bytes(bad)
    migrate_history.load_history()
    for item in _backups(str(path), "corrupt"):
        os.remove(item)
    monkeypatch.setattr(migrate_history, "backup_corrupt_file", lambda _p: "")
    assert migrate_history.record_history({
        "ts": 2.0,
        "op": "migrate",
        "source": "s2",
        "target": "t2",
        "status": "success",
    }) is None
    assert path.read_bytes() == bad


def test_list_migration_history_reports_recovery(tmp_path, monkeypatch):
    from core import migrate_history
    from app.api.migrate import MigrateMixin

    path = tmp_path / "migration_history.json"
    monkeypatch.setattr(migrate_history, "_history_path", lambda: str(path))
    path.write_text("not json", encoding="utf-8")

    result = MigrateMixin().list_migration_history()
    assert result.get("ok") is True
    assert result.get("history") == []
    assert result.get("warning")
    assert "Traceback" not in result["warning"]


# ---- 迁移成功但登记或历史写入失败 ----


from app.api.migrate import MigrateMixin  # noqa: E402


class _MigrationApi(MigrateMixin):
    """只覆盖迁移后台线程需要的属性与事件推送。"""

    def __init__(self):
        self.events = []
        self._link_job_lock = threading.Lock()
        self._link_job = ""
        self._link_job_token = 0
        self._link_seq = 0
        self._migrate_cancel = threading.Event()
        self._remigrate_cancel = threading.Event()
        self._restore_cancel = threading.Event()
        self._ask_lock = threading.Lock()
        self._ask_id = 0
        self._ask_result = None
        self._ask_event = threading.Event()

    def _emit(self, event, payload):
        self.events.append((event, dict(payload or {})))


def test_migration_reports_registry_and_history_failures(tmp_path, monkeypatch):
    from core import fs_migrate, migrate_history, migrate_links

    monkeypatch.setattr(
        migrate_links, "_registry_path", lambda: str(tmp_path / "links.json")
    )
    monkeypatch.setattr(
        migrate_history, "_history_path",
        lambda: str(tmp_path / "migration_history.json"),
    )

    def _registry_fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(fs_migrate, "register_link", _registry_fail)
    monkeypatch.setattr(fs_migrate, "record_history", lambda entry: None)

    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_text("m", encoding="utf-8")
    dst = tmp_path / "dst"

    api = _MigrationApi()
    api._run_directory_migration(str(src), str(dst), "keep", 0)

    event, payload = api.events[-1]
    assert event == "migrate-dir-done"
    assert payload["ok"] is True
    assert payload["link_registered"] is False
    assert payload["link_error"]
    assert "import" in payload["link_error"].lower()
    assert payload["history_saved"] is False
    assert (dst / "f.txt").read_text(encoding="utf-8") == "m"
    assert (src / "f.txt").read_text(encoding="utf-8") == "m"


# ---- 快照目录迁移一致性 ----


def _make_snapshot(path, size=20):
    with open(path, "wb") as fh:
        fh.write(b"x" * size)


def test_snapshot_migration_verifies_copy_and_removes_partial(
    tmp_path, monkeypatch
):
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    src = src_dir / "a.db"
    _make_snapshot(src, 30)
    dst = dst_dir / "a.db"

    real_replace = os.replace

    def _replace(a, b):
        if _norm(str(a)) == _norm(str(src)) and _norm(str(b)) == _norm(str(dst)):
            raise OSError("cross volume")
        return real_replace(a, b)

    def _short_copy(a, b):
        with open(a, "rb") as fh:
            data = fh.read()
        with open(b, "wb") as fh:
            fh.write(data[: len(data) // 2])

    monkeypatch.setattr(store_paths.os, "replace", _replace)
    monkeypatch.setattr("shutil.copy2", _short_copy)

    result = store_paths.migrate_snapshots(str(src_dir), str(dst_dir))
    assert result["failed"] == 1
    assert result["moved"] == 0
    assert src.is_file()
    assert not dst.exists()
    assert not list(dst_dir.glob("*.tmp"))


def test_snapshot_migration_same_size_skips_and_diff_size_renames(tmp_path):
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()

    same_src = src_dir / "same.db"
    _make_snapshot(same_src, 12)
    (dst_dir / "same.db").write_bytes(same_src.read_bytes())

    diff_src = src_dir / "diff.db"
    _make_snapshot(diff_src, 24)
    (dst_dir / "diff.db").write_bytes(b"old")

    result = store_paths.migrate_snapshots(str(src_dir), str(dst_dir))
    assert result["failed"] == 0
    assert result["skipped"] == 1
    assert result["moved"] == 1
    assert same_src.is_file()
    assert (dst_dir / "same.db").read_bytes() == same_src.read_bytes()
    assert not diff_src.exists()
    assert (dst_dir / "diff.db").read_bytes() == b"old"
    renamed = dst_dir / "diff (2).db"
    assert renamed.is_file()
    assert len(renamed.read_bytes()) == 24
    assert any("diff (2).db" in item for item in result["renamed"])


def test_snapshot_migration_counts_unreadable_subfolder_as_failed(
    tmp_path, monkeypatch
):
    """快照归纳子目录读不到时必须计入失败，不能当成零个文件。"""
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    (src_dir / "sub").mkdir(parents=True)
    dst_dir.mkdir()
    _make_snapshot(src_dir / "top.db")
    _make_snapshot(src_dir / "sub" / "inner.db")

    real_listdir = os.listdir
    blocked = _norm(str(src_dir / "sub"))

    def _listdir(path):
        try:
            key = _norm(os.fspath(path))
        except (TypeError, ValueError):
            key = ""
        if key == blocked:
            raise PermissionError(13, "access denied")
        return real_listdir(path)

    monkeypatch.setattr(store_paths.os, "listdir", _listdir)

    result = store_paths.migrate_snapshots(str(src_dir), str(dst_dir))

    assert result["failed"] >= 1
    assert any("sub" in item for item in result["errors"])
    assert result["moved"] == 1
    assert (dst_dir / "top.db").is_file()


def test_snapshot_migration_rejects_unwritable_target(tmp_path, monkeypatch):
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    src = src_dir / "a.db"
    _make_snapshot(src)

    monkeypatch.setattr(
        store_paths, "probe_writable_dir", lambda _p: "permission"
    )
    result = store_paths.migrate_snapshots(str(src_dir), str(dst_dir))
    assert result["failed"] == 1
    assert result["error"] == "destination_unwritable"
    assert src.is_file()
    assert not (dst_dir / "a.db").exists()


# ---- 文件已搬走但设置写入失败 ----


def test_apply_settings_reports_move_done_without_saved_config(
    tmp_path, monkeypatch
):
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    src_file = src_dir / "a.db"
    _make_snapshot(src_file, 10)
    store._snapshot_dir = str(src_dir)

    def _persist_fail():
        raise OSError("settings disk full")

    monkeypatch.setattr(store, "_persist", _persist_fail)

    result = store.apply_settings({"snapshot_dir": str(dst_dir)})
    assert result["error"] == "snapshot_dir_not_saved"
    assert result["path"] == os.path.abspath(str(dst_dir))
    assert result["migrate"]["moved"] == 1
    assert (dst_dir / "a.db").is_file()
    assert not src_file.exists()
    # 配置没有保存，内存也不能显示成新目录
    assert store.get_snapshot_dir_configured() == os.path.abspath(str(src_dir))


def test_apply_settings_save_failure_message_is_readable(tmp_path, monkeypatch):
    from app.api.settings import SettingsMixin
    from core import i18n

    i18n.set_lang("zh")
    target = tmp_path / "moved-snaps"
    target.mkdir()

    class ApiForApply(SettingsMixin):
        def __init__(self):
            self.events = []

        def _emit(self, event, payload):
            self.events.append((event, payload))

    monkeypatch.setattr(
        store,
        "apply_settings",
        lambda payload, progress=None: {
            "error": "snapshot_dir_not_saved",
            "path": str(target),
            "migrate": {"moved": 1, "skipped": 0, "failed": 0, "total": 1},
        },
    )
    api = ApiForApply()
    api._run_apply_settings({"snapshot_dir": str(target)})
    event, payload = api.events[-1]
    assert event == "settings-applied"
    assert payload["ok"] is False
    assert str(target) in payload["error"]
    assert "设置没有保存" in payload["error"]
    assert "snapshot_dir_not_saved" not in payload["error"]
    assert "Traceback" not in payload["error"]


def test_apply_settings_rolls_back_all_values_when_persist_fails(monkeypatch):
    old = {
        "workers": store.get_scan_workers(),
        "compress": store.get_compress_snapshots(),
        "mft": store.get_use_mft(),
        "sanitize": store.get_log_sanitize(),
        "level": store.get_log_level(),
        "sanitize_explicit": store.is_log_sanitize_explicit(),
        "level_explicit": store.is_log_level_explicit(),
        "blacklist": store.get_delete_blacklist(),
        "pinned": store.get_pinned_snapshots(),
    }

    def _persist_fail():
        raise OSError("disk full")

    monkeypatch.setattr(store, "_persist", _persist_fail)

    result = store.apply_settings(
        {
            "scan_workers": old["workers"] + 1,
            "compress_snapshots": not old["compress"],
            "use_mft": not old["mft"],
            "log_sanitize": not old["sanitize"],
            "log_level": "DEBUG" if old["level"] != "DEBUG" else "INFO",
            "delete_blacklist": [{"path": r"D:\rollback", "mode": "exact"}],
            "pinned_snapshots": [r"D:\rollback.db"],
        }
    )

    assert result["error"] == "settings_write_failed"
    assert result["migrate"]["moved"] == 0
    assert store.get_scan_workers() == old["workers"]
    assert store.get_compress_snapshots() == old["compress"]
    assert store.get_use_mft() == old["mft"]
    assert store.get_log_sanitize() == old["sanitize"]
    assert store.get_log_level() == old["level"]
    assert store.is_log_sanitize_explicit() == old["sanitize_explicit"]
    assert store.is_log_level_explicit() == old["level_explicit"]
    assert store.get_delete_blacklist() == old["blacklist"]
    assert store.get_pinned_snapshots() == old["pinned"]


def test_settings_write_failure_passes_migrate_details(monkeypatch):
    from app.api.settings import SettingsMixin
    from core import i18n

    i18n.set_lang("zh")
    migrate = {
        "moved": 1,
        "skipped": 1,
        "failed": 2,
        "total": 4,
        "errors": ["a.db: denied", "b.db: denied"],
    }

    class ApiForApply(SettingsMixin):
        def __init__(self):
            self.events = []

        def _emit(self, event, payload):
            self.events.append((event, payload))

    monkeypatch.setattr(
        store,
        "apply_settings",
        lambda payload, progress=None: {
            "error": "settings_write_failed",
            "reason": "permission",
            "migrate": migrate,
            "snapshot_dir_changed": False,
        },
    )

    api = ApiForApply()
    api._run_apply_settings({"scan_workers": 2})
    event, payload = api.events[-1]
    assert event == "settings-applied"
    assert payload["ok"] is False
    assert payload["migrate"] == migrate
    assert "settings_write_failed" not in payload["error"]
    assert "Traceback" not in payload["error"]


def test_settings_write_failed_copy_states_cause_before_action():
    from app import winmsgs
    from core import i18n

    i18n.set_lang("zh")
    msg = winmsgs._settings_write_failed_message("permission")
    assert msg.startswith("设置没有保存，原因是当前运行账户没有写入数据文件夹的权限。")
    assert "文件夹属性" in msg
    assert "重试一次" not in msg
