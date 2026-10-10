"""应用引导层 —— app.py 拆出的模块级启动/环境函数。

内容按职责分节：管理员检测与错误文案、窗口标题、窗口几何、WebView2
运行时检测、DevTools、日志同步、设置变更日志、单实例互斥量、临时文件
清扫。被 app/api 各 Mixin 与入口 app.py 共同引用。
"""

from __future__ import annotations

import os
import sys
import threading

from core import applog, i18n, store
from core.compress import sweep_stale_temp_dbs

# 下面这些名字的实现已拆到 paths / winmsgs / wingeometry / webview2 / logsync / singleinstance，这里再导入一遍，旧的引用路径继续可用。
from .paths import (
    _ICON_PATH,
    _RES_DIR,
    _WEB_DIR,
)

from .winmsgs import (
    _delete_error_message,
    _migrate_error_message,
)

from .wingeometry import (
    _centered_xy,
    _clamp_window_size,
    _default_window_size,
    _primary_work_area,
    _window_is_maximized,
)

from .webview2 import (
    _WEBVIEW2_BUNDLE_DIR,
    _WEBVIEW2_GUID,
    _WEBVIEW2_URL,
    _find_fixed_runtime,
    _warn_missing_webview2,
    _webview2_installed,
    _wire_bundled_webview2,
)

from .logsync import (
    _diff_common_settings,
    _settings_field_log_value,
    _sync_log_level_to_applog,
    _sync_log_sanitize_to_applog,
)

from .singleinstance import (
    _SINGLE_INSTANCE_MUTEX,
    _acquire_single_instance,
    _activate_existing_window,
    _single_instance_handle,
    _warn_already_running,
)

# 资源根目录：PyInstaller --onefile 会把打包数据解到 sys._MEIPASS；
# 未打包时就是项目根目录。web/ 与 logo.ico 都属于打包资源。

# 本次运行内是否已发现可升级的新版本；为真时窗口标题栏追加提示。
_update_available = False


def _is_admin() -> bool:
    """当前进程是否以管理员权限运行（仅 Windows 有意义；其它平台视为 True，不提示）。"""
    if os.name != "nt":
        return True
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _window_title() -> str:
    """窗口标题：程序名 + 管理员状态 + 版本号（状态文案随界面语言）。

    非管理员时在状态后附加「推荐以管理员启动」提示，方便一眼看见。
    运行中发现新版本时追加「有新版本更新」提示。
    """
    if _is_admin():
        mode = i18n.t("管理员", "Administrator")
    else:
        mode = i18n.t(
            "非管理员（推荐以管理员启动）",
            "Not Administrator (run as Administrator recommended)",
        )
    title = f"WhoShitsOnMyC — {mode}"
    if _update_available:
        title += " — " + i18n.t("有新版本更新", "New version available")
    return title


# WebView2 常青运行时（Evergreen Runtime）的固定注册表 GUID。
# 随包附带「固定版本(Fixed Version)」运行时时，约定解压到资源目录下这个文件夹。


def _detect_lang() -> str:
    """按操作系统语言判定界面语言：中文系统返回 ``"zh"``，其余一律 ``"en"``。

    Windows 读用户界面语言（``GetUserDefaultUILanguage``，主语言号 0x04 为中文）；
    其它平台退回 ``locale``。任何异常都保守回落英文。
    """
    if os.name == "nt":
        try:
            import ctypes

            langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "zh" if (langid & 0x3FF) == 0x04 else "en"
        except Exception:  # noqa: BLE001
            return "en"
    try:
        import locale

        loc = (locale.getdefaultlocale()[0] or "").lower()
        return "zh" if loc.startswith("zh") else "en"
    except Exception:  # noqa: BLE001
        return "en"


def _wants_devtools(argv: list[str] | None = None) -> bool:
    """是否打开前端 DevTools（F12 / 右键检查）。

    任一条满足即开启：
    - 命令行 ``--devtools`` / ``--debug``
    - 环境变量 ``WSMC_DEVTOOLS=1``（或 true/yes/on）
    """
    args = list(argv if argv is not None else sys.argv[1:])
    if "--devtools" in args or "--debug" in args:
        return True
    flag = (
        os.environ.get("WSMC_DEVTOOLS") or os.environ.get("WSMC_DEBUG") or ""
    ).strip().lower()
    return flag in ("1", "true", "yes", "on")


def _prepare_devtools_without_http_spam() -> None:
    """开 DevTools 时避免 pywebview/Bottle 把本地静态资源访问打到 Python 控制台。

    pywebview 在 ``debug=True`` 时会：
    - 启用 WebView2 DevTools（需要）
    - 把 Bottle 的 ``quiet`` 关掉 → 刷 ``GET /js/...`` 访问日志（不需要）
    - 把 pywebview logger 提到 DEBUG（不需要）

    前端业务日志应只出现在浏览器控制台；此处只静音本地 HTTP 服务噪音。
    """
    # 阻止 start(debug=True) 把 pywebview logger 提到 DEBUG
    os.environ.setdefault("PYWEBVIEW_LOG", "WARNING")

    try:
        from wsgiref.simple_server import WSGIRequestHandler

        def _silent_log_message(self, format, *args):  # noqa: A002, ANN001, ARG001
            return None

        WSGIRequestHandler.log_message = _silent_log_message  # type: ignore[method-assign]
    except Exception:  # noqa: BLE001
        pass

    try:
        import bottle

        _orig_run = bottle.run

        def _run_quiet(*args, **kwargs):  # noqa: ANN002, ANN003
            # 强制 quiet：不打印 Bottle 启动横幅与访问日志
            kwargs["quiet"] = True
            return _orig_run(*args, **kwargs)

        bottle.run = _run_quiet  # type: ignore[assignment]
    except Exception:  # noqa: BLE001
        pass


# ---- 单实例（Windows 命名互斥量） ---------------------------------------

# 互斥量句柄持有到进程退出，由 OS 回收，无需显式释放。


def _start_temp_sweep() -> None:
    """后台清扫上次异常退出遗留的临时解压快照（``wsmc_*.db``）。"""

    def _run() -> None:
        try:
            removed = sweep_stale_temp_dbs()
            if removed:
                applog.info(f"startup temp sweep removed {removed} stale db(s)")
        except Exception as exc:  # noqa: BLE001 - 清扫失败不影响启动
            applog.warn(f"startup temp sweep failed: {exc}")

    threading.Thread(target=_run, daemon=True, name="temp-sweep").start()
