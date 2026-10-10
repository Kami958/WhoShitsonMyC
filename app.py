"""pywebview 桥接层入口 —— 创建窗口并启动应用。

JS-API 实现在 ``app/`` 包（``from app import Api``）；引导/环境函数在
``app/bootstrap.py``。启动应用直接运行本文件::

    python app.py
"""

from __future__ import annotations

import os

import webview  # pywebview

from core import applog, i18n, store, wingeom
from version import __version__ as APP_VERSION

from app import bootstrap
from app.api import Api
from app.bootstrap import (
    _ICON_PATH,
    _WEB_DIR,
    _acquire_single_instance,
    _activate_existing_window,
    _centered_xy,
    _clamp_window_size,
    _default_window_size,
    _detect_lang,
    _prepare_devtools_without_http_spam,
    _start_temp_sweep,
    _sync_log_level_to_applog,
    _sync_log_sanitize_to_applog,
    _warn_already_running,
    _warn_missing_webview2,
    _webview2_installed,
    _window_title,
    _wire_bundled_webview2,
    _wants_devtools,
)


def main() -> None:
    """创建窗口并启动应用。"""
    # 语言 / 主题：settings.yaml 显式写入则用它；语言缺省用系统语言。
    # 标题栏默认跟 store 主题，避免先按 dark 刷再被前端纠正。
    # 脱敏 / 日志等级须在 note_startup 之前生效，启动日志才反映真实状态。
    _sync_log_sanitize_to_applog()
    _sync_log_level_to_applog()
    applog.note_startup(APP_VERSION)
    # 单实例检测须在临时文件清扫之前：第二个实例直接退出，
    # 不去碰第一个实例正在使用的解压临时文件。
    if not _acquire_single_instance():
        applog.warn("another instance is already running; exiting")
        i18n.set_lang(_detect_lang())
        if not _activate_existing_window():
            _warn_already_running()
        return
    # 兜底清掉上次异常退出（被强杀 / 崩溃）遗留的解压临时文件；
    # 正常退出由 _shutdown() 自删；设置页「临时文件清理」可整体关闭。
    if store.get_temp_cleanup():
        _start_temp_sweep()
    # 语言在 common.lang（旧扁平顶层 lang 亦经 _common_view 合并），
    # 不能写 ``"lang" in _disk``——分节 YAML 顶层只有 common/ai，会误判成「无语言」
    # 从而用系统 UI 语言覆盖配置文件。
    _disk = store._load_settings_yaml(store.settings_path())  # noqa: SLF001
    _common = store._common_view(_disk) if isinstance(_disk, dict) else {}  # noqa: SLF001
    if "lang" in _common:
        # import store 时已 apply；此处只同步运行时 i18n
        i18n.set_lang(store.get_lang())
    else:
        detected = _detect_lang()
        store._lang = detected  # noqa: SLF001 - 启动初始化，不触发 persist
        i18n.set_lang(detected)
    # 优先用随包附带的固定版本运行时；没有再检测系统运行时，仍缺则引导安装。
    if not _wire_bundled_webview2() and not _webview2_installed():
        _warn_missing_webview2()
        return

    api = Api()
    # 开启「记住」且 YAML/内存里已有有效尺寸 → 用记住的；否则用屏幕推导默认。
    # 记住的值经 _clamp_window_size 压到工作区，防历史异常宽高把窗口撑出屏幕。
    # window_width 存的是「侧栏展开时」的宽度，收起态要减掉侧栏宽；
    # 老配置（YAML 没写过 ui_tool_panel_open）的 window_width 还是旧语义，
    # 首次启动不减，等前端 sync_tool_panel_state 迁移后再启用新语义。
    if store.get_remember_window_size() and store.has_saved_window_size():
        width, height = _clamp_window_size(*store.get_window_size())
        if store.has_explicit_tool_panel_open():
            width = _clamp_window_size(
                wingeom.to_launch_width(
                    width,
                    panel_open=store.get_tool_panel_open(),
                    panel_width=store.get_tool_panel_width(),
                ),
                height,
            )[0]
    else:
        width, height = _default_window_size()
    api._last_win_w, api._last_win_h = width, height  # noqa: SLF001
    # 启动宽度若已按展开态减过侧栏宽度，登记加宽量，运行期收起时才能收回
    api._panel_win_extra = (  # noqa: SLF001
        store.get_tool_panel_width()
        if (
            store.get_remember_window_size()
            and store.has_saved_window_size()
            and store.has_explicit_tool_panel_open()
            and store.get_tool_panel_open()
        )
        else 0
    )
    x, y = _centered_xy(width, height)
    # 窗口底色与页面主题都以 settings.yaml 为准，避免默认 light 先白后黑。
    # 主题经 URL 参数交给 index.html 首帧脚本，不写生成文件。
    # auto 在此解析一次系统深浅色（仅启动时判断，运行中不监听变化）。
    # v 参数用于破坏 WebView2 静态资源缓存：发版改版本号后强制重载 index.html。
    _boot_theme = store.resolve_theme(store.get_theme())
    _boot_bg = "#ffffff" if _boot_theme == "light" else "#14161a"
    _boot_url = os.path.join(
        _WEB_DIR, "index.html"
    ) + f"?theme={_boot_theme}&v={APP_VERSION}"
    window = webview.create_window(
        title=_window_title(),
        url=_boot_url,
        js_api=api,
        width=width,
        height=height,
        x=x,
        y=y,
        min_size=(820, 560),
        background_color=_boot_bg,
        # loaded 后再 show，导航过程不可见
        hidden=True,
        # 关掉 ExtendFrameIntoClientArea，避免 DWM 客户区首帧发白
        shadow=False,
    )
    api.set_window(window)
    _ui_shown = {"done": False}

    def _on_resized(w, h) -> None:
        try:
            iw, ih = int(w), int(h)
        except (TypeError, ValueError):
            iw, ih = 0, 0
        if iw and ih:
            api._last_win_w, api._last_win_h = iw, ih  # noqa: SLF001
        try:
            api._schedule_window_size_save(w, h)
        except Exception:  # noqa: BLE001
            pass

    try:
        window.events.resized += _on_resized
    except Exception:  # noqa: BLE001
        pass

    # 标题栏主题先跟 store，避免窗口显示后先浅色标题栏再跳暗色
    try:
        api._titlebar.dark = _boot_theme != "light"
    except Exception:  # noqa: BLE001
        pass

    def _prepare_chrome() -> None:
        api._refresh_window_title()
        api._titlebar.hook()
        api._apply_icon(_ICON_PATH)
        # 首屏不要 force_nudge：尺寸 +1 微扰本身会造成可见闪动
        api._titlebar.apply(api._titlebar.dark, force_nudge=False)

    def _on_shown() -> None:
        # hidden 窗口内部也会走过 show/hide；这里只做壳准备，不主动显示
        _prepare_chrome()

    try:
        window.events.shown += _on_shown
    except Exception:  # noqa: BLE001
        pass
    try:
        window.events.restored += lambda: api._titlebar.apply(
            api._titlebar.dark, force_nudge=False
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        def _on_loaded() -> None:
            if _ui_shown["done"]:
                return
            _ui_shown["done"] = True
            _prepare_chrome()
            try:
                window.show()
            except Exception as exc:  # noqa: BLE001
                applog.exception("window.show after loaded failed", exc)
            # 显示后再轻量补一次标题栏，仍不做首帧尺寸微扰
            api._titlebar.schedule_refresh(delays_ms=(200,), force_nudge=False)
            # 启动后台检查更新（设置开启时），有新版标题栏提示
            api.start_auto_update_check()

        window.events.loaded += _on_loaded
    except Exception:  # noqa: BLE001
        pass
    start_kwargs = {}
    if os.path.exists(_ICON_PATH):
        start_kwargs["icon"] = _ICON_PATH  # 任务栏/GUI 图标
    # debug=True：WebView2 可 F12 / 右键检查；仅开发排查时开
    if _wants_devtools():
        _prepare_devtools_without_http_spam()
        start_kwargs["debug"] = True
        applog.info("DevTools enabled (--devtools / WSMC_DEVTOOLS)")
    try:
        try:
            webview.start(**start_kwargs)
        except TypeError:
            # 个别后端不接受 icon/debug 组合，逐步降级
            start_kwargs.pop("icon", None)
            try:
                webview.start(**start_kwargs)
            except TypeError:
                webview.start()
    finally:
        # 消息循环退出（窗口关闭）后先关 Diff、再删临时解压文件，
        # 必须赶在 atexit 之前：那时 sqlite 句柄已释放，删除才会成功。
        api._shutdown()


if __name__ == "__main__":
    # Windows + PyInstaller：多进程 spawn 子进程会重新执行本模块入口，
    # freeze_support 让子进程走 worker 路径而非再起 GUI。
    import multiprocessing

    multiprocessing.freeze_support()
    main()
