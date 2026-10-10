"""窗口尺寸、工具侧栏与系统入口（JS-API Mixin）。"""

from __future__ import annotations

import os
import threading

from core import applog, i18n, store, fs_migrate, wingeom

from app.bootstrap import _clamp_window_size, _window_is_maximized


class WindowMixin:
    """窗口尺寸记忆/防抖落盘、侧栏开合伸缩、打开目录与链接。"""

    def report_window_size(self, width: int = 0, height: int = 0) -> dict:
        """前端/窗口尺寸稳定后上报；仅开启「记住窗口大小」时写入 YAML。

        防抖在调用方完成（保持 15 秒不变再调）。尺寸未变则不写盘。
        """
        if not store.get_remember_window_size():
            return {"ok": True, "saved": False, "remember_window_size": False}
        try:
            w, h = store.set_window_size(int(width), int(height), persist=True)
        except (TypeError, ValueError) as exc:
            return {"error": i18n.t(
                f"保存窗口大小失败：{exc}",
                f"Failed to save window size: {exc}",
            )}
        return {
            "ok": True,
            "saved": True,
            "remember_window_size": True,
            "window_width": w,
            "window_height": h,
        }

    def _schedule_window_size_save(self, width: int, height: int) -> None:
        """窗口尺寸变化：8 秒保持不变才写入 YAML（仅开启记住时）。

        侧栏开合的一次性窗口伸缩也会触发本函数，防抖后落的是终态宽度，
        再经 ``wingeom.to_stored_width`` 归一化回「展开态宽度」，不会逐次漂移。
        """
        if not store.get_remember_window_size():
            return
        try:
            w, h = int(width), int(height)
        except (TypeError, ValueError):
            return
        if w < 820 or h < 560:
            return
        with self._window_size_lock:
            self._pending_window_size = (w, h)
            if self._window_size_timer is not None:
                try:
                    self._window_size_timer.cancel()
                except Exception:  # noqa: BLE001
                    pass
            t = threading.Timer(8.0, self._flush_window_size_save)
            t.daemon = True
            self._window_size_timer = t
            t.start()

    def _flush_window_size_save(self) -> None:
        with self._window_size_lock:
            pending = self._pending_window_size
            self._window_size_timer = None
            self._pending_window_size = None
        if not pending or not store.get_remember_window_size():
            return
        w, h = pending
        try:
            # 存的是「侧栏展开时的宽度」：收起态补回侧栏宽，保证开合不漂移
            stored_w = wingeom.to_stored_width(
                w,
                panel_open=store.get_tool_panel_open(),
                panel_width=store.get_tool_panel_width(),
            )
            store.set_window_size(stored_w, h, persist=True)
            applog.info(f"window size saved: {w}x{h} (stored width {stored_w})")
        except Exception as exc:  # noqa: BLE001
            applog.warn(f"save window size failed: {exc}")

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

    def open_folder(self, path: str = "") -> dict:
        """在资源管理器中打开一个绝对路径目录。"""
        p = fs_migrate.normalize_abs(path or "")
        if not p or not os.path.isdir(p):
            return {"error": i18n.t("目录不存在", "Folder does not exist")}
        try:
            os.startfile(p)  # noqa: S606
        except OSError as exc:
            return {
                "error": i18n.t(f"无法打开：{exc}", f"Cannot open: {exc}"),
                "code": "os",
            }
        return {"ok": True, "path": p}

    def set_tool_panel_open(self, open: bool = False, width: int = 340) -> dict:
        """工具侧栏展开/收起：只记住状态与宽度，不直接改窗口尺寸。

        窗口宽度的伸缩由前端编排（见 pending.js）：展开时先一次性加宽
        窗口，主区钉死原宽度，侧栏再用 CSS 过渡滑入新增区域；收起反向。
        本方法只持久化最终状态，供下次启动恢复。
        """
        panel_open = bool(open)
        panel_w = wingeom.clamp_panel_width(width)
        store.set_tool_panel_width(panel_w)
        store.set_tool_panel_open(panel_open)
        return {
            "ok": True,
            "panel_open": panel_open,
            "panel_width": panel_w,
        }

    def resize_window_for_panel(self, open: bool = False, panel_width: int = 0) -> dict:
        """侧栏开合时一次性调整窗口宽度，高度不变。

        原生 resize 是异步的且多次调用会被合并，无法与 CSS 过渡逐帧对齐，
        故不再逐帧驱动：展开传 ``open=True`` 与目标侧栏宽，窗口一次加宽
        ``panel_width``；收起传 ``open=False``，窗口收回之前加宽的量
        （``self._panel_win_extra`` 记账，重复同向调用不叠加）。
        最大化时窗口宽度锁死在屏幕，直接拒绝。主区宽度恒定由前端
        钉宽保证（pending.js 的 _pinMainWidth）。
        """
        win = self._window
        if win is None or self._last_win_h <= 0:
            return {"ok": False}
        if _window_is_maximized(win):
            return {"ok": False, "maximized": True}
        panel_w = wingeom.clamp_panel_width(panel_width)
        with self._panel_win_lock:
            if bool(open) == (self._panel_win_extra > 0):
                return {"ok": True, "nochange": True}
            if open:
                target = int(self._last_win_w) + panel_w
            else:
                target = int(self._last_win_w) - self._panel_win_extra
            target, h = _clamp_window_size(target, int(self._last_win_h))
            if self._resize_window_width_height(target, h):
                # 立即记下目标宽度：resized 事件即使滞后，下次计算也不失真
                self._last_win_w = target
                self._panel_win_extra = panel_w if open else 0
                return {"ok": True, "width": target, "height": h}
            return {"ok": False}

    def set_tool_panel_width(self, width: int = 340) -> dict:
        """拖拽手柄改侧栏宽度：只记宽度，不改窗口尺寸。

        拖拽挤压的是主区，窗口宽度不变；展开态下存储宽度等于当前实际
        窗口宽度，本来就与新宽度自洽，无需额外换算。
        """
        return {"ok": True, "tool_panel_width": store.set_tool_panel_width(width)}

    def sync_tool_panel_state(self, open: bool = False, width: int = 340) -> dict:
        """启动时把旧版 localStorage 里的侧栏状态搬进 YAML，只搬一次。

        返回 YAML 里的权威值，前端按返回值渲染。
        """
        store.migrate_tool_panel_state(bool(open), width)
        return {
            "ok": True,
            "tool_panel_open": store.get_tool_panel_open(),
            "tool_panel_width": store.get_tool_panel_width(),
        }

    # ---- 侧栏宽度换算 -------------------------------------------------

    def _resize_window_width_height(self, width: int, height: int) -> bool:
        """把窗口尺寸改到 ``width x height``（逻辑像素）；失败返回 False。"""
        win = self._window
        if win is None:
            return False
        try:
            win.resize(int(width), int(height))
        except Exception as exc:  # noqa: BLE001
            applog.warn(f"resize window failed: {exc}")
            return False
        return True

    # ---- 扫描（后台线程 + 进度推送）------------------------------------
