"""单实例互斥：抢锁、唤回已有窗口、已在运行的提示。"""

from __future__ import annotations

from core import applog, i18n
import os



_SINGLE_INSTANCE_MUTEX = "Local\\WhoShitsOnMyC_SingleInstance"


def _acquire_single_instance() -> bool:
    """登记本进程为唯一实例；已有实例在运行返回 False。

    非 Windows 平台不做检测恒返回 True；互斥量申请失败也放行，
    不让检测本身拦住启动。
    """
    global _single_instance_handle
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p, wintypes.BOOL, ctypes.c_wchar_p
        ]
        handle = kernel32.CreateMutexW(None, True, _SINGLE_INSTANCE_MUTEX)
        if not handle:
            return True
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return False
        _single_instance_handle = handle
        return True
    except Exception:  # noqa: BLE001
        return True


def _activate_existing_window() -> bool:
    """把已运行实例的主窗口还原并带到前台；找不到返回 False。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def on_window(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0 and user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                if title == "WhoShitsOnMyC" or title.startswith("WhoShitsOnMyC "):
                    found.append(hwnd)
                    return False
            return True

        if not user32.EnumWindows(on_window, 0) or not found:
            return False
        hwnd = found[0]
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:  # noqa: BLE001 - 激活失败退回弹提示，不拦退出
        return False


_single_instance_handle = None


def _warn_already_running() -> None:
    """已有实例但找不到其窗口（可能仍在启动中）时弹原生提示。"""
    msg = i18n.t(
        "WhoShitsOnMyC 已经在运行，请查看任务栏中的已有窗口。",
        "WhoShitsOnMyC is already running; see the existing window in the taskbar.",
    )
    title = "WhoShitsOnMyC"
    try:
        import ctypes

        # MB_OK | MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40)
    except Exception:  # noqa: BLE001
        applog.warn(f"already running (MessageBox failed): {msg}")
