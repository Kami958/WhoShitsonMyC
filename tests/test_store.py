"""快照存储管理与应用设置测试。"""

import os

import pytest

from core.models import Entry, SnapshotMeta
from core.snapshot import write_snapshot
from core import store
from core.snapshot import SnapshotError
from core.store import (
    apply_settings,
    builtin_snapshot_dir,
    create_snapshot_folder,
    default_scan_workers,
    default_snapshot_dir,
    delete_snapshot,
    delete_snapshot_folder,
    get_compress_snapshots,
    get_lang,
    get_log_level,
    get_log_sanitize,
    get_scan_workers,
    get_snapshot_dir_configured,
    get_theme,
    get_use_mft,
    get_search_memory_index,
    is_log_level_explicit,
    is_log_sanitize_explicit,
    list_snapshot_folders,
    list_snapshots,
    move_snapshot_to_folder,
    new_snapshot_path,
    rename_snapshot_folder,
    reset_settings_to_defaults,
    sanitize_folder_name,
    set_compress_snapshots,
    set_lang,
    set_log_level,
    set_log_sanitize,
    set_note,
    set_scan_workers,
    set_snapshot_dir,
    set_theme,
    set_use_mft,
    set_search_memory_index,
    settings_path,
    snapshot_content_key,
    snapshot_info,
    _apply_loaded,
    _load_settings_yaml,
    _write_settings_yaml,
)


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    """所有用例把应用数据根指到 tmp，避免读写真实 settings.yaml。"""
    monkeypatch.setattr(store, "_app_base_dir", lambda: str(tmp_path))
    store._data_wiped = False
    store._use_mft = True
    store._compress_snapshots = True
    store._search_memory_index = True
    store._log_sanitize = True
    store._log_sanitize_explicit = None
    store._log_level = "INFO"
    store._log_level_explicit = None
    store._scan_workers = store.default_scan_workers()
    store._lang = "en"
    store._theme = "light"
    store._snapshot_dir = ""
    store._delete_blacklist = []
    yield


def _write(db, root, total, when):
    entries = [Entry(id=1, parent_id=None, name="", size=total, is_dir=True)]
    meta = SnapshotMeta(root=root, scanned_at=when, total_size=total)
    write_snapshot(db, root, entries, meta)


def test_new_snapshot_path_naming(tmp_path):
    p = new_snapshot_path("C:\\", when=0.0, out_dir=str(tmp_path))
    assert os.path.basename(p).startswith("C_")
    assert p.endswith(".db")

    p2 = new_snapshot_path("D:\\Games\\Steam", when=0.0, out_dir=str(tmp_path))
    assert "D_Games_Steam" in os.path.basename(p2)


def test_list_snapshots_sorted_newest_first(tmp_path):
    _write(os.path.join(tmp_path, "a.db"), "C:\\", 100, when=10.0)
    _write(os.path.join(tmp_path, "b.db"), "C:\\", 200, when=50.0)
    _write(os.path.join(tmp_path, "c.db"), "C:\\", 300, when=30.0)

    infos = list_snapshots(str(tmp_path))
    assert [i.total_size for i in infos] == [200, 300, 100]  # 按 scanned_at 降序


def test_list_snapshots_skips_bad_files(tmp_path):
    _write(os.path.join(tmp_path, "ok.db"), "C:\\", 100, when=1.0)
    with open(os.path.join(tmp_path, "broken.db"), "wb") as f:
        f.write(b"garbage")
    with open(os.path.join(tmp_path, "ignore.txt"), "w") as f:
        f.write("not a db")

    infos = list_snapshots(str(tmp_path))
    assert len(infos) == 1
    assert infos[0].total_size == 100


def test_delete_snapshot(tmp_path):
    db = os.path.join(tmp_path, "x.db")
    _write(db, "C:\\", 100, when=1.0)
    assert os.path.exists(db)
    delete_snapshot(db)
    assert not os.path.exists(db)
    delete_snapshot(db)  # 再删不存在的不报错


def test_sanitize_folder_name():
    assert sanitize_folder_name("") == ""
    assert sanitize_folder_name("  工作  ") == "工作"
    with pytest.raises(ValueError):
        sanitize_folder_name("a/b")
    with pytest.raises(ValueError):
        sanitize_folder_name("..")
    with pytest.raises(ValueError):
        sanitize_folder_name("a:b")


def test_list_snapshots_includes_one_level_folder(tmp_path):
    _write(os.path.join(tmp_path, "root.db"), "C:\\", 10, when=1.0)
    sub = tmp_path / "归档"
    sub.mkdir()
    _write(str(sub / "in_folder.db"), "D:\\", 20, when=2.0)
    deeper = sub / "nested"
    deeper.mkdir()
    _write(str(deeper / "too_deep.db"), "E:\\", 30, when=3.0)

    infos = list_snapshots(str(tmp_path))
    paths = {os.path.basename(i.path): i for i in infos}
    assert set(paths) == {"root.db", "in_folder.db"}
    assert paths["root.db"].folder == ""
    assert paths["in_folder.db"].folder == "归档"
    assert "folder" in paths["in_folder.db"].to_dict()


def test_create_move_rename_delete_folder(tmp_path):
    db = os.path.join(tmp_path, "x.db")
    _write(db, "C:\\", 100, when=1.0)

    name = create_snapshot_folder("Games", out_dir=str(tmp_path))
    assert name == "Games"
    assert "Games" in list_snapshot_folders(str(tmp_path))

    new_path = move_snapshot_to_folder(db, "Games", out_dir=str(tmp_path))
    assert os.path.isfile(new_path)
    assert os.path.basename(os.path.dirname(new_path)) == "Games"
    info = snapshot_info(new_path, base_dir=str(tmp_path))
    assert info.folder == "Games"

    renamed = rename_snapshot_folder("Games", "游戏", out_dir=str(tmp_path))
    assert renamed == "游戏"
    assert not os.path.isdir(os.path.join(tmp_path, "Games"))
    infos = list_snapshots(str(tmp_path))
    assert len(infos) == 1
    assert infos[0].folder == "游戏"

    # 非空不能删
    with pytest.raises(OSError):
        delete_snapshot_folder("游戏", out_dir=str(tmp_path), force=False)

    back = move_snapshot_to_folder(infos[0].path, "", out_dir=str(tmp_path))
    assert os.path.dirname(back) == str(tmp_path) or os.path.samefile(
        os.path.dirname(back), str(tmp_path)
    )
    # 移空后空夹应被清掉
    assert "游戏" not in list_snapshot_folders(str(tmp_path))

    create_snapshot_folder("空夹", out_dir=str(tmp_path))
    delete_snapshot_folder("空夹", out_dir=str(tmp_path))
    assert "空夹" not in list_snapshot_folders(str(tmp_path))


def test_snapshot_info_reads_external_path(tmp_path):
    """用户把快照挪到别处后，仍可按绝对路径读摘要。"""
    other = tmp_path / "elsewhere"
    other.mkdir()
    db = other / "moved.db"
    _write(str(db), "D:\\Games", 4096, when=42.0)
    info = snapshot_info(str(db))
    assert info.root == "D:\\Games"
    assert info.total_size == 4096
    assert info.scanned_at == 42.0
    assert info.path == os.path.abspath(str(db))


def test_snapshot_info_rejects_non_snapshot(tmp_path):
    bad = tmp_path / "note.txt"
    bad.write_text("nope", encoding="utf-8")
    with pytest.raises(SnapshotError):
        snapshot_info(str(bad))


def test_snapshot_content_key_stable_across_paths(tmp_path):
    """复制到另一路径后 content_key 相同（导入去重依据）。"""
    a = tmp_path / "a.db"
    bdir = tmp_path / "other"
    bdir.mkdir()
    b = bdir / "b.db"
    _write(str(a), "C:\\", 1000, when=12.5)
    _write(str(b), "C:\\", 1000, when=12.5)
    ia, ib = snapshot_info(str(a),), snapshot_info(str(b))
    assert ia.content_key == ib.content_key
    assert ia.content_key == snapshot_content_key("C:\\", 12.5, 1000, 0, 0)
    assert "content_key" in ia.to_dict()

    # 不同扫描时间 → 不同指纹
    c = tmp_path / "c.db"
    _write(str(c), "C:\\", 1000, when=99.0)
    assert snapshot_info(str(c)).content_key != ia.content_key


def test_snapshot_note_in_file(tmp_path):
    """备注写入快照文件本身；复制文件后新路径也带备注。"""
    import shutil

    a = tmp_path / "a.db"
    b = tmp_path / "copy.db"
    _write(str(a), "E:\\Data", 500, when=7.0)
    assert set_note(str(a), "  weekly backup  ") == "weekly backup"
    assert snapshot_info(str(a)).note == "weekly backup"
    assert snapshot_info(str(a)).to_dict()["note"] == "weekly backup"
    shutil.copy2(str(a), str(b))
    assert snapshot_info(str(b)).note == "weekly backup"
    assert set_note(str(a), "") == ""
    assert snapshot_info(str(a)).note == ""
    # 清除 a 不影响已复制的 b
    assert snapshot_info(str(b)).note == "weekly backup"


def test_scan_workers_default_is_positive_and_clamped():
    """默认线程数：HDD→1，SSD/未知→CPU 核数。"""
    d = default_scan_workers()
    assert d >= 1
    assert d <= max(1, os.cpu_count() or 1)
    assert isinstance(d, int)


def test_set_and_get_scan_workers_roundtrip():
    assert set_scan_workers(4) == 4
    assert get_scan_workers() == 4


def test_set_scan_workers_clamps_out_of_range():
    assert set_scan_workers(0) == 1
    assert set_scan_workers(999) == 128


def test_compress_snapshots_defaults_on():
    set_compress_snapshots(True)
    assert get_compress_snapshots() is True
    assert set_compress_snapshots(False) is False
    assert get_compress_snapshots() is False
    assert set_compress_snapshots(True) is True


def test_use_mft_defaults_on():
    assert get_use_mft() is True
    assert set_use_mft(False) is False
    assert get_use_mft() is False
    set_use_mft(True)


def test_search_memory_index_defaults_on():
    assert get_search_memory_index() is True
    assert set_search_memory_index(False) is False
    assert get_search_memory_index() is False
    assert set_search_memory_index(True) is True


def test_log_sanitize_defaults_on_and_explicit_flag():
    assert get_log_sanitize() is True
    assert is_log_sanitize_explicit() is False
    assert set_log_sanitize(False) is False
    assert get_log_sanitize() is False
    assert is_log_sanitize_explicit() is True
    assert set_log_sanitize(True) is True
    assert get_log_sanitize() is True


def test_log_level_defaults_info_and_explicit_flag():
    assert get_log_level() == "INFO"
    assert is_log_level_explicit() is False
    assert set_log_level("debug") == "DEBUG"
    assert get_log_level() == "DEBUG"
    assert is_log_level_explicit() is True
    assert set_log_level("WARNING") == "WARN"
    assert get_log_level() == "WARN"
    assert set_log_level("nope") == "INFO"


def test_yaml_roundtrip_helpers(tmp_path):
    path = str(tmp_path / "settings.yaml")
    custom = str(tmp_path / "snaps")
    data = {
        "scan_workers": 6,
        "compress_snapshots": False,
        "use_mft": True,
        "search_memory_index": False,
        "log_sanitize": False,
        "log_level": "DEBUG",
        "lang": "zh",
        "theme": "light",
        "snapshot_dir": custom,
    }
    _write_settings_yaml(path, data)
    # 新格式：顶层 common: 节；不再写 persist
    raw = open(path, encoding="utf-8").read()
    assert "common:" in raw
    assert "persist" not in raw
    assert "\n  scan_workers:" in raw or "\n  scan_workers: " in raw
    assert "search_memory_index: false" in raw
    assert "log_sanitize: false" in raw
    assert "log_level: DEBUG" in raw
    assert "log_to_file" not in raw
    loaded = _load_settings_yaml(path)
    assert "persist" not in loaded
    # common / ai 同为顶层节；值先是字符串，类型在 _apply_loaded 转
    common = loaded["common"]
    assert common["scan_workers"] == "6"
    assert common["compress_snapshots"] == "false"
    assert common["use_mft"] == "true"
    assert common["search_memory_index"] == "false"
    assert common["log_sanitize"] == "false"
    assert common["log_level"] == "DEBUG"
    assert "log_to_file" not in common
    assert common["lang"] == "zh"
    assert common["theme"] == "light"
    assert common["snapshot_dir"] == os.path.abspath(custom)
    assert isinstance(loaded.get("ai"), dict)
    _apply_loaded(loaded)
    assert get_scan_workers() == 6
    assert get_compress_snapshots() is False
    assert get_use_mft() is True
    assert get_search_memory_index() is False
    assert get_log_sanitize() is False
    assert is_log_sanitize_explicit() is True
    assert get_log_level() == "DEBUG"
    assert is_log_level_explicit() is True


def test_yaml_loads_legacy_flat_format(tmp_path):
    """旧版顶层扁平 YAML 仍可读取；persist 键可解析但不影响行为。"""
    path = str(tmp_path / "settings.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "persist: true\n"
            "scan_workers: 4\n"
            "compress_snapshots: false\n"
            "use_mft: true\n"
            "lang: en\n"
            "theme: dark\n"
            "snapshot_dir: ''\n"
        )
    loaded = _load_settings_yaml(path)
    assert loaded.get("persist") == "true"
    assert loaded.get("scan_workers") == "4"
    assert loaded.get("compress_snapshots") == "false"
    assert loaded.get("use_mft") == "true"
    _apply_loaded(loaded)
    assert get_scan_workers() == 4
    assert get_compress_snapshots() is False
    assert get_use_mft() is True


def test_common_lang_present_for_startup_branch(tmp_path):
    """分节 YAML 的 lang 在 common 下；启动分支须用 _common_view，不能 ``\"lang\" in raw``。

    回归：旧逻辑只查顶层键，会把 common.lang 当成「无语言」并用系统 UI 覆盖。
    """
    from core.store import _common_view

    path = settings_path()
    set_lang("en")
    set_theme("dark")
    assert os.path.isfile(path)
    raw = open(path, encoding="utf-8").read()
    assert "common:" in raw
    assert "lang:" in raw

    loaded = _load_settings_yaml(path)
    # 分节格式：顶层没有 lang，只有 common.lang
    assert "lang" not in loaded or not isinstance(loaded.get("lang"), str)
    assert isinstance(loaded.get("common"), dict)
    assert loaded["common"].get("lang") == "en"

    common = _common_view(loaded)
    assert "lang" in common
    assert common["lang"] == "en"

    # 模拟 app.main 正确分支：有 common.lang 则保留 store（不写系统语言）
    store._lang = "zh"  # 假装被系统语言污染
    if "lang" in common:
        _apply_loaded(loaded)
    assert get_lang() == "en"

    # 旧扁平：_common_view 仍能看到顶层 lang
    flat = {"lang": "zh", "theme": "light"}
    assert _common_view(flat).get("lang") == "zh"
    assert "lang" not in _common_view({})


def test_wipe_app_data_delete_and_settings_only(tmp_path, monkeypatch):
    """卸载清理：删掉整个应用数据文件夹 / 仅删 settings.yaml。"""
    from core.store import wipe_app_data

    app_base = tmp_path / "WhoShitsOnMyC"
    app_base.mkdir()
    monkeypatch.setattr(store, "_app_base_dir_path", lambda: str(app_base))
    monkeypatch.setattr(store, "_app_base_dir", lambda: str(app_base))

    snaps = app_base / "snapshots"
    snaps.mkdir()
    (snaps / "a.db").write_bytes(b"x")
    conf = app_base / "settings.yaml"
    conf.write_text("common:\n  scan_workers: 4\n", encoding="utf-8")

    r = wipe_app_data(delete_data=True)
    assert r["ok"] is True
    assert r["deleted_data"] is True
    assert not app_base.exists()  # 整个 WhoShitsOnMyC 文件夹去掉

    # 仅删配置：目录本身保留
    app_base.mkdir()
    conf.write_text("common:\n  scan_workers: 4\n", encoding="utf-8")
    (app_base / "keep.me").write_text("y", encoding="utf-8")
    r2 = wipe_app_data(delete_data=False)
    assert r2["ok"] is True
    assert r2["deleted_data"] is False
    assert not conf.exists()
    assert (app_base / "keep.me").is_file()
    assert app_base.is_dir()


def test_wipe_app_data_leaves_custom_snapshot_dir(tmp_path, monkeypatch):
    """勾选删数据时：删掉应用数据根文件夹，不碰外部自定义快照目录。"""
    from core.store import wipe_app_data

    app_base = tmp_path / "appdata"
    app_base.mkdir()
    monkeypatch.setattr(store, "_app_base_dir_path", lambda: str(app_base))
    monkeypatch.setattr(store, "_app_base_dir", lambda: str(app_base))

    custom = tmp_path / "elsewhere_snaps"
    custom.mkdir()
    (custom / "keep.txt").write_text("safe", encoding="utf-8")
    (custom / "x.db").write_bytes(b"db")
    (custom / "y.dbz").write_bytes(b"dbz")
    store._snapshot_dir = str(custom)

    snaps = app_base / "snapshots"
    snaps.mkdir()
    (snaps / "builtin.db").write_bytes(b"b")

    r = wipe_app_data(delete_data=True)
    assert r["ok"] is True
    assert not app_base.exists()
    # 自定义目录完整保留
    assert custom.is_dir()
    assert (custom / "x.db").is_file()
    assert (custom / "y.dbz").is_file()
    assert (custom / "keep.txt").read_text(encoding="utf-8") == "safe"
    assert store.get_snapshot_dir_configured() == ""


def test_wipe_does_not_recreate_via_base_path(tmp_path, monkeypatch):
    """wipe 使用 path 版 API，不会因 makedirs 把目录又建出来。"""
    from core.store import wipe_app_data

    app_base = tmp_path / "WhoShitsOnMyC"
    app_base.mkdir()
    (app_base / "settings.yaml").write_text(
        "common:\n  scan_workers: 4\n", encoding="utf-8"
    )
    monkeypatch.setattr(store, "_app_base_dir_path", lambda: str(app_base))

    def boom_create():
        raise AssertionError("_app_base_dir should not be called during wipe")

    monkeypatch.setattr(store, "_app_base_dir", boom_create)
    r = wipe_app_data(delete_data=True)
    assert r["ok"] is True
    assert not app_base.exists()


def test_wipe_blocks_makedirs_after_delete(tmp_path, monkeypatch):
    """删数据后 _app_base_dir / builtin_snapshot_dir 不再重建目录。"""
    from core.store import wipe_app_data

    app_base = tmp_path / "WhoShitsOnMyC"
    app_base.mkdir()
    (app_base / "snapshots").mkdir()
    (app_base / "settings.yaml").write_text(
        "common:\n  scan_workers: 4\n", encoding="utf-8"
    )
    monkeypatch.setattr(store, "_app_base_dir_path", lambda: str(app_base))
    store._data_wiped = False

    r = wipe_app_data(delete_data=True)
    assert r["ok"] is True
    assert not app_base.exists()
    assert store._data_wiped is True

    # 即便再调会创建的 API，也不应把空夹建回来
    store._app_base_dir()
    store.builtin_snapshot_dir()
    assert not app_base.exists()
    assert not (app_base / "snapshots").exists()


def test_setters_auto_write_yaml(tmp_path):
    """改设置后自动写 YAML；值未变不强制要求，变化必有文件。"""
    path = settings_path()
    assert path.startswith(str(tmp_path))
    assert not os.path.isfile(path)

    set_scan_workers(3)
    set_compress_snapshots(False)
    set_use_mft(True)
    set_lang("zh")
    set_theme("light")
    custom = str(tmp_path / "my_snaps")
    set_snapshot_dir(custom)

    assert os.path.isfile(path)
    loaded = _load_settings_yaml(path)
    assert "persist" not in loaded
    common = loaded["common"]
    assert common.get("scan_workers") == "3"
    assert common.get("compress_snapshots") == "false"
    assert common.get("use_mft") == "true"
    assert common.get("lang") == "zh"
    assert common.get("theme") == "light"
    assert common.get("snapshot_dir") == os.path.abspath(custom)

    set_scan_workers(7)
    set_theme("dark")
    loaded2 = _load_settings_yaml(path)
    assert loaded2["common"].get("scan_workers") == "7"
    assert loaded2["common"].get("theme") == "dark"


def test_reset_settings_to_defaults_deletes_yaml(tmp_path):
    """恢复默认：删配置文件，内存回内置默认；不碰快照文件。"""
    set_scan_workers(9)
    set_compress_snapshots(False)
    set_use_mft(True)
    set_lang("en")
    set_theme("light")
    custom = str(tmp_path / "elsewhere")
    set_snapshot_dir(custom)
    path = settings_path()
    assert os.path.isfile(path)

    out = reset_settings_to_defaults(lang="zh")
    assert not os.path.isfile(path)
    assert out["scan_workers"] == default_scan_workers()
    assert out["compress_snapshots"] is True
    assert out["use_mft"] is True
    assert out["theme"] == "light"
    assert out["lang"] == "zh"
    assert out["snapshot_dir_configured"] == ""
    assert out["settings_file_exists"] is False
    assert get_scan_workers() == default_scan_workers()
    assert get_lang() == "zh"
    assert get_theme() == "light"
    assert get_snapshot_dir_configured() == ""


def test_snapshot_dir_custom_and_reset(tmp_path):
    builtin = builtin_snapshot_dir()
    assert default_snapshot_dir() == builtin
    assert get_snapshot_dir_configured() == ""

    custom = str(tmp_path / "elsewhere")
    effective = set_snapshot_dir(custom)
    assert effective == os.path.abspath(custom)
    assert os.path.isdir(effective)
    assert default_snapshot_dir() == effective
    # 新快照路径应落在自定义目录
    p = new_snapshot_path("C:\\Games")
    assert p.startswith(effective)

    back = set_snapshot_dir("")
    assert back == builtin
    assert get_snapshot_dir_configured() == ""


def test_apply_loaded_partial_keeps_missing_keys():
    """有 yaml 内容就覆盖；未写的项保持默认；persist 忽略。"""
    store._scan_workers = store.default_scan_workers()
    store._compress_snapshots = True
    store._use_mft = False
    _apply_loaded(
        {
            "persist": False,  # 旧键，忽略
            "scan_workers": 6,
            "use_mft": True,
        }
    )
    assert get_scan_workers() == 6
    assert get_use_mft() is True
    # compress 未出现 → 保持默认 True
    assert get_compress_snapshots() is True


def test_delete_blacklist_roundtrip_and_apply(tmp_path):
    """删除黑名单：apply 写入、YAML 往返、reset 清空。"""
    entries = [
        {"path": r"C:\Windows", "mode": "prefix"},
        {"path": r"D:\keep", "mode": "exact"},
        {"path": r".*\\.tmp$", "mode": "regex"},
    ]
    out = apply_settings({"delete_blacklist": entries})
    got = out.get("delete_blacklist") or []
    assert any(e.get("path") == r"C:\Windows" and e.get("mode") == "prefix" for e in got)
    assert any(e.get("path") == r"D:\keep" and e.get("mode") == "exact" for e in got)

    path = settings_path()
    assert os.path.isfile(path)
    # 重新加载
    store._delete_blacklist = []
    store.reload_settings_from_disk()
    again = store.get_delete_blacklist()
    assert any(e.get("path") == r"C:\Windows" for e in again)

    store.reset_settings_to_defaults(lang="en")
    assert store.get_delete_blacklist() == []


def test_apply_settings_batch_always_writes(tmp_path):
    """设置页「完成」：一次提交多项并写 yaml。"""
    custom = str(tmp_path / "batch_snaps")
    out = apply_settings(
        {
            "scan_workers": 5,
            "compress_snapshots": False,
            "use_mft": True,
            "log_sanitize": False,
            "log_level": "DEBUG",
            "snapshot_dir": custom,
            "persist_settings": False,  # 旧键忽略，仍写盘
        }
    )
    assert get_log_sanitize() is False
    assert is_log_sanitize_explicit() is True
    assert get_log_level() == "DEBUG"
    assert is_log_level_explicit() is True
    assert out["scan_workers"] == 5
    assert out["compress_snapshots"] is False
    assert out["use_mft"] is True
    assert out["log_level"] == "DEBUG"
    assert "log_to_file" not in out
    assert "persist_settings" not in out
    assert out["snapshot_dir_is_custom"] is True
    assert out["settings_file_exists"] is True
    path = settings_path()
    assert os.path.isfile(path)
    loaded = _load_settings_yaml(path)
    common = loaded["common"]
    assert common.get("scan_workers") == "5"
    assert common.get("compress_snapshots") == "false"
    assert common.get("use_mft") == "true"
    assert common.get("log_level") == "DEBUG"
    assert "log_to_file" not in common
    assert common.get("snapshot_dir") == os.path.abspath(custom)
    assert "persist" not in loaded
    assert "persist" not in common


def test_apply_settings_migrates_snapshots(tmp_path):
    """更改快照目录时把原目录 .db/.dbz 迁到新目录。"""
    src = tmp_path / "src_snaps"
    dst = tmp_path / "dst_snaps"
    src.mkdir()
    a = src / "a.db"
    b = src / "b.dbz"
    _write(str(a), "C:\\", 100, when=1.0)
    b.write_bytes(b"pk\x03\x04fake")  # 非完整 zip 也算文件名命中迁移
    # 非快照文件不应迁移
    (src / "notes.txt").write_text("x", encoding="utf-8")

    store._snapshot_dir = str(src)
    events: list[dict] = []
    out = apply_settings(
        {"snapshot_dir": str(dst)},
        progress=events.append,
    )
    assert out["snapshot_dir_changed"] is True
    assert out["migrate"]["moved"] == 2
    assert out["migrate"]["failed"] == 0
    assert out["migrate"]["total"] == 2
    assert not a.exists()
    assert not b.exists()
    assert (dst / "a.db").is_file()
    assert (dst / "b.dbz").is_file()
    assert (src / "notes.txt").is_file()
    # progress：start + 每个文件一次
    assert events and events[0]["status"] == "start" and events[0]["total"] == 2
    assert len(events) == 3
    assert events[-1]["done"] == 2 and events[-1]["moved"] == 2
    assert os.path.isfile(settings_path())

    # 目标已有同名 → 跳过
    c = src / "c.db"
    _write(str(c), "D:\\", 50, when=2.0)
    (dst / "c.db").write_bytes(b"existing")
    store._snapshot_dir = str(src)
    out2 = apply_settings({"snapshot_dir": str(dst)})
    assert out2["migrate"]["skipped"] >= 1
    assert (dst / "c.db").read_bytes() == b"existing"
    assert c.is_file()  # 跳过则源文件保留


def test_apply_loaded_empty_keeps_defaults():
    store._scan_workers = 5
    store._use_mft = False
    _apply_loaded({})
    assert get_scan_workers() == 5
    assert get_use_mft() is False


def test_yaml_keeps_unknown_keys_and_sections(tmp_path):
    """解析不丢未知键/节；common 与 ai 同为顶层节；apply 只消费认识的字段。"""
    path = str(tmp_path / "settings.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "common:\n"
            "  scan_workers: 8\n"
            "  future_flag: true\n"
            "ai:\n"
            "  enabled: true\n"
            "  model: m1\n"
            "  mystery: keep-me\n"
            "other:\n"
            "  foo: bar\n"
        )
    loaded = _load_settings_yaml(path)
    assert set(loaded.keys()) >= {"common", "ai", "other"}
    assert loaded["common"].get("scan_workers") == "8"
    assert loaded["common"].get("future_flag") == "true"
    assert loaded["ai"].get("enabled") == "true"
    assert loaded["ai"].get("model") == "m1"
    assert loaded["ai"].get("mystery") == "keep-me"
    assert loaded.get("other") == {"foo": "bar"}

    store._scan_workers = store.default_scan_workers()
    store._ai_enabled = False
    store._ai_model = ""
    _apply_loaded(loaded)
    assert get_scan_workers() == 8
    assert store.get_ai_settings()["enabled"] is True
    assert store.get_ai_settings()["model"] == "m1"
