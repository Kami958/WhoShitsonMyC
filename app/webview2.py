"""WebView2 运行时：检测系统安装、挂载内置固定版本、缺失提示。"""

from __future__ import annotations

from core import applog, i18n
import os
from .paths import _RES_DIR



_WEBVIEW2_BUNDLE_DIR = "webview2_runtime"


_WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


_WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"


def _find_fixed_runtime() -> str | None:
    """找出随包附带的 WebView2 固定版本运行时目录（含 msedgewebview2.exe）。

    约定把固定版本运行时放在资源目录下的 ``webview2_runtime/``。既支持直接
    把内容铺在该目录，也支持里面套一层微软默认的版本号子目录（自动下探一层）。
    没有则返回 None。
    """
    if os.name != "nt":
        return None
    base = os.path.join(_RES_DIR, _WEBVIEW2_BUNDLE_DIR)
    if not os.path.isdir(base):
        return None
    if os.path.isfile(os.path.join(base, "msedgewebview2.exe")):
        return base
    try:
        for name in os.listdir(base):
            sub = os.path.join(base, name)
            if os.path.isfile(os.path.join(sub, "msedgewebview2.exe")):
                return sub
    except OSError:
        pass
    return None


def _warn_missing_webview2() -> None:
    """弹原生对话框告知缺少 WebView2，并打开下载页。"""
    msg = i18n.t(
        (
            "本程序的界面依赖 Microsoft Edge WebView2 运行时，"
            "但当前系统里没有检测到它。\n\n"
            "点击「确定」将打开官方下载页，请下载并安装"
            "『常青版 Evergreen 引导安装程序』，装好后重新启动本程序。\n\n"
            "（Windows 11 通常自带；部分 Windows 10 需手动安装，一次即可。）"
        ),
        (
            "This app's interface needs the Microsoft Edge WebView2 runtime, "
            "which was not detected on this system.\n\n"
            "Click OK to open the official download page, then download and install "
            "the \"Evergreen Bootstrapper\" and restart this app.\n\n"
            "(Windows 11 usually includes it; some Windows 10 systems need a "
            "one-time manual install.)"
        ),
    )
    title = i18n.t("缺少 WebView2 运行时", "WebView2 runtime missing")
    try:
        import ctypes

        # MB_OK | MB_ICONWARNING
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x30)
    except Exception:  # noqa: BLE001
        applog.warn(f"WebView2 missing (MessageBox failed): {msg}")
    try:
        os.startfile(_WEBVIEW2_URL)  # noqa: S606 - 打开官方下载页
    except OSError:
        pass


def _webview2_installed() -> bool:
    """检测系统是否装了 WebView2 运行时（界面依赖它渲染）。

    Windows 11 通常自带；随新版 Edge 也会一并装上，所以多数 Win10 也有。
    但 LTSC、N 版、纯净镜像等可能缺失——缺了 pywebview 起不来。
    非 Windows 一律返回 True（不适用）。
    """
    if os.name != "nt":
        return True
    import winreg

    key = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\%s" % _WEBVIEW2_GUID
    wow = r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\%s" % _WEBVIEW2_GUID
    for root, sub in (
        (winreg.HKEY_LOCAL_MACHINE, wow),   # 64 位系统上运行时装在 WOW6432Node 下
        (winreg.HKEY_LOCAL_MACHINE, key),
        (winreg.HKEY_CURRENT_USER, key),
    ):
        try:
            with winreg.OpenKey(root, sub) as k:
                pv, _ = winreg.QueryValueEx(k, "pv")
                if pv and pv not in ("", "0.0.0.0"):
                    return True
        except OSError:
            continue
    return False


def _wire_bundled_webview2() -> bool:
    """若附带了固定版本运行时，指引 WebView2 加载器优先用它（离线自足）。

    Returns:
        True 表示已挂上内置运行时，可跳过系统运行时检测。
    """
    folder = _find_fixed_runtime()
    if folder:
        # WebView2Loader 会优先读这个环境变量定位运行时，不再依赖系统安装。
        os.environ.setdefault("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", folder)
        return True
    return False
