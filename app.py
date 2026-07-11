"""pywebview 桥接层 —— 连接 HTML 前端与纯 Python 核心引擎。

前端（``web/``）通过 pywebview 的 JS-API 调用这里 :class:`Api` 上的方法：
扫描、列举快照、对比、下钻、删除等。扫描在**后台线程**运行，
进度经 ``window.evaluate_js`` 主动推给前端，界面全程不卡。

启动应用直接运行本文件::

    python app.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import traceback

import webview  # pywebview

from core.compress import CompressError, compress_db, ensure_db_path
from core.differ import Diff, DiffError
from core.scanner import ScanCancelled, scan_to_snapshot
from core.snapshot import SnapshotError
from core import i18n, store
from version import __version__ as APP_VERSION

# 资源根目录：PyInstaller --onefile 会把打包数据解到 sys._MEIPASS；
# 未打包时就是本文件所在目录。web/ 与 logo.ico 都属于打包资源。
_RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
_WEB_DIR = os.path.join(_RES_DIR, "web")
_ICON_PATH = os.path.join(_RES_DIR, "logo.ico")


class Api:
    """暴露给前端 JS 的接口。所有方法的返回值都会被 pywebview 序列化为 JSON。

    约定：正常返回业务数据；可预期的错误以 ``{"error": "...消息..."}`` 返回，
    让前端弹友好提示而非崩溃。
    """

    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._scan_thread: threading.Thread | None = None
        self._cancel = threading.Event()
        # 当前对比会话，按需在多次下钻之间复用（避免每次重开连接）。
        # pywebview 的每次 JS-API 调用可能在不同线程执行，
        # SQLite 连接跨线程复用必须串行化，故加锁。
        self._diff: Diff | None = None
        self._diff_key: tuple[str, str] | None = None
        self._diff_lock = threading.Lock()
        # 应用自己的标题栏明暗（默认暗色）。不能跟系统主题走：
        # pywebview 会按 AppsUseLightTheme 重刷标题栏，Win10 上经常把我们刚设的盖掉。
        self._titlebar_dark: bool = True
        self._titlebar_hooked: bool = False
        # 上次已成功刷到标题栏的主题；相同主题的重复 set_theme 直接跳过，加快启动。
        self._titlebar_applied: bool | None = None
        self._titlebar_refresh_gen: int = 0

    # ---- 生命周期 -------------------------------------------------------

    def set_window(self, window: "webview.Window") -> None:
        """由启动代码注入窗口引用，用于向前端推事件、开原生对话框。"""
        self._window = window

    def _emit(self, event: str, payload: dict) -> None:
        """向前端推送一个事件（调用前端注册的 ``window.__onPyEvent``）。"""
        if self._window is None:
            return
        data = json.dumps(payload, ensure_ascii=False)
        # 事件名与数据都经 JSON 编码，避免注入与转义问题。
        self._window.evaluate_js(
            f"window.__onPyEvent && window.__onPyEvent({json.dumps(event)}, {data})"
        )

    # ---- 快照列举 / 选择目录 -------------------------------------------

    def list_snapshots(self) -> list[dict]:
        """返回默认目录下所有快照的摘要（新→旧）。"""
        return [i.to_dict() for i in store.list_snapshots()]

    def choose_folder(self) -> dict:
        """弹出原生「选择文件夹」对话框，返回 ``{"path": ...}`` 或空。"""
        if self._window is None:
            return {"path": ""}
        # 兼容新旧 pywebview：优先用新的 FileDialog.FOLDER 枚举。
        folder_dialog = getattr(
            getattr(webview, "FileDialog", None), "FOLDER", None
        )
        if folder_dialog is None:
            folder_dialog = webview.FOLDER_DIALOG
        result = self._window.create_file_dialog(folder_dialog)
        if not result:
            return {"path": ""}
        return {"path": result[0]}

    def delete_snapshot(self, path: str) -> dict:
        """删除一个快照文件。

        顺序很关键：**先**释放对比会话（它持有该文件的 sqlite 只读连接，
        Windows 上打开的句柄会让删除报 WinError 32），**再**删文件。
        """
        with self._diff_lock:
            if self._diff_key and path in self._diff_key:
                self._close_diff()
        try:
            store.delete_snapshot(path)
        except OSError as exc:
            return {"error": i18n.t(
                f"删除失败，文件可能正被其它程序占用：{exc}",
                f"Delete failed; the file may be in use by another program: {exc}",
            )}
        return {"ok": True}

    # ---- 设置 -----------------------------------------------------------

    def get_settings(self) -> dict:
        """返回当前设置与环境信息（版本、存放目录、扫描线程数、是否压缩、CPU 核数、界面语言、是否管理员）。"""
        return {
            "version": APP_VERSION,
            "snapshot_dir": store.default_snapshot_dir(),
            "scan_workers": store.get_scan_workers(),
            "compress_snapshots": store.get_compress_snapshots(),
            "cpu_count": os.cpu_count() or 2,
            "lang": i18n.get_lang(),
            "is_admin": _is_admin(),
        }

    def set_language(self, lang: str) -> dict:
        """由前端在启动/手动切换时调用，同步后端报错文案的语言。"""
        i18n.set_lang(lang)
        self._refresh_window_title()
        return {"ok": True, "lang": i18n.get_lang()}

    def set_scan_workers(self, n: int) -> dict:
        """设置本次会话的扫描线程数，下次扫描生效（不持久化，重启回默认）。"""
        try:
            return {"ok": True, "scan_workers": store.set_scan_workers(n)}
        except (TypeError, ValueError) as exc:
            return {"error": i18n.t(
                f"设置线程数失败：{exc}", f"Failed to set thread count: {exc}")}

    def set_compress_snapshots(self, enabled: bool) -> dict:
        """设置扫描完成后是否压缩快照（``.db`` → ``.dbz``），仅本次会话。"""
        return {
            "ok": True,
            "compress_snapshots": store.set_compress_snapshots(bool(enabled)),
        }

    def open_snapshot_dir(self) -> dict:
        """在资源管理器中打开快照存放目录。"""
        path = store.default_snapshot_dir()
        try:
            os.startfile(path)  # noqa: S606 - 打开的是自己管理的目录
        except OSError as exc:
            return {"error": i18n.t(
                f"无法打开目录 {path}（{exc}）",
                f"Cannot open folder {path} ({exc})")}
        return {"ok": True}

    def open_url(self, url: str) -> dict:
        """用系统默认浏览器打开 http(s) 链接。"""
        if not isinstance(url, str) or not (
            url.startswith("https://") or url.startswith("http://")
        ):
            return {"error": i18n.t("无效的链接", "Invalid URL")}
        try:
            os.startfile(url)  # noqa: S606 - 仅打开经校验的 http(s) URL
        except OSError as exc:
            return {"error": i18n.t(
                f"无法打开链接（{exc}）",
                f"Cannot open link ({exc})")}
        return {"ok": True}

    def set_theme(self, theme: str) -> dict:
        """前端切换主题时同步窗口标题栏的明暗（仅 Windows 有效）。

        启动阶段前端可能连打几次；主题没变就立刻返回，避免反复 DWM/尺寸微扰拖慢首屏。
        """
        dark = theme == "dark"
        self._hook_titlebar_theme()
        # 已成功刷成同一主题：直接返回，避免启动阶段重复 DWM/尺寸微扰。
        if self._titlebar_applied is not None and self._titlebar_applied == dark:
            self._titlebar_dark = dark
            return {"ok": True, "theme": "dark" if dark else "light", "skipped": True}
        self._titlebar_dark = dark
        self._apply_titlebar(dark, force_nudge=True)
        self._schedule_titlebar_refresh(delays_ms=(80, 280))
        return {"ok": True, "theme": "dark" if dark else "light"}

    def _hook_titlebar_theme(self) -> None:
        """接管 pywebview 的标题栏主题逻辑，改跟应用主题走。

        pywebview WinForms 后端在窗口创建时、以及系统「应用使用浅色」变化时，
        会按注册表 AppsUseLightTheme 重刷标题栏。这和本程序自己的暗/浅切换
        冲突。另外 Win10 上仅 SetWindowPos(FRAMECHANGED) 常常不换色，
        用户一点最大化（真的改了客户区尺寸）才刷新——所以这里还挂上 Resize。
        """
        if os.name != "nt" or self._titlebar_hooked:
            return
        form = getattr(self._window, "native", None) if self._window else None
        if form is None:
            return
        try:
            # 用实例属性盖掉类方法；UserPreferenceChanged 仍会调到这里。
            form.update_title_bar_theme = (
                lambda *a, **k: self._apply_titlebar(
                    self._titlebar_dark, force_nudge=False
                )
            )
            # 最大化/还原会改尺寸，借这次系统重绘把标题栏颜色钉牢。
            form.Resize += (
                lambda *a, **k: self._apply_titlebar_hwnd(
                    self._titlebar_dark, force_nudge=False
                )
            )
            self._titlebar_hooked = True
        except Exception:  # noqa: BLE001 - 钩不上就只靠主动 set_theme
            pass

    def _run_on_ui(self, fn) -> None:
        """把可调用对象丢到 WinForms UI 线程执行；没有窗口则直接跑。"""
        form = getattr(self._window, "native", None) if self._window else None
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

    def _schedule_titlebar_refresh(
        self, delays_ms: tuple[int, ...] = (100, 320)
    ) -> None:
        """启动/切主题后延迟再刷（默认 2 次，够修 Win10，又不太拖首屏）。

        每次调度递增 generation，旧 timer 回调自动作废，避免 shown/loaded/set_theme
        叠在一起把 UI 线程打满。
        """
        if os.name != "nt":
            return
        form = getattr(self._window, "native", None) if self._window else None
        if form is None:
            return
        self._titlebar_refresh_gen += 1
        gen = self._titlebar_refresh_gen
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
                    if g != self._titlebar_refresh_gen:
                        return
                    self._apply_titlebar_hwnd(
                        self._titlebar_dark, force_nudge=True
                    )

                timer.Tick += _tick
                timer.Start()
        except Exception:  # noqa: BLE001
            def _later(delay: float, g: int = gen) -> None:
                import time

                time.sleep(delay)
                if g != self._titlebar_refresh_gen:
                    return
                self._run_on_ui(
                    lambda: self._apply_titlebar_hwnd(
                        self._titlebar_dark, force_nudge=True
                    )
                )

            for ms in delays_ms:
                threading.Thread(
                    target=_later, args=(ms / 1000.0,), daemon=True
                ).start()

    def _apply_titlebar(
        self, dark: bool | None = None, *, force_nudge: bool = False
    ) -> None:
        """用 DWM 把原生标题栏刷成暗/亮色。"""
        if os.name != "nt":
            return
        if dark is None:
            dark = self._titlebar_dark
        else:
            self._titlebar_dark = bool(dark)

        target = self._titlebar_dark
        nudge = force_nudge
        self._run_on_ui(
            lambda: self._apply_titlebar_hwnd(target, force_nudge=nudge)
        )

    def _apply_titlebar_hwnd(
        self, dark: bool, *, force_nudge: bool = False
    ) -> None:
        """对当前窗口句柄写入 DWM 暗色属性并强制标题栏重绘。

        force_nudge：非最大化时对窗口宽做 +1/-1 像素抖动。Win10 上这和
        「点最大化」一样会逼 DWM 重画标题栏；Resize 回调里不要开，防抖死循环。
        """
        hwnd = self._hwnd()
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
            self._titlebar_applied = bool(dark)
        except Exception:  # noqa: BLE001 - 装饰性功能，失败不影响使用
            pass

    def _apply_icon(self, ico_path: str) -> None:
        """把窗口标题栏/任务栏图标设成指定的 .ico（仅 Windows）。"""
        if os.name != "nt" or not os.path.exists(ico_path):
            return
        hwnd = self._hwnd()
        if not hwnd:
            return
        try:
            import ctypes

            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x0010
            LR_DEFAULTSIZE = 0x0040
            WM_SETICON = 0x0080
            ICON_SMALL, ICON_BIG = 0, 1
            user32 = ctypes.windll.user32
            hicon = user32.LoadImageW(
                None, ico_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
            )
            if hicon:
                user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon)
                user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon)
        except Exception:  # noqa: BLE001 - 图标是锦上添花，失败不影响使用
            pass

    def _refresh_window_title(self) -> None:
        """按当前语言与是否管理员刷新窗口标题栏文案。"""
        title = _window_title()
        if self._window is None:
            return
        try:
            self._window.set_title(title)
        except Exception:  # noqa: BLE001
            try:
                self._window.title = title
            except Exception:  # noqa: BLE001
                pass

    def _hwnd(self) -> int:
        """尽力取到顶层窗口句柄：先问 pywebview 原生对象，再按标题兜底。"""
        try:
            native = getattr(self._window, "native", None)
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
            for title in (_window_title(), "WhoShitsOnMyC"):
                hwnd = user32.FindWindowW(None, title)
                if hwnd:
                    return hwnd
            return 0
        except Exception:  # noqa: BLE001
            return 0

    def reveal_path(self, root: str, rel_path: str) -> dict:
        """在资源管理器中定位一个对比节点对应的真实路径。

        目标已被删除时退而求其次打开其父目录；父目录也没了才报错。
        """
        full = os.path.join(root, rel_path) if rel_path else root
        if os.path.exists(full):
            # /select, 让资源管理器打开父目录并选中该项。
            subprocess.Popen(["explorer", f"/select,{full}"])  # noqa: S603,S607
            return {"ok": True}
        parent = os.path.dirname(full)
        if os.path.isdir(parent):
            os.startfile(parent)  # noqa: S606
            return {"ok": True, "message": i18n.t(
                "该项已不存在，已打开其所在目录",
                "This item no longer exists; opened its parent folder instead")}
        return {"error": i18n.t(
            f"路径已不存在：{full}", f"Path no longer exists: {full}")}

    # ---- 扫描（后台线程 + 进度推送）------------------------------------

    def start_scan(self, root: str, follow_symlinks: bool = False) -> dict:
        """在后台线程扫描 ``root``，进度与结果通过事件推送。

        事件：``scan-progress`` {files, current} / ``scan-done`` {snapshot} /
        ``scan-error`` {message} / ``scan-cancelled`` {}。

        Returns:
            立即返回 ``{"started": True}``；已有扫描在跑则返回 error。
        """
        if self._scan_thread and self._scan_thread.is_alive():
            return {"error": i18n.t(
                "已有扫描正在进行", "A scan is already in progress")}
        if not os.path.isdir(root):
            return {"error": i18n.t(
                f"目录不存在：{root}", f"Folder does not exist: {root}")}

        self._cancel.clear()
        self._scan_thread = threading.Thread(
            target=self._run_scan, args=(root, follow_symlinks), daemon=True
        )
        self._scan_thread.start()
        return {"started": True}

    def cancel_scan(self) -> dict:
        """请求取消正在进行的扫描。"""
        self._cancel.set()
        return {"ok": True}

    def _run_scan(self, root: str, follow_symlinks: bool) -> None:
        """后台线程体：执行扫描、回报进度、发终态事件。"""
        db_path = store.new_snapshot_path(root)
        final_path = db_path

        def on_progress(files: int, current: str) -> None:
            self._emit("scan-progress", {"files": files, "current": current})

        try:
            meta = scan_to_snapshot(
                root,
                db_path,
                follow_symlinks=follow_symlinks,
                progress=on_progress,
                cancel=self._cancel.is_set,
                workers=store.get_scan_workers(),
            )
            # 扫完可选压缩：失败时保留 .db，不把整次扫描判失败。
            if store.get_compress_snapshots():
                self._emit("scan-progress", {
                    "files": meta.file_count,
                    "current": i18n.t("正在压缩快照…", "Compressing snapshot…"),
                })
                try:
                    final_path = compress_db(db_path, meta)
                except CompressError as exc:
                    traceback.print_exc()
                    self._emit(
                        "scan-done",
                        {
                            "snapshot": {
                                "path": db_path,
                                "root": meta.root,
                                "scanned_at": meta.scanned_at,
                                "total_size": meta.total_size,
                                "file_count": meta.file_count,
                                "skipped_count": len(meta.skipped),
                                "compressed": False,
                            },
                            "warning": i18n.t(
                                f"快照已保存，但压缩失败，已保留未压缩文件：{exc}",
                                f"Snapshot saved, but compression failed; kept uncompressed file: {exc}",
                            ),
                        },
                    )
                    return
        except ScanCancelled:
            store.delete_snapshot(db_path)  # 丢弃不完整快照
            self._emit("scan-cancelled", {})
        except Exception as exc:  # noqa: BLE001 - 兜底，任何异常都不该让线程静默死掉
            store.delete_snapshot(db_path)
            traceback.print_exc()
            self._emit("scan-error", {"message": str(exc)})
        else:
            self._emit(
                "scan-done",
                {
                    "snapshot": {
                        "path": final_path,
                        "root": meta.root,
                        "scanned_at": meta.scanned_at,
                        "total_size": meta.total_size,
                        "file_count": meta.file_count,
                        "skipped_count": len(meta.skipped),
                        "compressed": final_path.lower().endswith(".dbz"),
                    }
                },
            )

    # ---- 对比 / 下钻 ---------------------------------------------------

    def compare(self, old_path: str, new_path: str) -> dict:
        """对比两份快照，返回概览 + 顶层变化节点。

        Returns:
            成功：``{"summary": {...}, "nodes": [...]}``；
            失败：``{"error": "..."}``（如根不一致、文件损坏）。
        """
        try:
            with self._diff_lock:
                self._ensure_diff(old_path, new_path)
                assert self._diff is not None
                nodes = self._diff.compare_children("")
                return {
                    "summary": self._summary(self._diff),
                    "nodes": [n.to_dict() for n in nodes],
                }
        except (DiffError, SnapshotError, CompressError) as exc:
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - 任何异常都要回 JSON，不能让前端悬死
            traceback.print_exc()
            return {"error": i18n.t(f"对比失败：{exc}", f"Comparison failed: {exc}")}

    def get_children(self, old_path: str, new_path: str, parent: str) -> dict:
        """下钻：返回某父目录下的直接子节点对比结果。"""
        try:
            with self._diff_lock:
                self._ensure_diff(old_path, new_path)
                assert self._diff is not None
                nodes = self._diff.compare_children(parent)
                return {"nodes": [n.to_dict() for n in nodes]}
        except (DiffError, SnapshotError, CompressError) as exc:
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - 同上，兜底成 error 响应
            traceback.print_exc()
            return {"error": i18n.t(
                f"读取子目录失败：{exc}", f"Failed to read subfolder: {exc}")}

    def _ensure_diff(self, old_path: str, new_path: str) -> None:
        """确保当前 :class:`Diff` 会话对应给定的两份快照，否则重开。

        压缩快照（``.dbz``）在这里才解压到缓存；列表/选择阶段不解压。
        """
        key = (old_path, new_path)
        if self._diff is not None and self._diff_key == key:
            return
        self._close_diff()
        try:
            old_db = ensure_db_path(old_path)
            new_db = ensure_db_path(new_path)
        except (CompressError, SnapshotError):
            raise
        self._diff = Diff(old_db, new_db)
        self._diff_key = key

    def _close_diff(self) -> None:
        if self._diff is not None:
            self._diff.close()
        self._diff = None
        self._diff_key = None

    @staticmethod
    def _summary(diff: Diff) -> dict:
        """组装界面顶部概览所需的数据。"""
        return {
            "old": {
                "root": diff.old_meta.root,
                "scanned_at": diff.old_meta.scanned_at,
                "total_size": diff.old_meta.total_size,
                "skipped_count": len(diff.old_meta.skipped),
            },
            "new": {
                "root": diff.new_meta.root,
                "scanned_at": diff.new_meta.scanned_at,
                "total_size": diff.new_meta.total_size,
                "skipped_count": len(diff.new_meta.skipped),
            },
            "total_delta": diff.total_delta,
        }


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
    """
    if _is_admin():
        mode = i18n.t("管理员", "Administrator")
    else:
        mode = i18n.t(
            "非管理员（推荐以管理员启动）",
            "Not Administrator (run as Administrator recommended)",
        )
    return f"WhoShitsOnMyC — {mode} · v{APP_VERSION}"


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


# WebView2 常青运行时（Evergreen Runtime）的固定注册表 GUID。
_WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
_WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"
# 随包附带「固定版本(Fixed Version)」运行时时，约定解压到资源目录下这个文件夹。
_WEBVIEW2_BUNDLE_DIR = "webview2_runtime"


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
        print(msg)
    try:
        os.startfile(_WEBVIEW2_URL)  # noqa: S606 - 打开官方下载页
    except OSError:
        pass


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


def main() -> None:
    """创建窗口并启动应用。"""
    # 先按系统语言定界面语言，好让「缺少 WebView2」这类开窗前的弹窗已本地化；
    # 前端起来后会再调 set_language 校准（含用户手动切换）。
    i18n.set_lang(_detect_lang())
    # 优先用随包附带的固定版本运行时；没有再检测系统运行时，仍缺则引导安装。
    if not _wire_bundled_webview2() and not _webview2_installed():
        _warn_missing_webview2()
        return

    api = Api()
    width, height = 1100, 720
    x, y = _centered_xy(width, height)
    window = webview.create_window(
        title=_window_title(),
        url=os.path.join(_WEB_DIR, "index.html"),
        js_api=api,
        width=width,
        height=height,
        x=x,
        y=y,
        min_size=(820, 560),
        background_color="#0f1116",
    )
    api.set_window(window)
    # 窗口一出现：hook 标题栏主题 + 图标 + 轻量上色。
    # 真正按 localStorage 校准主题由前端 set_theme 完成；这里少做事，加快首屏。
    def _on_shown() -> None:
        api._refresh_window_title()
        api._hook_titlebar_theme()
        api._apply_icon(_ICON_PATH)
        # 不 force_nudge：避免启动瞬间抖窗口；后续 set_theme / 延迟刷新补上。
        api._apply_titlebar(api._titlebar_dark, force_nudge=False)

    try:
        window.events.shown += _on_shown
    except Exception:  # noqa: BLE001
        pass
    try:
        window.events.restored += lambda: api._apply_titlebar(
            api._titlebar_dark, force_nudge=False
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        # 页面就绪后再钉一次（前端多半已 set_theme）；只调度少量延迟刷新。
        def _on_loaded() -> None:
            api._hook_titlebar_theme()
            api._apply_titlebar(api._titlebar_dark, force_nudge=False)
            api._schedule_titlebar_refresh(delays_ms=(120,))

        window.events.loaded += _on_loaded
    except Exception:  # noqa: BLE001
        pass
    start_kwargs = {}
    if os.path.exists(_ICON_PATH):
        start_kwargs["icon"] = _ICON_PATH  # 任务栏/GUI 图标
    try:
        webview.start(**start_kwargs)
    except TypeError:
        # 个别后端不接受 icon 参数，退回无图标启动（窗口图标仍由 _apply_icon 补）。
        webview.start()


if __name__ == "__main__":
    main()
