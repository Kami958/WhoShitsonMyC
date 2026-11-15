"""pywebview 桥接层入口 —— 创建窗口并启动应用。

JS-API 实现在 ``app/`` 包（``from app import Api``）；引导/环境函数在
``app/bootstrap.py``。启动应用直接运行本文件::

    python app.py
"""

from __future__ import annotations

import os
import threading
import time

import webview  # pywebview

from core import applog, i18n, packenv, store, wingeom
from version import __version__ as APP_VERSION

from app import bootstrap
from app.api import Api
from app.webview2 import clear_webview_storage, sweep_stale_webview_storage
from app.bootstrap import (
    _ICON_PATH,
    _WEB_DIR,
    _acquire_single_instance,
    _activate_existing_window,
    _centered_xy,
    _clamp_window_size,
    _default_window_size,
    _detect_lang,
    _pick_ui_port,
    _prepare_devtools_without_http_spam,
    _start_temp_sweep,
    _start_webview,
    _sync_log_level_to_applog,
    _sync_log_sanitize_to_applog,
    _warn_already_running,
    _warn_missing_webview2,
    _watch_ui_server,
    _webview2_installed,
    _webview_storage_path,
    _window_title,
    _wire_bundled_webview2,
    _wants_devtools,
)


def main() -> None:
    """创建窗口并启动应用。"""
    applog.note_startup(APP_VERSION)
    # 单实例检测须在临时文件清扫之前：第二个实例直接退出，
    # 不去碰第一个实例正在使用的解压临时文件。
    if not _acquire_single_instance():
        applog.warn("another instance is already running; exiting")
        i18n.set_lang(_detect_lang())
        # 已有实例：先告知本次启动没有继续，再尝试把旧窗口唤回。
        # 找到窗口就静默退出会让用管理员方式启动的用户以为旧窗口已经是管理员权限。
        _warn_already_running()
        _activate_existing_window()
        return
    # 先确定当前运行账户的数据目录并验证可以访问，再读取设置。
    # 验证失败时不创建目录、不写设置，窗口照常建立，由启动恢复界面说明
    # 路径、原因和下一步；扫描与快照在存储恢复前保持停用。
    storage = store.initialize_storage()
    # 记下打包展开目录能否读取、所在磁盘还剩多少：日后出现「读不到运行
    # 环境」的反馈时，日志里直接能分清是目录被删还是磁盘紧张导致展开不全。
    applog.info(packenv.diagnostics_text())
    # 浏览器缓存放系统临时文件夹，先清掉上次异常退出留下的副本。
    try:
        sweep_stale_webview_storage()
    except Exception as exc:  # noqa: BLE001 - 清扫失败不影响启动
        applog.warn(f"startup webview sweep failed: {exc}")
    _sync_log_sanitize_to_applog()
    _sync_log_level_to_applog()
    if not storage.get("ok"):
        applog.warn(
            "app data folder unavailable at startup"
            f" | reason={storage.get('reason') or 'unknown'}"
        )
    # 兜底清掉上次异常退出（被强杀 / 崩溃）遗留的解压临时文件；
    # 正常退出由 _shutdown() 自删；设置页「临时文件清理」可整体关闭。
    if store.get_temp_cleanup():
        _start_temp_sweep()
    # 语言在 common.lang（旧扁平顶层 lang 亦经 _common_view 合并），
    # 不能写 ``"lang" in _disk``——分节 YAML 顶层只有 common/ai，会误判成「无语言」
    # 从而用系统 UI 语言覆盖配置文件。
    # 数据文件夹还不可用时读不到设置文件，按空配置继续启动。
    try:
        _disk = store.load_settings_document()
    except OSError as exc:
        applog.exception("read settings at startup failed", exc)
        _disk = {}
    _common = store._common_view(_disk) if isinstance(_disk, dict) else {}  # noqa: SLF001
    if "lang" in _common:
        # initialize_storage 已把设置读入 store；此处只同步运行时 i18n
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
    # v 参数只跟着版本号走，同版本内改了页面不会被重新取；
    # 因此在地址上再加一个启动时间，保证每次启动都重新读入口页面。
    # 页内脚本地址上的 ?v= 仍是写死的，改脚本时要把 web/index.html 里的
    # 那批版本号一并升上去，否则脚本本身仍会命中旧缓存。
    _boot_url = os.path.join(
        _WEB_DIR, "index.html"
    ) + f"?theme={_boot_theme}&v={APP_VERSION}&t={int(time.time())}"
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
    _fallback: list[threading.Timer] = []

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
            for timer in _fallback:
                try:
                    timer.cancel()
                except Exception:  # noqa: BLE001
                    pass
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
    except Exception as exc:  # noqa: BLE001
        applog.warn(
            f"window loaded event subscribe failed | reason={type(exc).__name__}"
        )

    def _fallback_show() -> None:
        """加载事件没送达时只负责把窗口显示出来，不重复做外壳准备。"""
        if _ui_shown["done"]:
            return
        applog.warn("window loaded event missing; showing window as fallback")
        _ui_shown["done"] = True
        for timer in _fallback:
            try:
                timer.cancel()
            except Exception:  # noqa: BLE001
                pass
        try:
            _prepare_chrome()
        except Exception as exc:  # noqa: BLE001
            applog.warn(
                f"window fallback chrome failed | reason={type(exc).__name__}"
            )
        try:
            window.show()
        except Exception as exc:  # noqa: BLE001
            applog.exception("window fallback show failed", exc)
        try:
            api.start_auto_update_check()
        except Exception:  # noqa: BLE001
            pass

    # 加载事件没送达时兜底显示，否则进程会一直藏着窗口并占着单实例锁
    try:
        fallback = threading.Timer(8.0, _fallback_show)
        fallback.daemon = True
        _fallback.append(fallback)
        fallback.start()
    except Exception as exc:  # noqa: BLE001
        applog.warn(
            f"window fallback show schedule failed | reason={type(exc).__name__}"
        )
    start_kwargs: dict = {
        # 隐私模式 + 固定数据目录：浏览器数据照样写进指定目录，退出时由 pywebview
        # 自己清除；数据文件夹只放设置、快照、链接登记与迁移历史。
        "private_mode": True,
    }
    # 界面服务端口由本程序先试绑一次再交给依赖库。依赖库绑定失败只让后台线程
    # 退出，窗口照样建出来并导航过去，用户只能看到浏览器错误页，因此既要在启动
    # 前挑一个确认可用的端口，也要在启动后确认服务真的在接受连接。
    ui_port = _pick_ui_port()
    if ui_port:
        start_kwargs["http_port"] = ui_port
    else:
        applog.warn("no free port for ui server; letting the library pick one")
    storage_path = _webview_storage_path()
    if storage_path:
        start_kwargs["storage_path"] = storage_path
    else:
        applog.warn("webview storage path unavailable; using pywebview default")
    if os.path.exists(_ICON_PATH):
        start_kwargs["icon"] = _ICON_PATH  # 任务栏/GUI 图标
    # debug=True：WebView2 可 F12 / 右键检查；命令行与设置页任一开启即生效
    want_devtools = _wants_devtools()
    if not want_devtools:
        try:
            want_devtools = store.get_dev_tools()
        except Exception as exc:  # noqa: BLE001
            applog.warn(f"read dev_tools setting failed: {exc}")
    if want_devtools:
        _prepare_devtools_without_http_spam()
        start_kwargs["debug"] = True
        applog.info("DevTools enabled")
    ui_server_stop = threading.Event()
    if ui_port:
        threading.Thread(
            target=_watch_ui_server,
            args=(ui_port, ui_server_stop),
            daemon=True,
        ).start()
    try:
        _start_webview(webview, start_kwargs)
    finally:
        ui_server_stop.set()
        # 消息循环退出（窗口关闭）后先关 Diff、再删临时解压文件，
        # 必须赶在 atexit 之前：那时 sqlite 句柄已释放，删除才会成功。
        api._shutdown()
        # 浏览器缓存放在系统临时文件夹，退出时删掉；删不掉留给下次启动清扫。
        # 重启时跳过：新进程可能已经建好并用着同一个目录，删除会把它踹掉，
        # 残留交给新进程启动时的清扫处理。
        if not getattr(api, "_restarting", False):
            clear_webview_storage()


if __name__ == "__main__":
    # Windows + PyInstaller：多进程 spawn 子进程会重新执行本模块入口，
    # freeze_support 让子进程走 worker 路径而非再起 GUI。
    import multiprocessing

    multiprocessing.freeze_support()
    main()
