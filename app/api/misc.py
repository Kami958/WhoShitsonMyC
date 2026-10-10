"""应用日志、更新检查、AI 对话导入导出、卸载与收尾（JS-API Mixin）。"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time

import webview  # pywebview

from app import bootstrap
from core import applog, i18n, store
from core.compress import clear_session_cache
from core.differ import Diff
from version import (
    GITHUB_LATEST_API,
    GITHUB_RELEASES_URL,
    __version__ as APP_VERSION,
    compare_versions,
    normalize_version,
)

from app.bootstrap import _window_title


class MiscMixin:
    """不属于单一功能域的杂项方法。"""

    def get_app_log(self, limit: int = 400) -> dict:
        """返回进程内内存日志（旧→新）。不落盘；导出另走 export。"""
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = 400
        cap = int(applog.get_entries_cap())
        n = max(1, min(n, cap))
        return {
            "ok": True,
            "entries": applog.get_entries(n),
            "count": applog.count(),
            "persisted": False,
            "entries_cap": cap,
            "env": applog.get_env_summary(),
            "min_level": applog.get_min_level(),
            "sanitize": applog.get_sanitize_enabled(),
        }

    def clear_app_log(self) -> dict:
        """清空内存日志。"""
        n = applog.clear()
        applog.info("Log cleared by user")
        return {"ok": True, "cleared": n, "count": applog.count()}

    def uninstall_app_data(self, delete_data: bool = True) -> dict:
        """设置页「卸载」：清理应用数据目录（不含用户自定义快照路径）。

        ``delete_data`` 默认 True：删除 ``%LOCALAPPDATA%\\WhoShitsOnMyC`` 下
        全部内容。False 时仅删 settings.yaml。
        **不**删除用户自选的外部快照目录，也**不**删除程序本身。
        """
        try:
            result = store.wipe_app_data(delete_data=bool(delete_data))
        except Exception as exc:  # noqa: BLE001
            applog.exception("uninstall_app_data failed", exc)
            return {"error": i18n.t(
                f"卸载清理失败：{exc}", f"Uninstall cleanup failed: {exc}")}
        if result.get("ok"):
            applog.info(
                f"Uninstall cleanup ok delete_data={bool(delete_data)} "
                f"removed={len(result.get('removed') or [])}"
            )
        else:
            applog.warn(
                f"Uninstall cleanup partial errors={len(result.get('errors') or [])}"
            )
        out = {"ok": bool(result.get("ok")), **result}
        return out

    def quit_app(self) -> dict:
        """卸载完成后关闭窗口并退出进程。"""
        applog.info("quit_app requested")
        win = self._window
        if win is not None:
            try:
                win.destroy()
            except Exception as exc:  # noqa: BLE001
                applog.exception("quit_app window.destroy failed", exc)
                return {"error": str(exc)}
        return {"ok": True}

    def export_app_log(self) -> dict:
        """弹出「另存为」导出日志文本；取消则 cancelled。默认不自动写文件。"""
        if self._window is None:
            return {"error": i18n.t("窗口未就绪", "Window is not ready")}
        save_dialog = getattr(
            getattr(webview, "FileDialog", None), "SAVE", None
        )
        if save_dialog is None:
            save_dialog = getattr(webview, "SAVE_DIALOG", None)
        if save_dialog is None:
            return {"error": i18n.t(
                "当前环境不支持保存对话框",
                "Save dialog is not supported in this environment",
            )}
        stamp = time.strftime("%Y%m%d-%H%M%S")
        default_name = f"WhoShitsOnMyC-log-{stamp}.txt"
        file_types = ("Text files (*.txt)", "All files (*.*)")
        try:
            result = self._window.create_file_dialog(
                save_dialog,
                allow_multiple=False,
                save_filename=default_name,
                file_types=file_types,
            )
        except TypeError:
            try:
                result = self._window.create_file_dialog(
                    save_dialog, False, default_name, file_types
                )
            except Exception as exc:  # noqa: BLE001
                applog.exception("export_app_log dialog failed", exc)
                return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            applog.exception("export_app_log dialog failed", exc)
            return {"error": str(exc)}
        if not result:
            return {"cancelled": True}
        # pywebview 可能返回 str 或 list/tuple
        if isinstance(result, (list, tuple)):
            path = str(result[0]) if result else ""
        else:
            path = str(result)
        if not path:
            return {"cancelled": True}
        if not path.lower().endswith(".txt"):
            path = path + ".txt"
        text = applog.format_export()
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        except OSError as exc:
            applog.exception("export_app_log write failed", exc)
            return {"error": i18n.t(
                f"写入日志失败：{exc}", f"Failed to write log: {exc}")}
        applog.info("Log exported")
        return {"ok": True, "path": path, "bytes": len(text.encode("utf-8"))}

    def export_ai_chat(self, content: str = "") -> dict:
        """弹出「另存为」导出 AI 对话 JSON 文本。"""
        body = content if isinstance(content, str) else str(content or "")
        if not body.strip():
            return {"error": i18n.t("没有可导出的对话", "No conversation to export")}
        if self._window is None:
            return {"error": i18n.t("窗口未就绪", "Window is not ready")}
        save_dialog = getattr(
            getattr(webview, "FileDialog", None), "SAVE", None
        )
        if save_dialog is None:
            save_dialog = getattr(webview, "SAVE_DIALOG", None)
        if save_dialog is None:
            return {"error": i18n.t(
                "当前环境不支持保存对话框",
                "Save dialog is not supported in this environment",
            )}
        stamp = time.strftime("%Y%m%d-%H%M%S")
        default_name = f"WhoShitsOnMyC-ai-chat-{stamp}.json"
        file_types = ("JSON files (*.json)", "All files (*.*)")
        try:
            result = self._window.create_file_dialog(
                save_dialog,
                allow_multiple=False,
                save_filename=default_name,
                file_types=file_types,
            )
        except TypeError:
            try:
                result = self._window.create_file_dialog(
                    save_dialog, False, default_name, file_types
                )
            except Exception as exc:  # noqa: BLE001
                applog.exception("export_ai_chat dialog failed", exc)
                return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            applog.exception("export_ai_chat dialog failed", exc)
            return {"error": str(exc)}
        if not result:
            return {"cancelled": True}
        if isinstance(result, (list, tuple)):
            path = str(result[0]) if result else ""
        else:
            path = str(result)
        if not path:
            return {"cancelled": True}
        if not path.lower().endswith(".json"):
            path = path + ".json"
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
        except OSError as exc:
            applog.exception("export_ai_chat write failed", exc)
            return {"error": i18n.t(
                f"写入对话失败：{exc}", f"Failed to write chat: {exc}")}
        applog.info(f"AI chat exported | bytes={len(body.encode('utf-8'))}")
        return {"ok": True, "path": path, "bytes": len(body.encode("utf-8"))}

    def import_ai_chat(self) -> dict:
        """弹出打开对话框，读取 AI 对话 JSON 文本（最大约 2MB）。"""
        if self._window is None:
            return {"error": i18n.t("窗口未就绪", "Window is not ready")}
        open_dialog = getattr(
            getattr(webview, "FileDialog", None), "OPEN", None
        )
        if open_dialog is None:
            open_dialog = getattr(webview, "OPEN_DIALOG", None)
        if open_dialog is None:
            return {"error": i18n.t(
                "当前环境不支持文件选择对话框",
                "File open dialog is not supported in this environment",
            )}
        file_types = ("JSON files (*.json)", "All files (*.*)")
        try:
            result = self._window.create_file_dialog(
                open_dialog,
                allow_multiple=False,
                file_types=file_types,
            )
        except TypeError:
            try:
                result = self._window.create_file_dialog(
                    open_dialog, False, None, file_types
                )
            except Exception as exc:  # noqa: BLE001
                applog.exception("import_ai_chat dialog failed", exc)
                return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            applog.exception("import_ai_chat dialog failed", exc)
            return {"error": str(exc)}
        if not result:
            return {"cancelled": True}
        if isinstance(result, (list, tuple)):
            path = str(result[0]) if result else ""
        else:
            path = str(result)
        if not path:
            return {"cancelled": True}
        max_bytes = 2 * 1024 * 1024
        try:
            size = os.path.getsize(path)
            if size > max_bytes:
                return {"error": i18n.t(
                    "对话文件过大（上限 2MB）",
                    "Chat file is too large (max 2MB)",
                )}
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as exc:
            applog.exception("import_ai_chat read failed", exc)
            return {"error": i18n.t(
                f"读取对话失败：{exc}", f"Failed to read chat: {exc}")}
        except UnicodeError as exc:
            applog.exception("import_ai_chat decode failed", exc)
            return {"error": i18n.t(
                "对话文件编码无效，请使用 UTF-8",
                "Invalid chat file encoding; use UTF-8",
            )}
        applog.info(f"AI chat import read | bytes={len(text.encode('utf-8'))}")
        return {"ok": True, "path": path, "text": text}

    # ---- 设置 -----------------------------------------------------------

    def check_for_updates(self) -> dict:
        """查询 GitHub 最新 Release，与本机版本比较。

        使用 stdlib，不依赖 httpx（精简版也可用）。
        返回：current / latest / update_available / release_url / html_url 等。
        """
        import urllib.error
        import urllib.request

        current = normalize_version(APP_VERSION) or APP_VERSION
        result: dict = {
            "ok": True,
            "current": current,
            "latest": "",
            "update_available": False,
            "release_url": GITHUB_RELEASES_URL,
            "html_url": GITHUB_RELEASES_URL,
            "name": "",
            "published_at": "",
        }
        req = urllib.request.Request(
            GITHUB_LATEST_API,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"WhoShitsOnMyC/{current}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:  # noqa: S310
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # 404：仓库尚无 release
            if exc.code == 404:
                applog.info("check_for_updates: no releases on GitHub (404)")
                result["error"] = i18n.t(
                    "暂未找到已发布的版本",
                    "No published release found",
                )
                return result
            applog.exception("check_for_updates HTTP error", exc)
            result["ok"] = False
            result["error"] = i18n.t(
                f"检查更新失败（HTTP {exc.code}）",
                f"Update check failed (HTTP {exc.code})",
            )
            return result
        except urllib.error.URLError as exc:
            applog.exception("check_for_updates network error", exc)
            result["ok"] = False
            result["error"] = i18n.t(
                "网络不可用，无法检查更新",
                "Network unavailable; cannot check for updates",
            )
            return result
        except TimeoutError as exc:
            applog.exception("check_for_updates timeout", exc)
            result["ok"] = False
            result["error"] = i18n.t(
                "检查更新超时，请稍后重试",
                "Update check timed out; try again later",
            )
            return result
        except OSError as exc:
            applog.exception("check_for_updates failed", exc)
            result["ok"] = False
            result["error"] = i18n.t(
                f"检查更新失败：{exc}",
                f"Update check failed: {exc}",
            )
            return result

        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            applog.exception("check_for_updates bad JSON", exc)
            result["ok"] = False
            result["error"] = i18n.t(
                "更新信息解析失败",
                "Failed to parse update info",
            )
            return result

        if not isinstance(data, dict):
            result["ok"] = False
            result["error"] = i18n.t(
                "更新信息格式异常",
                "Unexpected update info format",
            )
            return result

        tag = data.get("tag_name") or data.get("name") or ""
        latest = normalize_version(str(tag))
        html_url = data.get("html_url") or GITHUB_RELEASES_URL
        if not isinstance(html_url, str) or not html_url.startswith("http"):
            html_url = GITHUB_RELEASES_URL

        result["latest"] = latest or str(tag).strip()
        result["name"] = str(data.get("name") or "")
        result["published_at"] = str(data.get("published_at") or "")
        result["html_url"] = html_url
        # status: update=有新版 / latest=与发布相同 / ahead=本机高于发布
        cmp = compare_versions(latest, current)
        if cmp > 0:
            status = "update"
        elif cmp < 0:
            status = "ahead"
        else:
            status = "latest"
        result["status"] = status
        result["update_available"] = status == "update"
        applog.info(
            f"check_for_updates | current={current} latest={result['latest']} "
            f"status={status}"
        )
        return result

    def start_auto_update_check(self) -> None:
        """开启「自动检查更新」时，启动后台查询一次最新版本。

        有新版本则置全局标记并在窗口标题栏追加提示；网络失败静默。
        只在本函数启动的线程里执行，不阻塞界面。
        """
        if not store.get_auto_check_updates():
            return

        def _run() -> None:
            try:
                res = self.check_for_updates()
            except Exception as exc:  # noqa: BLE001 - 后台检查失败不影响使用
                applog.exception("auto update check failed", exc)
                return
            if res.get("update_available"):
                bootstrap._update_available = True
                applog.info(
                    f"auto update check | update available: {res.get('latest')}"
                )
                self._refresh_window_title()

        threading.Thread(
            target=_run, name="auto-update-check", daemon=True
        ).start()

    def _apply_icon(self, ico_path: str) -> None:
        """把窗口标题栏/任务栏图标设成指定的 .ico（仅 Windows）。"""
        if os.name != "nt" or not os.path.exists(ico_path):
            return
        hwnd = self._titlebar.hwnd()
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

    def _shutdown(self) -> None:
        """窗口关闭后、解释器退出前的收尾清理。

        顺序关键：先关对比会话释放 sqlite 对临时解压文件的只读句柄，
        再删临时文件；顺序反了会在 Windows 上触发 WinError 32，文件
        删除静默失败并遗留（这正是此前 temp 里 wsmc_*.db 不断堆积的
        根因，atexit 清理时 Diff 连接还开着）。由 :func:`main` 在
        ``webview.start()`` 返回后调用，先于 atexit 执行。
        设置页关闭「临时文件清理」时不删，文件留给下次启动或手动处理。
        """
        cleanup = store.get_temp_cleanup()
        try:
            with self._diff_lock:
                self._close_diff(drop_decompress_cache=cleanup)
        except Exception as exc:  # noqa: BLE001 - 清理失败不阻断退出
            applog.exception("shutdown close diff failed", exc)
        if cleanup:
            try:
                clear_session_cache()
            except Exception as exc:  # noqa: BLE001
                applog.exception("shutdown clear session cache failed", exc)
