"""入口冒烟：跑一遍 app.py 的启动序列，但不真的开窗口。

用户能看到的崩溃大多发生在从 main() 到窗口出现这段路径上，而单元测试从不执行
这条路径，所以「测试全绿但一启动就崩」是会发生的：``_start_temp_sweep`` 的定义
搬到 ``app/bootstrap.py`` 之后，启动代码漏了导入它，一跑就 NameError。

这里把窗口层、单实例锁、WebView2 探测、临时文件清扫换成假对象，让启动序列按真实
代码走一遍：日志同步、设置载入、窗口尺寸计算、Api 构造、事件挂载、收尾清理。
被替换的都是跟系统打交道的部件，真开窗会弹 GUI，真抢锁会占住单实例锁，真清扫会
删别的实例的临时文件，都不适合在测试里跑。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class _FakeHandler:
    """事件处理器占位：只支持 ``events.名字 += 回调``。"""

    def __iadd__(self, callback: object) -> "_FakeHandler":
        return self


class _FakeEvents:
    """pywebview 的窗口事件集合，任何事件名都给一个占位处理器。"""

    def __getattr__(self, name: str) -> _FakeHandler:
        handler = _FakeHandler()
        setattr(self, name, handler)
        return handler


class _FakeWindow:
    """假窗口：记录创建参数，事件与常用方法都不真的做事。"""

    def __init__(self, kwargs: dict) -> None:
        self.kwargs = kwargs
        self.events = _FakeEvents()
        self.native = None
        self.title = kwargs.get("title", "")

    def evaluate_js(self, script: str) -> None:
        pass

    def show(self) -> None:
        pass

    def hide(self) -> None:
        pass

    def destroy(self) -> None:
        pass


class _FakeWebview:
    """替掉 pywebview 模块：create_window 只记录，start 立即返回。"""

    def __init__(self) -> None:
        self.created: list[_FakeWindow] = []
        self.started: list[dict] = []

    def create_window(self, **kwargs) -> _FakeWindow:
        window = _FakeWindow(kwargs)
        self.created.append(window)
        return window

    def start(self, **kwargs) -> None:
        self.started.append(kwargs)


def _load_entry() -> types.ModuleType:
    """按文件路径执行 app.py。

    模块的 ``__name__`` 不是 ``__main__``，所以文件末尾的启动块不会触发，
    可以只调用 main() 而不进入真实的消息循环。
    """
    spec = importlib.util.spec_from_file_location("wsmc_entry_under_test", REPO_ROOT / "app.py")
    assert spec is not None and spec.loader is not None
    module = types.ModuleType(spec.name)
    module.__file__ = str(REPO_ROOT / "app.py")
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_startup_reaches_window_creation(monkeypatch):
    """启动序列跑到窗口创建，并且没有中途抛异常。"""
    from app import bootstrap
    from core import store

    entry = _load_entry()
    fake_webview = _FakeWebview()
    swept: list[int] = []

    monkeypatch.setattr(entry, "webview", fake_webview)
    # 随包运行时探测与系统注册表打交道，跟启动接线无关，直接放行。
    monkeypatch.setattr(entry, "_wire_bundled_webview2", lambda: True)
    # 真抢单实例锁会占住锁，且本进程已经没有窗口可激活。
    monkeypatch.setattr(entry, "_acquire_single_instance", lambda: True)
    monkeypatch.setattr(entry, "_activate_existing_window", lambda: True)
    monkeypatch.setattr(store, "get_temp_cleanup", lambda: True)

    # 清扫起后台线程去删临时目录里的 wsmc_*.db，测试里只记录是否被调用。
    # 这一行本身就是断言：入口模块必须自己持有这个名字，缺导入会在这里抛
    # AttributeError，正是曾经一启动就崩的那个漏法。
    assert entry._start_temp_sweep is bootstrap._start_temp_sweep  # noqa: SLF001
    monkeypatch.setattr(entry, "_start_temp_sweep", lambda: swept.append(1))

    entry.main()

    assert len(fake_webview.created) == 1, "启动序列没有创建窗口"
    assert len(fake_webview.started) == 1, "启动序列没有进入消息循环"
    assert swept == [1], "临时文件清扫没有被调用"
    window = fake_webview.created[0]
    assert window.kwargs["js_api"] is not None
    assert window.kwargs["min_size"] == (820, 560)


def test_single_instance_exit_path(monkeypatch):
    """已有实例在跑时走退出分支，不创建窗口。"""
    from core import store

    entry = _load_entry()
    fake_webview = _FakeWebview()
    activated: list[int] = []

    monkeypatch.setattr(entry, "webview", fake_webview)
    monkeypatch.setattr(entry, "_wire_bundled_webview2", lambda: True)
    monkeypatch.setattr(entry, "_acquire_single_instance", lambda: False)
    monkeypatch.setattr(entry, "_activate_existing_window", lambda: activated.append(1) or True)
    monkeypatch.setattr(entry, "_warn_already_running", lambda: None)
    monkeypatch.setattr(store, "get_temp_cleanup", lambda: True)

    entry.main()

    assert fake_webview.created == []
    assert activated == [1]
