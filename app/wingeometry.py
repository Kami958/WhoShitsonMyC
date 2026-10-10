"""窗口几何：默认尺寸、居中坐标、最大化判断。"""

from __future__ import annotations

import os


def _centered_xy(width: int, height: int) -> tuple[int | None, int | None]:
    """算出让窗口落在主屏正中的左上角坐标；非 Windows 返回 (None, None)。"""
    if os.name != "nt":
        return None, None
    try:
        import ctypes

        user32 = ctypes.windll.user32
        sw = user32.GetSystemMetrics(0)  # 主屏宽
        sh = user32.GetSystemMetrics(1)  # 主屏高
        return max(0, (sw - width) // 2), max(0, (sh - height) // 2)
    except Exception:  # noqa: BLE001
        return None, None


def _clamp_window_size(w: int, h: int) -> tuple[int, int]:
    """把恢复的窗口尺寸压到主屏工作区内（下限 min_size）。

    防止历史保存的异常宽高——例如旧版工具侧栏把侧栏宽度记进窗口宽度——
    在下一次启动时把窗口撑得超出屏幕。
    """
    try:
        w = int(w)
        h = int(h)
    except (TypeError, ValueError):
        return _default_window_size()
    work = _primary_work_area()
    if work is None:
        return w, h
    _x, _y, sw, sh = work
    return max(820, min(w, max(820, sw))), max(560, min(h, max(560, sh)))


def _default_window_size() -> tuple[int, int]:
    """未记住尺寸时的默认窗口：高度取主屏工作区约一半，宽度按 1100:720 比例。"""
    work = _primary_work_area()
    if work is not None:
        _x, _y, sw, sh = work
    elif os.name == "nt":
        try:
            import ctypes

            sw = int(ctypes.windll.user32.GetSystemMetrics(0))
            sh = int(ctypes.windll.user32.GetSystemMetrics(1))
        except Exception:  # noqa: BLE001
            sw, sh = 1280, 800
    else:
        sw, sh = 1280, 800

    # 高度 = 工作区高度的 50%，并夹在 min_size 与屏幕之间
    h = int(round(sh * 0.5))
    h = max(560, min(h, max(560, sh - 40)))
    # 宽随高保持约 11:7.2，且不超过工作区
    w = int(round(h * 1100 / 720))
    w = max(820, min(w, max(820, sw - 40)))
    return w, h


def _primary_work_area() -> tuple[int, int, int, int] | None:
    """主屏工作区 (x, y, w, h)；失败返回 None。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        # SPI_GETWORKAREA = 0x0030
        rect = RECT()
        if not ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return None
        return (
            int(rect.left),
            int(rect.top),
            int(rect.right - rect.left),
            int(rect.bottom - rect.top),
        )
    except Exception:  # noqa: BLE001
        return None


def _window_is_maximized(win: "webview.Window") -> bool:
    """当前窗口是否最大化（Windows / pywebview winforms）。"""
    if win is None:
        return False
    try:
        from webview.platforms import winforms as _wf  # type: ignore

        instances = getattr(getattr(_wf, "BrowserView", None), "instances", None) or {}
        form = instances.get(getattr(win, "uid", None))
        if form is None:
            return False
        state = getattr(form, "WindowState", None)
        if state is None:
            return False
        # System.Windows.Forms.FormWindowState.Maximized
        return str(state).endswith("Maximized") or int(state) == 2
    except Exception:  # noqa: BLE001
        return False
