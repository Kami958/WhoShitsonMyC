"""设置读写与应用（JS-API Mixin）。"""

from __future__ import annotations

import os
import threading

from core import applog, i18n, store
from version import __version__ as APP_VERSION

from app.bootstrap import (
    _data_dir_choice_error_message,
    _data_dir_unavailable_message,
    _diff_common_settings,
    _detect_lang,
    _is_admin,
    _settings_recovered_message,
    _settings_write_failed_message,
    _snapshot_dir_choice_error_message,
    _snapshot_dir_error_message,
    _snapshot_dir_not_saved_message,
    _snapshot_dir_reset_error_message,
    _sync_log_level_to_applog,
    _sync_log_sanitize_to_applog,
)


def _with_settings_warnings(d: dict) -> dict:
    """给设置结果补上设置文件恢复与快照目录不可读的提示文案。"""
    if d.get("settings_recovered"):
        d["settings_warning"] = _settings_recovered_message()
    if d.get("snapshot_dir_unavailable"):
        d["snapshot_dir_warning"] = _snapshot_dir_error_message()
    return d


class SettingsMixin:
    """设置项 getter/setter、应用设置后台线程、快照目录。"""

    def get_settings(self) -> dict:
        """返回当前设置与环境信息。

        应用数据目录不可用时只返回 ``{"error": 可读文案}``，不把内部错误对象
        混进正常设置结构。自定义快照目录不可用仍返回设置，另带
        ``snapshot_dir_warning``，由设置页与启动提示显示。
        """
        d = store.settings_dict()
        if d.get("error"):
            return {
                "error": _data_dir_unavailable_message(
                    str(d.get("path") or ""),
                    str(d.get("reason") or ""),
                ),
            }
        _with_settings_warnings(d)
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
        """由前端在启动/手动切换时调用；同步 i18n 与 store（可写 YAML）。

        数据文件夹还没确定时设置写不进去；此时本次会话仍按新语言显示，
        不让界面在无提示的情况下切不动语言。
        """
        try:
            code = store.set_lang(lang)
        except OSError:
            applog.warn("language setting not saved | reason=storage_unavailable")
            code = store.get_lang()
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
        if store.get_snapshot_dir_configured():
            # 恢复默认会把快照目录切回内置目录；内置目录不可写时不能假装成功。
            try:
                builtin = store.builtin_snapshot_dir()
            except OSError as exc:
                info = store.storage_error_payload(exc)
                return {
                    "error": _data_dir_unavailable_message(
                        str(info.get("path") or ""),
                        str(info.get("reason") or ""),
                    ),
                }
            reason = store.snapshot_dir_writable(builtin)
            if reason:
                return {"error": _snapshot_dir_reset_error_message(reason)}
        try:
            d = store.reset_settings_to_defaults(lang=default_lang)
        except store.SnapshotDirUnavailable as exc:
            return {
                "error": _snapshot_dir_reset_error_message(exc.reason),
            }
        except OSError as exc:
            applog.exception("reset_settings failed", exc)
            info = store.storage_error_payload(exc)
            return {
                "error": _data_dir_unavailable_message(
                    str(info.get("path") or ""),
                    str(info.get("reason") or ""),
                ),
            }
        except Exception as exc:  # noqa: BLE001
            applog.exception("reset_settings failed", exc)
            return {"error": i18n.t(
                f"恢复默认失败：{exc}", f"Failed to restore defaults: {exc}")}
        if d.get("error"):
            return {
                "error": _data_dir_unavailable_message(
                    str(d.get("path") or ""),
                    str(d.get("reason") or ""),
                ),
            }
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
        _with_settings_warnings(out)
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
        except store.SnapshotDirUnavailable as exc:
            applog.warn(f"apply_settings rejected snapshot dir | reason={exc.reason}")
            self._emit(
                "settings-applied",
                {
                    "ok": False,
                    "error": _snapshot_dir_choice_error_message(exc.reason),
                },
            )
            return
        except OSError as exc:
            applog.exception("apply_settings failed", exc)
            info = store.storage_error_payload(exc)
            self._emit(
                "settings-applied",
                {
                    "ok": False,
                    "error": _data_dir_unavailable_message(
                        str(info.get("path") or ""),
                        str(info.get("reason") or ""),
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

        if d.get("error"):
            code = str(d.get("error") or "")
            reason = str(d.get("reason") or "")
            if code == "snapshot_dir_not_saved":
                payload_out = {
                    "ok": False,
                    "error": _snapshot_dir_not_saved_message(str(d.get("path") or "")),
                    "migrate": dict(d.get("migrate") or {}),
                    "snapshot_dir_changed": False,
                }
            elif code == "settings_write_failed":
                payload_out = {
                    "ok": False,
                    "error": _settings_write_failed_message(reason),
                    "migrate": dict(d.get("migrate") or {}),
                    "snapshot_dir_changed": False,
                }
            else:
                payload_out = {
                    "ok": False,
                    "error": _data_dir_unavailable_message(
                        str(d.get("path") or ""),
                        reason,
                    ),
                }
            self._emit("settings-applied", payload_out)
            return

        # 日志脱敏 / 等级：设置项已写入则同步 applog（设置优先于环境变量）
        _sync_log_sanitize_to_applog()
        _sync_log_level_to_applog()
        # 变更日志走统一接口（DEBUG）；路径原文写入，是否脱敏由 applog 开关决定
        applog.log_settings_changed("settings", _diff_common_settings(before, d))
        out = {"ok": True}
        out.update(d)
        _with_settings_warnings(out)
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

    def get_storage_status(self) -> dict:
        """恢复界面读取数据目录状态、当前运行账户与指针记录情况。"""
        status = store.storage_status()
        status["ok"] = bool(status.get("ok"))
        status.setdefault("app_data_dir", "")
        status.setdefault("path", "")
        status.setdefault("account", "")
        status.setdefault("pointer_invalid", False)
        reason = str(status.get("reason") or "")
        if status.get("snapshot_dir_unavailable"):
            # 数据根可用，只是快照目录不可用；文案不要和数据根混在一起。
            status["problem_path"] = str(status.get("path") or "")
            status["path"] = str(
                status.get("app_data_dir") or status.get("path") or ""
            )
            status["message"] = _snapshot_dir_choice_error_message(reason)
        elif not status["ok"]:
            status["message"] = _data_dir_unavailable_message(
                str(status.get("app_data_dir") or status.get("path") or ""),
                reason,
            )
        return status

    def get_legacy_data(self) -> dict:
        """检测其他账户下可能存在的旧版本数据文件夹。"""
        return {"candidates": store.legacy_data_candidates()}

    def scan_legacy_conflicts(self, path: str = "") -> dict:
        """扫描导入旧数据时的同名文件，供用户选择保留哪一份。"""
        try:
            conflicts = store.scan_legacy_conflicts(path)
        except store.DataDirUnavailable as exc:
            applog.warn(f"legacy conflict scan rejected | reason={exc.reason}")
            return {
                "error": i18n.t(
                    "旧数据文件夹无法读取，请用该账户打开程序后重试。",
                    "The old data folder cannot be read. Open the app with that account and try again.",
                )
            }
        except OSError:
            applog.warn("legacy conflict scan failed | reason=storage_unavailable")
            return {
                "error": i18n.t(
                    "旧数据文件夹无法读取，请用该账户打开程序后重试。",
                    "The old data folder cannot be read. Open the app with that account and try again.",
                )
            }
        return {"ok": True, **conflicts}

    def import_legacy_data(self, path: str = "", choices: dict | None = None) -> dict:
        """把旧数据复制到当前数据文件夹，旧位置保持原样。"""
        try:
            result = store.import_legacy_data(path, choices or {})
        except store.DataDirUnavailable as exc:
            applog.warn(f"legacy data import rejected | reason={exc.reason}")
            return {
                "error": i18n.t(
                    "旧数据文件夹无法读取，请用该账户打开程序后重试。",
                    "The old data folder cannot be read. Open the app with that account and try again.",
                )
            }
        except OSError:
            applog.warn("legacy data import failed | reason=storage_unavailable")
            return {
                "error": i18n.t(
                    "旧数据文件夹无法读取，请用该账户打开程序后重试。",
                    "The old data folder cannot be read. Open the app with that account and try again.",
                )
            }
        return {"ok": True, **result}

    def choose_data_folder(self) -> dict:
        """让用户选择一个当前运行账户可以写入的数据文件夹。

        选择成功后把当前数据文件夹里的设置、登记、历史和快照移动到新
        位置，并写入指针文件让下次启动继续使用。取消时返回
        ``{"cancelled": True}``。
        """
        picked = self.choose_folder()
        if (picked or {}).get("error"):
            return picked
        path = (picked or {}).get("path") or ""
        if not path:
            return {"cancelled": True}
        try:
            info = store.switch_data_root(path)
        except store.DataDirUnavailable as exc:
            applog.warn(f"data folder rejected | reason={exc.reason}")
            return {"error": _data_dir_choice_error_message(exc.reason)}
        except OSError:
            applog.warn("data folder switch failed | reason=storage_unavailable")
            return {"error": _data_dir_choice_error_message("")}
        i18n.set_lang(store.get_lang())
        try:
            self._titlebar.set_theme(store.resolve_theme(store.get_theme()))
        except Exception:  # noqa: BLE001
            pass
        self._refresh_window_title()
        applog.log_settings_event("settings", "data folder changed", level="INFO")
        out = {"ok": True}
        out.update(info)
        return out

    def set_snapshot_dir(self, path: str = "") -> dict:
        """设置默认快照存放目录；空串恢复内置路径。"""
        try:
            effective = store.set_snapshot_dir(path if path is not None else "")
        except store.SnapshotDirUnavailable as exc:
            message = (
                _snapshot_dir_reset_error_message(exc.reason)
                if not path
                else _snapshot_dir_choice_error_message(exc.reason)
            )
            return {"error": message}
        except OSError:
            return {"error": i18n.t(
                "这个文件夹无法使用，系统拒绝访问该位置；请另选一个可以写入的文件夹。",
                "This folder cannot be used because the system refused access to that location; choose another folder the current account can write to.",
            )}
        try:
            builtin = store.builtin_snapshot_dir()
        except OSError:
            builtin = ""
        return {
            "ok": True,
            "snapshot_dir": effective,
            "snapshot_dir_configured": store.get_snapshot_dir_configured(),
            "snapshot_dir_builtin": builtin,
            "snapshot_dir_is_custom": bool(store.get_snapshot_dir_configured()),
        }

    def choose_snapshot_dir(self) -> dict:
        """弹出文件夹选择框，设为默认快照目录。取消则不改。"""
        picked = self.choose_folder()
        if (picked or {}).get("error"):
            return picked
        path = (picked or {}).get("path") or ""
        if not path:
            return {"cancelled": True}
        return self.set_snapshot_dir(path)

    def pick_snapshot_dir(self) -> dict:
        """仅弹出文件夹选择（不写入设置），供设置页草稿选用。"""
        picked = self.choose_folder()
        if (picked or {}).get("error"):
            return picked
        path = (picked or {}).get("path") or ""
        if not path:
            return {"cancelled": True}
        abspath = os.path.abspath(path)
        reason = store.snapshot_dir_writable(abspath)
        if reason:
            return {"error": _snapshot_dir_choice_error_message(reason)}
        try:
            builtin = store.builtin_snapshot_dir()
        except OSError:
            builtin = ""
        return {
            "ok": True,
            "path": abspath,
            "snapshot_dir": abspath,
            "snapshot_dir_configured": abspath,
            "snapshot_dir_builtin": builtin,
            "snapshot_dir_is_custom": True,
        }

    def reset_snapshot_dir(self) -> dict:
        """恢复内置默认快照目录。"""
        return self.set_snapshot_dir("")

    def open_snapshot_dir(self) -> dict:
        """在资源管理器中打开当前快照存放目录。"""
        try:
            path = store.default_snapshot_dir()
        except OSError:
            return {"error": i18n.t(
                "这个文件夹无法使用，系统拒绝访问该位置；请另选一个当前运行账户可以写入的文件夹。",
                "This folder cannot be used because the system refused access to that location; choose another folder the current account can write to.",
            )}
        try:
            os.startfile(path)  # noqa: S606 - 打开的是自己管理的目录
        except OSError:
            return {"error": i18n.t(
                "无法打开快照存放目录，请确认该文件夹仍然存在后重试。",
                "Cannot open the snapshot storage folder; check that it still exists and try again.",
            )}
        return {"ok": True}

    def set_theme(self, theme: str) -> dict:
        """前端切换主题：记入 store（可写 YAML）并同步标题栏。

        ``theme`` 可为 ``light``/``dark``/``auto``（auto=跟随系统，
        启动时解析一次）。标题栏需要实际值，auto 会被解析成 dark/light。
        数据文件夹还没确定时主题落不了盘，本次会话仍按所选主题显示。
        """
        try:
            code = store.set_theme(theme)
        except OSError:
            applog.warn("theme setting not saved | reason=storage_unavailable")
            code = store.get_theme()
        resolved = store.resolve_theme(code)
        try:
            self._titlebar.set_theme(resolved)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "theme": code, "resolved": resolved}
