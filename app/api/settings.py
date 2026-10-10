"""设置读写与应用（JS-API Mixin）。"""

from __future__ import annotations

import os
import threading

from core import applog, i18n, store
from version import __version__ as APP_VERSION

from app.bootstrap import (
    _diff_common_settings,
    _detect_lang,
    _is_admin,
    _sync_log_level_to_applog,
    _sync_log_sanitize_to_applog,
)


class SettingsMixin:
    """设置项 getter/setter、应用设置后台线程、快照目录。"""

    def get_settings(self) -> dict:
        """返回当前设置与环境信息。"""
        d = store.settings_dict()
        d.update(
            {
                "version": APP_VERSION,
                "cpu_count": os.cpu_count() or 2,
                "is_admin": _is_admin(),
                "mft_platform_ok": os.name == "nt",
                # 与 i18n 运行时保持一致（store.lang 为权威来源之一）
                "lang": store.get_lang() or i18n.get_lang(),
            }
        )
        return d

    def set_language(self, lang: str) -> dict:
        """由前端在启动/手动切换时调用；同步 i18n 与 store（可写 YAML）。"""
        code = store.set_lang(lang)
        i18n.set_lang(code)
        self._refresh_window_title()
        return {"ok": True, "lang": i18n.get_lang()}

    def set_scan_workers(self, n: int) -> dict:
        """设置扫描线程数，下次扫描生效；值变化时写 YAML。"""
        try:
            return {"ok": True, "scan_workers": store.set_scan_workers(n)}
        except (TypeError, ValueError) as exc:
            return {"error": i18n.t(
                f"设置线程数失败：{exc}", f"Failed to set thread count: {exc}")}

    def set_compress_snapshots(self, enabled: bool) -> dict:
        """设置扫描完成后是否压缩快照（``.db`` → ``.dbz``）。"""
        return {
            "ok": True,
            "compress_snapshots": store.set_compress_snapshots(bool(enabled)),
        }

    def set_use_mft(self, enabled: bool) -> dict:
        """设置是否对盘符根 NTFS 尝试 MFT（通常需管理员；失败回退目录扫描）。"""
        return {"ok": True, "use_mft": store.set_use_mft(bool(enabled))}

    def set_search_memory_index(self, enabled: bool) -> dict:
        """设置是否在打开搜索时使用内存索引加速。"""
        return {
            "ok": True,
            "search_memory_index": store.set_search_memory_index(bool(enabled)),
        }

    def set_remember_window_size(self, enabled: bool) -> dict:
        """设置是否记住窗口大小（写入 settings.yaml）。"""
        return {
            "ok": True,
            "remember_window_size": store.set_remember_window_size(bool(enabled)),
        }

    def reset_settings(self) -> dict:
        """恢复默认设置并删除 ``settings.yaml``（不删除快照文件）。

        语言回到冷启动默认（系统语言判定），主题 light，线程/压缩/MFT/目录回内置。
        """
        # 与无 yaml 冷启动一致：中文系统 → zh，否则 en
        default_lang = _detect_lang()
        try:
            d = store.reset_settings_to_defaults(lang=default_lang)
        except Exception as exc:  # noqa: BLE001
            applog.exception("reset_settings failed", exc)
            return {"error": i18n.t(
                f"恢复默认失败：{exc}", f"Failed to restore defaults: {exc}")}
        i18n.set_lang(store.get_lang())
        try:
            self._titlebar.set_theme(store.resolve_theme(store.get_theme()))
        except Exception:  # noqa: BLE001
            pass
        # 可选模块各自 reset（如 AI：清运行中请求、删旧 ai.json）
        for _name, inst in list(self._modules.items()):
            reset_fn = getattr(inst, "reset", None)
            if callable(reset_fn):
                try:
                    reset_fn()
                except Exception as exc:  # noqa: BLE001
                    applog.exception(f"module {_name} reset failed", exc)
        self._refresh_window_title()
        # 恢复默认后 yaml 已删：显式标志清空，可再吃环境变量
        _sync_log_sanitize_to_applog()
        _sync_log_level_to_applog()
        applog.log_settings_event("settings", "reset to defaults", level="INFO")
        out = {"ok": True}
        out.update(d)
        out.update(
            {
                "version": APP_VERSION,
                "cpu_count": os.cpu_count() or 2,
                "is_admin": _is_admin(),
                "mft_platform_ok": os.name == "nt",
            }
        )
        return out

    def apply_settings(self, payload: dict | None = None) -> dict:
        """设置页点「完成」时统一提交：线程/压缩/MFT/目录一次写入并自动持久化。

        在后台线程执行，立即返回 ``{"started": True}``。
        目录变更时推送 ``migrate-progress``；结束时推送 ``settings-applied``
        （含完整设置结果 / 错误；若有迁移还带 ``migrate`` 与 ``snapshot_dir_changed``）。
        兼容旧事件名：迁移结束时也会再发 ``migrate-done``。
        """
        if self._settings_thread and self._settings_thread.is_alive():
            return {"error": i18n.t(
                "正在应用设置，请稍候",
                "Settings are still being applied",
            )}
        body = dict(payload or {})
        self._settings_thread = threading.Thread(
            target=self._run_apply_settings, args=(body,), daemon=True
        )
        self._settings_thread.start()
        return {"started": True}

    def _run_apply_settings(self, payload: dict) -> None:
        """后台应用设置；边迁移边推进度，结束推 settings-applied。"""
        def on_progress(info: dict) -> None:
            self._emit("migrate-progress", dict(info or {}))

        before = store.settings_dict()
        try:
            d = store.apply_settings(payload, progress=on_progress)
        except OSError as exc:
            applog.exception("apply_settings failed", exc)
            self._emit(
                "settings-applied",
                {
                    "ok": False,
                    "error": i18n.t(
                        f"应用设置失败：{exc}",
                        f"Failed to apply settings: {exc}",
                    ),
                },
            )
            return
        except (TypeError, ValueError) as exc:
            applog.exception("apply_settings failed", exc)
            self._emit(
                "settings-applied",
                {
                    "ok": False,
                    "error": i18n.t(
                        f"应用设置失败：{exc}",
                        f"Failed to apply settings: {exc}",
                    ),
                },
            )
            return
        except Exception as exc:  # noqa: BLE001 — 终态必须回前端
            applog.exception("apply_settings failed", exc)
            self._emit(
                "settings-applied",
                {
                    "ok": False,
                    "error": i18n.t(
                        f"应用设置失败：{exc}",
                        f"Failed to apply settings: {exc}",
                    ),
                },
            )
            return

        # 日志脱敏 / 等级：设置项已写入则同步 applog（设置优先于环境变量）
        _sync_log_sanitize_to_applog()
        _sync_log_level_to_applog()
        # 变更日志走统一接口（DEBUG）；路径原文写入，是否脱敏由 applog 开关决定
        applog.log_settings_changed("settings", _diff_common_settings(before, d))
        out = {"ok": True}
        out.update(d)
        if d.get("snapshot_dir_changed"):
            mig = d.get("migrate") or {}
            applog.log_settings_event(
                "settings",
                "snapshot dir migrated"
                f" | moved={int(mig.get('moved') or 0)}"
                f" | skipped={int(mig.get('skipped') or 0)}"
                f" | failed={int(mig.get('failed') or 0)}",
            )
            self._emit(
                "migrate-done",
                {
                    "moved": int(mig.get("moved") or 0),
                    "skipped": int(mig.get("skipped") or 0),
                    "failed": int(mig.get("failed") or 0),
                    "total": int(mig.get("total") or 0),
                    "errors": list(mig.get("errors") or []),
                    "snapshot_dir": d.get("snapshot_dir") or "",
                },
            )
        self._emit("settings-applied", out)

    def set_snapshot_dir(self, path: str = "") -> dict:
        """设置默认快照存放目录；空串恢复内置路径。"""
        try:
            effective = store.set_snapshot_dir(path if path is not None else "")
        except OSError as exc:
            return {"error": i18n.t(
                f"无法使用该目录：{exc}",
                f"Cannot use that folder: {exc}",
            )}
        return {
            "ok": True,
            "snapshot_dir": effective,
            "snapshot_dir_configured": store.get_snapshot_dir_configured(),
            "snapshot_dir_builtin": store.builtin_snapshot_dir(),
            "snapshot_dir_is_custom": bool(store.get_snapshot_dir_configured()),
        }

    def choose_snapshot_dir(self) -> dict:
        """弹出文件夹选择框，设为默认快照目录。取消则不改。"""
        picked = self.choose_folder()
        path = (picked or {}).get("path") or ""
        if not path:
            return {"cancelled": True}
        return self.set_snapshot_dir(path)

    def pick_snapshot_dir(self) -> dict:
        """仅弹出文件夹选择（不写入设置），供设置页草稿选用。"""
        picked = self.choose_folder()
        path = (picked or {}).get("path") or ""
        if not path:
            return {"cancelled": True}
        abspath = os.path.abspath(path)
        return {
            "ok": True,
            "path": abspath,
            "snapshot_dir": abspath,
            "snapshot_dir_configured": abspath,
            "snapshot_dir_builtin": store.builtin_snapshot_dir(),
            "snapshot_dir_is_custom": True,
        }

    def reset_snapshot_dir(self) -> dict:
        """恢复内置默认快照目录。"""
        return self.set_snapshot_dir("")

    def open_snapshot_dir(self) -> dict:
        """在资源管理器中打开当前快照存放目录。"""
        path = store.default_snapshot_dir()
        try:
            os.startfile(path)  # noqa: S606 - 打开的是自己管理的目录
        except OSError as exc:
            return {"error": i18n.t(
                f"无法打开目录 {path}（{exc}）",
                f"Cannot open folder {path} ({exc})")}
        return {"ok": True}

    def set_theme(self, theme: str) -> dict:
        """前端切换主题：记入 store（可写 YAML）并同步标题栏。

        ``theme`` 可为 ``light``/``dark``/``auto``（auto=跟随系统，
        启动时解析一次）。标题栏需要实际值，auto 会被解析成 dark/light。
        """
        code = store.set_theme(theme)
        resolved = store.resolve_theme(code)
        try:
            self._titlebar.set_theme(resolved)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "theme": code, "resolved": resolved}
