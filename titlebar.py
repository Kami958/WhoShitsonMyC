"""Windows 原生标题栏主题控制（暗色 / 浅色）。

本模块只负责「窗口壳」上的标题栏颜色：

- 接管 pywebview WinForms 后端按系统主题重刷标题栏的行为；
- 用 DWM 属性把标题栏钉到应用自己的暗 / 浅主题；
- 在 Win10 上通过延迟刷新与尺寸微扰，保证换色真正生效。

与业务逻辑（扫描、对比、快照）无关；``app.Api`` 只做委托。
非 Windows 平台全部为空操作。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Sequence
from typing import Any


class TitleBarTheme:
    """绑定到某个 pywebview 窗口的标题栏主题控制器。

    Args:
        get_window: 返回当前 ``webview.Window``（或 ``None``）的回调。
            用回调而非直接持有引用，方便在 ``set_window`` 之后才就绪。
        get_title_candidates: 取窗口标题候选列表，供 ``FindWindowW`` 兜底
            查找句柄时使用（优先精确标题，再回退到程序名）。
    """

    def __init__(
        self,
        get_window: Callable[[], Any | None],
        get_title_candidates: Callable[[], Sequence[str]] | None = None,
    ) -> None:
        self._get_window = get_window
        self._get_title_candidates = get_title_candidates or (
            lambda: ("WhoShitsOnMyC",)
        )
        # 应用自己的标题栏明暗（默认亮色）。不能跟系统主题走：
        # pywebview 会按 AppsUseLightTheme 重刷标题栏，Win10 上经常把我们刚设的盖掉。
        self.dark: bool = False
        self._hooked: bool = False
        # 上次已成功刷到标题栏的主题；相同主题的重复 set_theme 直接跳过，加快启动。
        self._applied: bool | None = None
        self._refresh_gen: int = 0

    # ---- 对外接口 ---------------------------------------------------------

    def set_theme(self, theme: str) -> dict:
        """前端切换主题时同步窗口标题栏的明暗（仅 Windows 有效）。

        启动阶段前端可能连打几次；主题没变就立刻返回，避免反复 DWM/尺寸微扰拖慢首屏。
        """
        dark = theme == "dark"
        self.hook()
        # 已成功刷成同一主题：直接返回，避免启动阶段重复 DWM/尺寸微扰。
        if self._applied is not None and self._applied == dark:
            self.dark = dark
            return {
                "ok": True,
                "theme": "dark" if dark else "light",
                "skipped": True,
            }
        self.dark = dark
        self.apply(dark, force_nudge=True)
        self.schedule_refresh(delays_ms=(80, 280))
        return {"ok": True, "theme": "dark" if dark else "light"}

    def hook(self) -> None:
        """接管 pywebview 的标题栏主题逻辑，改跟应用主题走。

        pywebview WinForms 后端在窗口创建时、以及系统「应用使用浅色」变化时，
        会按注册表 AppsUseLightTheme 重刷标题栏。这和本程序自己的暗/浅切换
        冲突。另外 Win10 上仅 SetWindowPos(FRAMECHANGED) 常常不换色，
        用户一点最大化（真的改了客户区尺寸）才刷新——所以这里还挂上 Resize。
        """
        if os.name != "nt" or self._hooked:
            return
        window = self._get_window()
        form = getattr(window, "native", None) if window else None
        if form is None:
            return
        try:
            # 用实例属性盖掉类方法；UserPreferenceChanged 仍会调到这里。
            form.update_title_bar_theme = (
                lambda *a, **k: self.apply(self.dark, force_nudge=False)
            )
            # 最大化/还原会改尺寸，借这次系统重绘把标题栏颜色钉牢。
            form.Resize += (
                lambda *a, **k: self._apply_hwnd(self.dark, force_nudge=False)
            )
            self._hooked = True
        except Exception:  # noqa: BLE001 - 钩不上就只靠主动 set_theme
            pass

    def apply(
        self, dark: bool | None = None, *, force_nudge: bool = False
    ) -> None:
        """用 DWM 把原生标题栏刷成暗/亮色。"""
        if os.name != "nt":
            return
        if dark is None:
            dark = self.dark
        else:
            self.dark = bool(dark)

        target = self.dark
        nudge = force_nudge
        self._run_on_ui(
            lambda: self._apply_hwnd(target, force_nudge=nudge)
        )

    def schedule_refresh(
        self, delays_ms: tuple[int, ...] = (100, 320)
    ) -> None:
        """启动/切主题后延迟再刷（默认 2 次，够修 Win10，又不太拖首屏）。

        每次调度递增 generation，旧 timer 回调自动作废，避免 shown/loaded/set_theme
        叠在一起把 UI 线程打满。
        """
        if os.name != "nt":
            return
        window = self._get_window()
        form = getattr(window, "native", None) if window else None
        if form is None:
            return
        self._refresh_gen += 1
        gen = self._refresh_gen
        try:
            import System.Windows.Forms as WinForms  # type: ignore

            for ms in delays_ms:
                timer = WinForms.Timer()
                timer.Interval = int(ms)

                def _tick(sender, _e, t=timer, g=gen):  # noqa: ANN001
                    try:
                        t.Stop()
                        t.Dispose()
                    except Exception:  # noqa: BLE001
                        pass
                    if g != self._refresh_gen:
                        return
                    self._apply_hwnd(self.dark, force_nudge=True)

                timer.Tick += _tick
                timer.Start()
        except Exception:  # noqa: BLE001
            def _later(delay: float, g: int = gen) -> None:
                import time

                time.sleep(delay)
                if g != self._refresh_gen:
                    return
                self._run_on_ui(
                    lambda: self._apply_hwnd(self.dark, force_nudge=True)
                )

            for ms in delays_ms:
                threading.Thread(
                    target=_later, args=(ms / 1000.0,), daemon=True
                ).start()

    def hwnd(self) -> int:
        """尽力取到顶层窗口句柄：先问 pywebview 原生对象，再按标题兜底。"""
        try:
            window = self._get_window()
            native = getattr(window, "native", None) if window else None
            handle = getattr(native, "Handle", None) if native is not None else None
            if handle is not None:
                # pywebview WinForms 用 ToInt32()；IntPtr 上也优先走它，兼容性更好。
                for meth in ("ToInt32", "ToInt64"):
                    fn = getattr(handle, meth, None)
                    if callable(fn):
                        try:
                            return int(fn())
                        except Exception:  # noqa: BLE001
                            pass
                try:
                    return int(handle)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        try:
            import ctypes

            # 标题会带管理员状态后缀，优先精确匹配当前标题，再回退到程序名。
            user32 = ctypes.windll.user32
            for title in self._get_title_candidates():
                found = user32.FindWindowW(None, title)
                if found:
                    return found
            return 0
        except Exception:  # noqa: BLE001
            return 0

    # ---- 内部实现 ---------------------------------------------------------

    def _run_on_ui(self, fn) -> None:
        """把可调用对象丢到 WinForms UI 线程执行；没有窗口则直接跑。"""
        window = self._get_window()
        form = getattr(window, "native", None) if window else None
        if form is None:
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            if getattr(form, "InvokeRequired", False):
                try:
                    from System import Action  # type: ignore

                    form.BeginInvoke(Action(fn))
                except Exception:  # noqa: BLE001
                    form.BeginInvoke(fn)
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass

    def _apply_hwnd(self, dark: bool, *, force_nudge: bool = False) -> None:
        """对当前窗口句柄写入 DWM 暗色属性并强制标题栏重绘。

        force_nudge：非最大化时对窗口宽做 +1/-1 像素抖动。Win10 上这和
        「点最大化」一样会逼 DWM 重画标题栏；Resize 回调里不要开，防抖死循环。
        """
        hwnd = self.hwnd()
        if not hwnd:
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            value = ctypes.c_int(1 if dark else 0)
            dwm = ctypes.windll.dwmapi
            DwmSetWindowAttribute = dwm.DwmSetWindowAttribute
            DwmSetWindowAttribute.argtypes = [
                wintypes.HWND,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            # 19 = 旧 DWMWA_USE_IMMERSIVE_DARK_MODE（Win10 1809–1909）
            # 20 = 现行编号（Win10 2004+ / Win11）
            for attr in (20, 19):
                DwmSetWindowAttribute(
                    wintypes.HWND(hwnd),
                    ctypes.c_uint(attr),
                    ctypes.byref(value),
                    ctypes.sizeof(value),
                )

            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOZORDER = 0x0004
            SWP_NOACTIVATE = 0x0010
            SWP_FRAMECHANGED = 0x0020
            user32.SetWindowPos(
                wintypes.HWND(hwnd),
                None,
                0,
                0,
                0,
                0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
            )

            # 通知主题/非客户区变化（部分 Win10 构建只认这个）。
            WM_THEMECHANGED = 0x031A
            WM_NCACTIVATE = 0x0086
            WM_NCPAINT = 0x0085
            user32.SendMessageW(wintypes.HWND(hwnd), WM_THEMECHANGED, 0, 0)
            # 先假失活再激活非客户区，逼标题栏重画，且不抢焦点。
            user32.SendMessageW(wintypes.HWND(hwnd), WM_NCACTIVATE, 0, 0)
            user32.SendMessageW(wintypes.HWND(hwnd), WM_NCACTIVATE, 1, 0)
            user32.SendMessageW(wintypes.HWND(hwnd), WM_NCPAINT, 1, 0)

            RDW_INVALIDATE = 0x0001
            RDW_FRAME = 0x0400
            RDW_UPDATENOW = 0x0100
            RDW_ALLCHILDREN = 0x0080
            user32.RedrawWindow(
                wintypes.HWND(hwnd),
                None,
                None,
                RDW_INVALIDATE | RDW_FRAME | RDW_UPDATENOW | RDW_ALLCHILDREN,
            )

            # 尺寸微扰：效果等同用户点一次最大化，是 Win10 上最靠谱的一招。
            if force_nudge and not user32.IsZoomed(wintypes.HWND(hwnd)):
                class RECT(ctypes.Structure):
                    _fields_ = [
                        ("left", ctypes.c_long),
                        ("top", ctypes.c_long),
                        ("right", ctypes.c_long),
                        ("bottom", ctypes.c_long),
                    ]

                rect = RECT()
                if user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
                    w = rect.right - rect.left
                    h = rect.bottom - rect.top
                    if w > 1 and h > 1:
                        flags = SWP_NOZORDER | SWP_NOACTIVATE
                        user32.SetWindowPos(
                            wintypes.HWND(hwnd),
                            None,
                            rect.left,
                            rect.top,
                            w + 1,
                            h,
                            flags,
                        )
                        user32.SetWindowPos(
                            wintypes.HWND(hwnd),
                            None,
                            rect.left,
                            rect.top,
                            w,
                            h,
                            flags | SWP_FRAMECHANGED,
                        )
                        # 微扰后再写一次属性，避免中间被系统主题盖掉。
                        for attr in (20, 19):
                            DwmSetWindowAttribute(
                                wintypes.HWND(hwnd),
                                ctypes.c_uint(attr),
                                ctypes.byref(value),
                                ctypes.sizeof(value),
                            )
                        user32.RedrawWindow(
                            wintypes.HWND(hwnd),
                            None,
                            None,
                            RDW_INVALIDATE | RDW_FRAME | RDW_UPDATENOW,
                        )
            self._applied = bool(dark)
        except Exception:  # noqa: BLE001 - 装饰性功能，失败不影响使用
            pass
