"""数据目录：当前账户解析、启动恢复、主动切换与选择窗口起始位置。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


class _DataFolderApi:
    """只装设置 Mixin 的假 Api：文件夹选择框由用例给定，标题栏与窗口不参与。"""

    def __init__(self, picked: str = "") -> None:
        self._picked = picked

    def choose_folder(self) -> dict:
        return {"path": self._picked}

    def _refresh_window_title(self) -> None:
        pass


def _api(picked: str = ""):
    from app.api.settings import SettingsMixin

    class Api(SettingsMixin, _DataFolderApi):
        pass

    return Api(picked)


def _write(path, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


class _FakeUser:
    """当前运行账户的替身，行为与 CurrentUser 一致。"""

    def __init__(self, local, home=None, public=None, account: str = "tester") -> None:
        self.local_app_data = str(local)
        self.home_dir = str(home or local)
        self.public_dir = str(public or local)
        self.account = account

    def accessible_dir(self, path: str) -> bool:
        return os.path.isdir(path)


def _reset_storage_state() -> None:
    """把存储模块的内存状态清回未初始化，便于重新走启动解析。"""
    from core import store

    store._storage_initialized = False
    store._storage_ok = False
    store._app_data_root = ""
    store._storage_state = {}
    store._current_user = None
    store._snapshot_dir = ""
    store._data_wiped = False


def _init_with_user(monkeypatch, user) -> dict:
    """按给定的当前账户执行一次启动初始化。"""
    from core import store, user_dirs

    monkeypatch.setattr(user_dirs, "open_current_user", lambda: user)
    _reset_storage_state()
    return store.initialize_storage()


def test_startup_enters_window_when_storage_unavailable(monkeypatch):
    """数据目录不可用时照常建立窗口，由前端启动恢复界面处理。"""
    from core import store
    from tests.test_entry_smoke import _FakeWebview, _load_entry

    entry = _load_entry()
    fake_webview = _FakeWebview()

    monkeypatch.setattr(entry, "webview", fake_webview)
    monkeypatch.setattr(entry, "_wire_bundled_webview2", lambda: True)
    monkeypatch.setattr(entry, "_acquire_single_instance", lambda: True)
    monkeypatch.setattr(store, "get_temp_cleanup", lambda: False)
    monkeypatch.setattr(
        store,
        "initialize_storage",
        lambda: {"ok": False, "path": r"C:\blocked", "reason": "permission"},
    )

    def _unreadable() -> dict:
        raise OSError("blocked")

    monkeypatch.setattr(store, "load_settings_document", _unreadable)

    entry.main()

    assert len(fake_webview.created) == 1, "数据目录不可用时没有建立窗口"
    assert len(fake_webview.started) == 1, "数据目录不可用时没有进入消息循环"


@pytest.mark.skipif(os.name != "nt", reason="当前账户解析依赖 Windows 接口")
def test_open_current_user_returns_real_known_folder():
    """真实执行 Windows 已知文件夹解析，不能只靠替身对象。"""
    from core import user_dirs

    user = user_dirs.open_current_user()

    assert os.path.isabs(user.local_app_data)
    assert os.path.isdir(user.local_app_data)
    assert os.path.isabs(user.home_dir)
    assert user.account


def test_startup_uses_current_user_local_app_data(tmp_path, monkeypatch):
    """默认数据根落在当前运行账户的本地应用数据目录下。"""
    from core import store

    local = tmp_path / "current" / "AppData" / "Local"
    local.mkdir(parents=True)
    fake = _FakeUser(local, account="alice")

    try:
        state = _init_with_user(monkeypatch, fake)
    finally:
        store.set_data_root_for_tests(str(tmp_path / "reset"))

    root = local / store._APP_DIR_NAME
    assert state["ok"] is True
    assert state["app_data_dir"] == str(root)
    assert state["account"] == "alice"
    assert store.settings_dict()["account"] == "alice"


def test_startup_does_not_copy_other_account_data(tmp_path, monkeypatch):
    """启动只使用当前账户目录，不读取也不复制其他账户的数据。"""
    from core import store

    local = tmp_path / "current" / "AppData" / "Local"
    local.mkdir(parents=True)
    other = tmp_path / "administrator" / "AppData" / "Local" / store._APP_DIR_NAME
    (other / "snapshots").mkdir(parents=True)
    _write(
        other / "settings.yaml",
        "common:\n  lang: zh\n  theme: dark\n  scan_workers: 7\n",
    )
    _write(other / "snapshots" / "admin.db", "snapshot")
    fake = _FakeUser(local)

    try:
        state = _init_with_user(monkeypatch, fake)
    finally:
        store.set_data_root_for_tests(str(tmp_path / "reset"))

    root = local / store._APP_DIR_NAME
    assert state["ok"] is True
    assert not (root / "settings.yaml").exists()
    assert not (root / "snapshots" / "admin.db").exists()
    assert store.get_scan_workers() != 7
    assert store.get_theme() != "dark"
    assert (other / "settings.yaml").is_file()
    assert (other / "snapshots" / "admin.db").is_file()


def test_startup_uses_recorded_data_root(tmp_path, monkeypatch):
    """记录过自定义数据文件夹时，启动继续使用它。"""
    from core import store

    local = tmp_path / "local"
    local.mkdir()
    picked = tmp_path / "picked"
    root = picked / store._APP_DIR_NAME
    root.mkdir(parents=True)
    _write(
        root / "settings.yaml",
        "common:\n  lang: zh\n  theme: dark\n  scan_workers: 3\n",
    )
    _write(local / (store._APP_DIR_NAME + store._DATA_DIR_POINTER_SUFFIX), str(picked))
    fake = _FakeUser(local)

    try:
        state = _init_with_user(monkeypatch, fake)
    finally:
        store.set_data_root_for_tests(str(tmp_path / "reset"))

    assert state["ok"] is True
    assert state["app_data_dir"] == str(root)
    assert store.get_lang() == "zh"
    assert store.get_theme() == "dark"
    assert store.get_scan_workers() == 3


def test_startup_does_not_migrate_flat_pointer_contents(tmp_path, monkeypatch):
    """记录的是旧版平铺目录时，数据根补一层应用子目录，旧文件原地保留。"""
    from core import store

    local = tmp_path / "local"
    local.mkdir()
    picked = tmp_path / "picked"
    (picked / "snapshots").mkdir(parents=True)
    _write(picked / "settings.yaml", "common:\n  lang: zh\n")
    _write(picked / "snapshots" / "old.db", "snapshot")
    _write(picked / "user-notes.txt", "keep me")
    _write(local / (store._APP_DIR_NAME + store._DATA_DIR_POINTER_SUFFIX), str(picked))
    fake = _FakeUser(local)

    try:
        state = _init_with_user(monkeypatch, fake)
    finally:
        store.set_data_root_for_tests(str(tmp_path / "reset"))

    root = picked / store._APP_DIR_NAME
    assert state["app_data_dir"] == str(root)
    assert not (root / "settings.yaml").exists()
    assert not (root / "snapshots" / "old.db").exists()
    assert (picked / "settings.yaml").is_file()
    assert (picked / "snapshots" / "old.db").is_file()
    assert (picked / "user-notes.txt").read_text(encoding="utf-8") == "keep me"


def test_switch_works_when_user_lookup_failed(tmp_path):
    """账户解析失败时允许显式选目录继续，不能把恢复面板变成死路。"""
    from core import store

    target = tmp_path / "picked"
    target.mkdir()
    store._storage_initialized = True
    store._storage_ok = False
    store._app_data_root = ""
    store._storage_state = {
        "ok": False,
        "app_data_dir": "",
        "path": "",
        "reason": "identity_unavailable",
    }
    store._current_user = None
    try:
        info = store.switch_data_root(str(target))
        assert info["app_data_dir"] == os.path.join(
            os.path.abspath(str(target)), store._APP_DIR_NAME
        )
        assert info["pointer_saved"] is False
        assert store.storage_ready() is True
    finally:
        store.set_data_root_for_tests(str(tmp_path / "reset"))


def test_switch_reads_existing_settings_and_snapshot_dir(tmp_path):
    """选中的文件夹里有设置与快照目录时直接沿用。"""
    from core import store

    target = tmp_path / "picked"
    root = target / store._APP_DIR_NAME
    root.mkdir(parents=True)
    _write(
        root / "settings.yaml",
        "common:\n"
        "  lang: zh\n"
        "  theme: dark\n"
        "  scan_workers: 3\n"
        '  snapshot_dir: ""\n',
    )
    (root / "snapshots").mkdir()
    target_abs = os.path.abspath(str(root))

    info = store.switch_data_root(str(target))

    assert info["app_data_dir"] == target_abs
    assert info["settings_found"] is True
    assert info["target_had_config"] is True
    assert store.app_data_dir() == target_abs
    assert store.settings_path() == os.path.join(target_abs, "settings.yaml")
    assert store.get_lang() == "zh"
    assert store.get_theme() == "dark"
    assert store.get_scan_workers() == 3
    assert store.builtin_snapshot_dir() == os.path.join(target_abs, "snapshots")

    settings = store.settings_dict()
    assert settings["app_data_dir"] == target_abs
    assert settings["snapshot_dir"] == os.path.join(target_abs, "snapshots")
    assert settings["snapshot_dir_is_custom"] is False


def test_switch_moves_old_settings_and_snapshots(tmp_path):
    """从旧位置切到新位置时，旧设置和快照移动到新位置。"""
    from core import store

    old_root = store.app_data_dir()
    store.set_theme("dark")
    store.set_scan_workers(2)
    snap_dir = store.builtin_snapshot_dir()
    _write(os.path.join(snap_dir, "old.db"), "snapshot")

    target = tmp_path / "picked"
    target.mkdir()
    info = store.switch_data_root(str(target))
    root = target / store._APP_DIR_NAME

    assert info["moved"] >= 2
    assert os.path.isfile(os.path.join(root, "settings.yaml"))
    assert os.path.isfile(os.path.join(root, "snapshots", "old.db"))
    assert not os.path.exists(os.path.join(old_root, "settings.yaml"))
    assert not os.path.exists(os.path.join(old_root, "snapshots", "old.db"))
    assert store.get_theme() == "dark"
    assert store.get_scan_workers() == 2


def test_switch_reads_migration_list_and_history_from_new_folder(tmp_path):
    """迁移列表与迁移历史随后续接口读新文件夹里的文件。"""
    from core import migrate_history, migrate_links, store

    target = tmp_path / "picked"
    root = target / store._APP_DIR_NAME
    root.mkdir(parents=True)
    _write(
        root / "links.json",
        json.dumps([{"link": r"C:\Games", "target": r"D:\Games", "created_at": 1}]),
    )
    _write(
        root / "migration_history.json",
        json.dumps(
            [
                {
                    "id": "h1",
                    "ts": 1.0,
                    "op": "migrate",
                    "status": "success",
                    "source": r"C:\Games",
                    "target": r"D:\Games",
                }
            ]
        ),
    )

    info = store.switch_data_root(str(target))

    assert info["target_had_config"] is True
    assert [rec["link"] for rec in migrate_links.load_links()] == [r"C:\Games"]
    assert [rec["id"] for rec in migrate_history.load_history()] == ["h1"]


def test_switch_rejects_unusable_folder(tmp_path):
    """文件夹不可用时不切换，原数据目录保持不变。"""
    from core import store

    before = store.app_data_dir()
    blocked = tmp_path / "blocked.txt"
    _write(blocked, "not a folder")

    with pytest.raises(store.DataDirUnavailable):
        store.switch_data_root(str(blocked))
    with pytest.raises(store.DataDirUnavailable):
        store.switch_data_root("")

    assert store.app_data_dir() == before


def test_get_storage_status_returns_readable_message(monkeypatch):
    """数据目录不可用时只回可读说明，不把机器原因键给界面。"""
    from core import store

    monkeypatch.setattr(
        store,
        "storage_status",
        lambda: {"ok": False, "path": r"C:\blocked", "reason": "permission"},
    )

    result = _api().get_storage_status()

    assert result["ok"] is False
    assert result["message"]
    for leaked in ("OSError", "storage_unavailable"):
        assert leaked not in result["message"]


def test_set_language_keeps_working_without_writable_settings(monkeypatch):
    """数据目录还没确定时，切换界面语言不因为写不进设置而报错。"""
    from core import store

    def _blocked() -> None:
        raise OSError("blocked")

    monkeypatch.setattr(store, "_persist", _blocked)

    assert _api().set_language("zh") == {"ok": True, "lang": "zh"}
    assert store.get_lang() == "zh"


def test_dialog_initial_dir_uses_current_user_location(monkeypatch, tmp_path):
    """原生对话框的起始位置来自当前运行账户可访问的目录。"""
    from app import dialogs
    from core import store

    picked = tmp_path / "user-folder"
    picked.mkdir()
    monkeypatch.setattr(store, "preferred_pick_dir", lambda: str(picked))

    def _no_snapshot_dir() -> str:
        raise OSError("blocked")

    monkeypatch.setattr(store, "get_snapshot_dir", _no_snapshot_dir)

    assert dialogs.pick_initial_dir() == os.path.abspath(str(picked))


def test_switch_remembers_user_choice(tmp_path):
    """用户主动选择的数据目录会写进当前账户下的指针文件。"""
    from core import store

    local = tmp_path / "local"
    local.mkdir()
    target = tmp_path / "picked"
    target.mkdir()
    previous = store._current_user
    store._current_user = _FakeUser(local)
    try:
        info = store.switch_data_root(str(target))
    finally:
        store._current_user = previous

    pointer = local / (store._APP_DIR_NAME + store._DATA_DIR_POINTER_SUFFIX)
    expected = os.path.join(os.path.abspath(str(target)), store._APP_DIR_NAME)
    assert info["pointer_saved"] is True
    assert pointer.read_text(encoding="utf-8").strip() == expected


def test_switch_appends_app_subdir(tmp_path):
    """用户选定目录后，数据落在其下的应用子目录里，重选该子目录不重复叠加。"""
    from core import store

    target = tmp_path / "picked"
    target.mkdir()
    old_root = tmp_path / "old-root"
    old_root.mkdir()
    _write(old_root / "settings.yaml", "common:\n  lang: zh\n")

    store._storage_initialized = True
    store._storage_ok = False
    store._storage_state = {
        "ok": False,
        "app_data_dir": str(old_root),
        "path": str(old_root),
        "reason": "permission",
    }
    store._app_data_root = str(old_root)
    store._snapshot_dir = ""
    store._current_user = None
    try:
        info = store.switch_data_root(str(target))
        root = target / store._APP_DIR_NAME
        assert info["app_data_dir"] == str(root)
        assert (root / "settings.yaml").is_file()

        again = store.switch_data_root(str(root))
        assert again["app_data_dir"] == str(root)
        assert (root / "settings.yaml").is_file()
    finally:
        store.set_data_root_for_tests(str(tmp_path / "reset"))


def test_identity_failure_messages_do_not_ask_for_folder():
    """账户解析失败时文案只讲重启，不再引导反复选文件夹。"""
    from app import winmsgs

    unavailable = winmsgs._data_dir_unavailable_message("", "identity_unavailable")
    assert "choose a folder" not in unavailable.lower()
    assert "restart" in unavailable.lower()

    choice = winmsgs._data_dir_choice_error_message("identity_unavailable")
    assert "choose another folder" not in choice.lower()
    assert "restart" in choice.lower()


def test_switch_resets_snapshot_dir_inside_old_root(tmp_path):
    """快照目录位于旧数据目录内时，切换后改回新数据目录下的内置目录。"""
    from core import store

    old_root = store.app_data_dir()
    old_snap = store.builtin_snapshot_dir()
    store.set_snapshot_dir(old_snap)

    target = tmp_path / "picked"
    target.mkdir()
    info = store.switch_data_root(str(target))

    assert info["snapshot_dir_reset"] is True
    assert store.get_snapshot_dir_configured() == ""
    assert not os.path.exists(old_root)


def test_storage_status_flags_builtin_snapshot_unavailable(tmp_path):
    """内置快照目录不可用时，状态里要带专用标记供界面区分。"""
    from core import store

    root = tmp_path / "data"
    root.mkdir()
    (root / "snapshots").write_text("not a folder", encoding="utf-8")

    store.set_data_root_for_tests(str(root))
    store._snapshot_dir = ""
    try:
        status = store.storage_status()
    finally:
        store.set_data_root_for_tests(str(tmp_path / "reset"))

    assert status["ok"] is False
    assert status["snapshot_dir_unavailable"] is True
    assert status["path"].endswith("snapshots")


def test_startup_reports_invalid_pointer(tmp_path, monkeypatch):
    """指针记录存在但内容无效时，启动使用默认位置并在状态里标记。"""
    from core import store

    local = tmp_path / "local"
    local.mkdir()
    pointer = local / (store._APP_DIR_NAME + store._DATA_DIR_POINTER_SUFFIX)
    _write(pointer, "relative/path")
    fake = _FakeUser(local)

    state = _init_with_user(monkeypatch, fake)
    try:
        assert state["ok"] is True
        assert state["pointer_invalid"] is True
        assert store.settings_dict()["data_dir_pointer_invalid"] is True
        assert state["app_data_dir"] == str(local / store._APP_DIR_NAME)
    finally:
        store.set_data_root_for_tests(str(tmp_path / "reset"))


def test_switch_rejects_target_inside_current_root(tmp_path):
    """新位置位于现有数据文件夹内部时拒绝，避免移动递归或残留。"""
    from core import store

    old_root = Path(store.app_data_dir())
    inside = old_root / "nested"
    inside.mkdir()

    with pytest.raises(store.DataDirUnavailable) as err:
        store.switch_data_root(str(inside))

    assert err.value.reason == "inside_source"
    assert not (inside / store._APP_DIR_NAME).exists()


def test_switch_keeps_custom_snapshot_dir_in_old_root(tmp_path):
    """旧数据文件夹内的自定义快照目录不随迁移，配置保留且文件仍在。"""
    from core import store

    old_root = Path(store.app_data_dir())
    custom = old_root / "my-snaps"
    custom.mkdir()
    _write(custom / "keep.db", "snapshot")
    store.set_snapshot_dir(str(custom))

    target = tmp_path / "picked"
    target.mkdir()
    info = store.switch_data_root(str(target))

    assert info["snapshot_dir_reset"] is False
    assert os.path.normcase(store.get_snapshot_dir_configured()) == os.path.normcase(
        str(custom)
    )
    assert (custom / "keep.db").is_file()


def test_switch_reports_previous_root_and_target_config(tmp_path):
    """切换结果带旧数据路径与目标原有配置标记，旧设置留在原位置。"""
    from core import store

    old_root = Path(store.app_data_dir())
    store.set_theme("dark")
    target = tmp_path / "picked"
    target_root = target / store._APP_DIR_NAME
    target_root.mkdir(parents=True)
    _write(target_root / "settings.yaml", "common:\n  lang: zh\n")

    info = store.switch_data_root(str(target))

    assert info["target_had_config"] is True
    assert info["previous_root"] == os.path.abspath(str(old_root))
    assert store.get_lang() == "zh"
    assert os.path.isfile(old_root / "settings.yaml")


def test_get_storage_status_keeps_problem_path_for_snapshot(monkeypatch):
    """内置快照目录不可用时，显示用数据根，打开按钮用出问题的快照路径。"""
    from core import store

    monkeypatch.setattr(
        store,
        "storage_status",
        lambda: {
            "ok": False,
            "app_data_dir": r"C:\data",
            "path": r"C:\data\snapshots",
            "reason": "permission",
            "snapshot_dir_unavailable": True,
        },
    )

    result = _api().get_storage_status()

    assert result["problem_path"] == r"C:\data\snapshots"
    assert result["path"] == r"C:\data"
    assert result["message"]


@pytest.mark.skipif(os.name != "nt", reason="盘符与共享路径只在 Windows 上成立")
def test_wipe_app_data_refuses_root_targets(monkeypatch):
    """清理目标异常成磁盘根目录或共享目录时，在删除任何文件之前拒绝。"""
    import shutil as shutil_mod

    from core import store

    removed: list[str] = []
    monkeypatch.setattr(
        shutil_mod, "rmtree", lambda p, *a, **kw: removed.append(str(p))
    )

    for target in ("C:\\", "\\\\server\\share"):
        removed.clear()
        monkeypatch.setattr(store, "_app_base_dir_path", lambda t=target: t)
        result = store.wipe_app_data(delete_data=True)
        assert result["ok"] is False
        assert "unsafe_root" in result["errors"]
        assert removed == []
