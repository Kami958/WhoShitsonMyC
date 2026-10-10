"""桥接层调用冒烟：把 Api 的方法真调用一遍。

前端点一下按钮，走的就是这些方法。单元测试不构造真正的 Api，静态检查不看运行时，
所以「拆包时漏了一行初始化」「少了 @staticmethod」这类缺陷只有真调用才会暴露：
``_summary`` 掉了 @staticmethod 之后，界面每次点对比都弹出参数个数错误，而 320 条
测试全绿。

这里把应用数据目录指到临时目录（``LOCALAPPDATA``），设置、快照、日志全部落在
临时目录里，不碰用户真实数据；窗口换成假对象。会弹原生对话框、开资源管理器、
退出进程的方法不调用，只断言它们还存在。
"""

from __future__ import annotations

import time

import pytest

# 这些方法会弹系统的文件对话框、打开资源管理器或结束进程，测试里只检查存在。
DIALOG_METHODS = (
    "choose_folder",
    "choose_snapshot_dir",
    "choose_snapshot_files",
    "delete_snapshot_folder",
    "export_ai_chat",
    "export_app_log",
    "import_ai_chat",
    "open_folder",
    "open_snapshot_dir",
    "open_url",
    "pick_snapshot_dir",
    "quit_app",
    "reveal_path",
    "set_window",
)

# 无参只读方法：调用它们只为了证明不抛异常。
READ_ONLY_CALLS = (
    "get_app_log",
    "list_modules",
    "list_fixed_drives",
    "list_snapshots",
    "list_snapshot_folders",
    "list_directory_links",
    "list_migration_history",
    "link_find_status",
    "link_find_result",
    "search_preheat_status",
)


class _Handler:
    """事件处理器占位，只支持 ``events.名字 += 回调``。"""

    def __iadd__(self, callback: object) -> "_Handler":
        return self


class _Events:
    """pywebview 的窗口事件集合，任何事件名都给一个占位处理器。"""

    def __getattr__(self, name: str) -> _Handler:
        handler = _Handler()
        setattr(self, name, handler)
        return handler


class _FakeWindow:
    """假窗口：吸收事件推送，尺寸相关方法只记录。"""

    def __init__(self) -> None:
        self.events = _Events()
        self.native = None
        self.title = ""
        self.width = 1000
        self.height = 700
        self.x = 0
        self.y = 0

    def evaluate_js(self, script: str) -> None:
        pass

    def resize(self, width: int, height: int) -> None:
        self.width, self.height = width, height

    def show(self) -> None:
        pass

    def hide(self) -> None:
        pass

    def destroy(self) -> None:
        pass

    def minimize(self) -> None:
        pass

    def restore(self) -> None:
        pass


@pytest.fixture
def api(monkeypatch, tmp_path):
    """构造一个数据目录落在临时目录里的 Api。

    设置、语言都写在 core.store 的模块级全局里，会留给同一进程里后面的用例，
    所以用完要把语言改回去。
    """
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    import app
    from core import fs_migrate, i18n

    language = i18n.get_lang()
    instance = app.Api()
    instance.set_window(_FakeWindow())
    yield instance
    instance.set_language(language)
    # 迁移会把备份后缀改成当前界面语言的写法，改回默认的中文写法
    fs_migrate.set_backup_suffix("")
    fs_migrate.set_old_target_suffix("")


@pytest.fixture
def tree(tmp_path):
    """待扫描的目录：一个子目录、一个小文件、一个大文件。"""
    root = tmp_path / "root"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.txt").write_text("x" * 200, encoding="utf-8")
    (root / "big.bin").write_bytes(b"y" * 4096)
    return root


def call(api, name, *args):
    """调用 Api 方法，方法不存在直接判失败。"""
    method = getattr(api, name, None)
    assert method is not None, f"Api 上没有 {name}"
    return method(*args)


def scan(api, root, expect_at_least, timeout=60.0):
    """扫一个目录，等快照数量达标，返回全部快照。"""
    call(api, "start_scan", str(root))
    deadline = time.time() + timeout
    snapshots = []
    while time.time() < deadline:
        snapshots = call(api, "list_snapshots") or []
        if len(snapshots) >= expect_at_least:
            break
        time.sleep(0.1)
    call(api, "cancel_scan")
    return snapshots


def test_all_bridge_methods_exist(api):
    """前端能调到的名字都在 Api 上，对话框类方法也不能少。"""
    missing = [name for name in DIALOG_METHODS if not callable(getattr(api, name, None))]
    assert not missing, f"Api 缺少方法：{missing}"


def test_read_only_calls_do_not_raise(api):
    """无参只读方法在空数据目录下也要正常返回。"""
    for name in READ_ONLY_CALLS:
        result = call(api, name)
        assert isinstance(result, (dict, list)), f"{name} 返回了 {result!r}"
        if isinstance(result, dict):
            assert "error" not in result, f"{name} 出错：{result}"


def test_compare_flow(api, tree):
    """扫描两次、对比、下钻、搜索、备注、置顶、归文件夹、删除，整条链路。"""
    snapshots = scan(api, tree, 1)
    assert snapshots, "扫描第一份快照失败"
    (tree / "docs" / "a.txt").write_text("x" * 900, encoding="utf-8")
    (tree / "new.txt").write_text("new", encoding="utf-8")
    (tree / "big.bin").unlink()
    # 快照文件名带秒级时间戳，两次扫描隔开一秒才不会撞名。
    time.sleep(1.2)
    snapshots = scan(api, tree, 2)
    assert len(snapshots) >= 2, f"第二份快照没有产出：{snapshots}"

    first, second = snapshots[0]["path"], snapshots[1]["path"]

    # 对比：_summary 少了 @staticmethod 时这里返回参数个数错误。
    result = call(api, "compare", first, second)
    assert "error" not in result, f"对比失败：{result}"
    summary = result["summary"]
    assert summary["old"]["root"] == str(tree)
    assert summary["new"]["root"] == str(tree)
    assert summary["old"]["scanned_at"] != summary["new"]["scanned_at"]

    # 下钻：根目录这一层能看到刚改动的文件。
    children = call(api, "get_children", first, second, "")
    assert "error" not in children, f"下钻失败：{children}"
    names = {node["name"] for node in children["nodes"]}
    assert "new.txt" in names

    found = call(api, "search_diff", first, second, "a")
    assert "error" not in found, f"搜索失败：{found}"
    assert any("a.txt" in node["path"] for node in found["nodes"])

    infos = call(api, "read_snapshot_infos", [first, second])
    assert "error" not in infos, f"读取快照信息失败：{infos}"

    assert call(api, "set_snapshot_note", first, "冒烟备注")["ok"] is True
    pinned = call(api, "set_pinned_snapshots", [first])
    assert pinned["ok"] is True and pinned["pinned_snapshots"]

    assert call(api, "create_snapshot_folder", "冒烟文件夹")["ok"] is True
    moved = call(api, "move_snapshot_to_folder", first, "冒烟文件夹")
    assert "error" not in moved, f"归入文件夹失败：{moved}"
    call(api, "close_diff_session")

    deleted = call(api, "delete_snapshot", moved["path"], True)
    assert deleted["ok"] is True, f"删除快照失败：{deleted}"


def test_settings_roundtrip(api):
    """设置项写入后能读回来，恢复默认也有响应。"""
    call(api, "set_snapshot_dir", "")
    assert call(api, "set_scan_workers", 3)["ok"] is True
    assert call(api, "set_use_mft", False)["ok"] is True
    assert call(api, "set_compress_snapshots", False)["ok"] is True
    call(api, "set_theme", "dark")
    call(api, "set_language", "zh")

    settings = call(api, "get_settings")
    assert settings["scan_workers"] == 3
    assert settings["use_mft"] is False

    # 恢复默认要重新读一遍全部设置项，从前这里因为 _modules 没初始化而抛异常。
    assert call(api, "reset_settings")["ok"] is True
    assert call(api, "get_settings")["scan_workers"] != 3


def test_module_dispatch(api):
    """可选模块接口在不启用 AI 时返回可读错误，而不是抛异常。"""
    assert isinstance(call(api, "list_modules"), dict)
    assert "error" in call(api, "module_invoke", "ai", "chat", {})


def test_migration_flow(api, tmp_path):
    """目录迁移：校验、启动、查历史、取消。"""
    src = tmp_path / "mig_src"
    src.mkdir()
    (src / "f.txt").write_text("m", encoding="utf-8")
    dst = tmp_path / "mig_dst"

    checked = call(api, "validate_directory_migration", str(src), str(dst))
    assert isinstance(checked, dict)

    call(api, "start_directory_migration", str(src), str(dst))
    # 历史里可能还留着别的用例写下的记录，只等本次迁移那一条。
    deadline = time.time() + 15
    history: list = []
    mine: list = []
    while time.time() < deadline:
        history = call(api, "list_migration_history").get("history") or []
        mine = [item for item in history if item["target"] == str(dst)]
        if mine:
            break
        time.sleep(0.1)
    call(api, "cancel_directory_migration")
    assert mine, f"迁移历史里没有本次迁移：{history}"
    assert mine[0]["status"] == "success", mine[0]
    assert isinstance(call(api, "clear_migration_history"), dict)


def test_remigration_flow(api, tmp_path, monkeypatch):
    """再次迁移：数据换到第三个位置，旧目标改名保留，再从接口删掉它。"""
    from core import migrate_links

    # 登记文件指向临时目录，别往真实 app_data_dir 里写
    monkeypatch.setattr(
        migrate_links, "_registry_path", lambda: str(tmp_path / "links.json")
    )

    def _wait_until(cond, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.05)
        return False

    link = tmp_path / "remig_src"
    link.mkdir()
    (link / "f.txt").write_text("m", encoding="utf-8")
    first = tmp_path / "remig_first"
    second = tmp_path / "remig_second"

    def _record():
        recs = call(api, "list_directory_links").get("links") or []
        return next((r for r in recs if r["link"] == str(link)), None)

    # 源目录一直都在，光看文件在不在分不出迁没迁完，要看登记换没换
    call(api, "start_directory_migration", str(link), str(first))
    assert _wait_until(lambda: (_record() or {}).get("target") == str(first)), "第一次迁移没完成"

    checked = call(api, "validate_directory_remigration", str(link), str(second))
    assert checked.get("ok") is True, checked

    call(api, "start_directory_remigration", str(link), str(second), "keep")

    # 登记换成新目标只发生在链接切换之后，等到它就是等到整件事结束
    assert _wait_until(lambda: (_record() or {}).get("target") == str(second)), "再次迁移没完成"

    rec = _record()
    assert rec, "登记里没有这条链接"
    assert rec["target"] == str(second), rec
    assert rec["old_target"].startswith(str(first)), rec
    assert rec["old_target_exists"] is True
    assert (link / "f.txt").read_text(encoding="utf-8") == "m"

    deleted = call(api, "delete_old_target", str(link), True)
    assert deleted.get("ok") is True, deleted
    assert not (tmp_path / (first.name + " 旧目标")).exists()
    after = [r for r in (call(api, "list_directory_links").get("links") or []) if r["link"] == str(link)]
    assert after and not after[0].get("old_target")


def test_delete_old_target_clears_missing_dir(api, tmp_path, monkeypatch):
    """旧目标被人手动删掉后，登记要能清掉，这条链接不能一直被它挡着。"""
    import os
    import shutil

    from core import migrate_links

    monkeypatch.setattr(
        migrate_links, "_registry_path", lambda: str(tmp_path / "links.json")
    )

    def _wait_until(cond, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.05)
        return False

    link = tmp_path / "clear_src"
    link.mkdir()
    (link / "f.txt").write_text("m", encoding="utf-8")
    first = tmp_path / "clear_first"
    second = tmp_path / "clear_second"
    third = tmp_path / "clear_third"

    def _record():
        recs = call(api, "list_directory_links").get("links") or []
        return next((r for r in recs if r["link"] == str(link)), None)

    call(api, "start_directory_migration", str(link), str(first))
    assert _wait_until(lambda: (_record() or {}).get("target") == str(first)), "第一次迁移没完成"
    call(api, "start_directory_remigration", str(link), str(second), "keep")

    assert _wait_until(lambda: (_record() or {}).get("target") == str(second)), "再次迁移没完成"
    left = (_record() or {}).get("old_target") or ""
    assert left and os.path.isdir(left), left

    # 用户自己在资源管理器里把留下的旧目标删了
    shutil.rmtree(left)

    cleared = call(api, "delete_old_target", str(link), True)
    assert cleared.get("ok") is True, cleared
    assert not (_record() or {}).get("old_target")
    # 旧目标不再挡路，还能接着往第三块盘迁
    assert call(api, "validate_directory_remigration", str(link), str(third)).get("ok") is True


def test_delete_path_and_link_records(api, tmp_path):
    """删除单个路径；链接记录类接口用空参数调用只验证不抛异常。"""
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "gone.txt").write_text("g", encoding="utf-8")

    deleted = call(api, "delete_path", str(junk), "gone.txt", True)
    assert isinstance(deleted, dict)
    assert not (junk / "gone.txt").exists()

    for name, args in (
        ("delete_directory_link", ("",)),
        ("delete_link_backup", ("", True)),
        ("restore_directory_link", ("",)),
        ("remove_directory_link_record", ("",)),
        ("cancel_directory_restore", ()),
        ("cancel_search", ()),
        ("cancel_link_find", ()),
    ):
        assert isinstance(call(api, name, *args), dict), f"{name} 没有返回字典"
