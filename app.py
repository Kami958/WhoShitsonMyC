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
import time

import webview  # pywebview

from core.compress import (
    CompressError,
    compress_db,
    drop_cache_for,
    ensure_db_path,
    is_session_cached,
)
from core.differ import Diff, DiffError, SearchCancelled
from core import fs_delete
from core.scanner import ScanCancelled, scan_to_snapshot
from core.snapshot import SnapshotError
from core.timing_probe import start_scan_timer
from core import applog, i18n, store
from titlebar import TitleBarTheme
from version import (
    GITHUB_LATEST_API,
    GITHUB_RELEASES_URL,
    __version__ as APP_VERSION,
    compare_versions,
    normalize_version,
)

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
        # 设置应用 / 快照目录迁移（后台线程，避免阻塞 JS bridge 导致进度画不出来）
        self._settings_thread: threading.Thread | None = None
        # 当前对比会话，按需在多次下钻之间复用（避免每次重开连接）。
        # pywebview 的每次 JS-API 调用可能在不同线程执行，
        # SQLite 连接跨线程复用必须串行化，故加锁。
        self._diff: Diff | None = None
        self._diff_key: tuple[str, str] | None = None
        self._diff_lock = threading.Lock()
        # 搜索是否进行中：cancel_search 只在此期间打断连接，避免误伤其他查询
        self._search_active = False
        # 搜索预热回调代际：换会话后丢弃旧线程的 UI 推送
        self._preheat_token = 0
        # 标题栏主题（Windows 原生暗/浅色）；实现见 titlebar.py。
        self._titlebar = TitleBarTheme(
            get_window=lambda: self._window,
            get_title_candidates=lambda: (_window_title(), "WhoShitsOnMyC"),
        )
        # 构建期可选模块（AI 等）：discover 失败静默；core 零依赖 modules
        self._modules: dict[str, object] = {}
        self._init_modules()
        # 工具侧栏展开时已叠加到窗口宽度上的像素（收起时原样扣回）
        self._tool_panel_boost = 0

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

    # ---- 可选模块（AI 等） -------------------------------------------------

    def _init_modules(self) -> None:
        """发现并实例化构建期可选模块；失败静默。"""
        try:
            from modules import discover
        except ImportError:
            self._modules = {}
            return
        ctx = {
            "emit": self._emit,
            "app_data_dir": store.app_data_dir,
            "t": i18n.t,
            "get_lang": i18n.get_lang,
            # AI 清理 packing：一层子项，与 get_children 同源
            "get_diff_children": self._get_diff_children_for_modules,
        }
        mods: dict[str, object] = {}
        try:
            factories = discover()
        except Exception as exc:  # noqa: BLE001
            applog.exception("module discover failed", exc)
            self._modules = {}
            return
        for name, factory in (factories or {}).items():
            try:
                inst = factory(ctx)
            except Exception as exc:  # noqa: BLE001
                applog.exception(f"module {name} create failed", exc)
                continue
            if inst is not None:
                mods[str(name)] = inst
        self._modules = mods
        if mods:
            applog.info(f"modules loaded: {', '.join(sorted(mods))}")
        else:
            applog.info("modules loaded: (none)")

    def list_modules(self) -> dict:
        """返回可用模块表，如 ``{"ai": True}``。"""
        return {name: True for name in self._modules}

    def module_invoke(
        self, module: str, method: str, kwargs: dict | None = None
    ) -> dict:
        """统一分发到模块实例；只允许 PUBLIC_METHODS 白名单。

        不要给 Api 动态 setattr——pywebview 桥的方法枚举时机不可控。
        """
        name = str(module or "")
        meth = str(method or "")
        inst = self._modules.get(name)
        if inst is None:
            return {
                "error": i18n.t(
                    f"模块不可用：{name or '?'}",
                    f"Module unavailable: {name or '?'}",
                )
            }
        public = getattr(inst, "PUBLIC_METHODS", None)
        if public is None:
            public = frozenset()
        if meth not in public:
            return {
                "error": i18n.t(
                    f"方法不可用：{meth or '?'}",
                    f"Method unavailable: {meth or '?'}",
                )
            }
        fn = getattr(inst, meth, None)
        if not callable(fn):
            return {
                "error": i18n.t(
                    f"方法不可用：{meth or '?'}",
                    f"Method unavailable: {meth or '?'}",
                )
            }
        body = dict(kwargs or {})
        try:
            result = fn(**body)
        except TypeError as exc:
            return {
                "error": i18n.t(
                    f"参数错误：{exc}",
                    f"Invalid arguments: {exc}",
                )
            }
        except Exception as exc:  # noqa: BLE001
            applog.exception(f"module_invoke {name}.{meth} failed", exc)
            return {
                "error": i18n.t(
                    f"调用失败：{exc}",
                    f"Call failed: {exc}",
                )
            }
        if result is None:
            return {"ok": True}
        if isinstance(result, dict):
            return result
        return {"ok": True, "result": result}

    # ---- 快照列举 / 选择目录 -------------------------------------------

    def list_snapshots(self) -> list[dict]:
        """返回默认目录（含一层归纳文件夹）下所有快照的摘要（新→旧）。"""
        return [i.to_dict() for i in store.list_snapshots()]

    def list_snapshot_folders(self) -> list[str]:
        """返回快照根下一层归纳文件夹名（按名称排序）。"""
        return store.list_snapshot_folders()

    def create_snapshot_folder(self, name: str) -> dict:
        """在快照根下新建一层归纳文件夹。"""
        try:
            folder = store.create_snapshot_folder(str(name or ""))
        except ValueError as exc:
            return {"error": i18n.t(
                f"文件夹名称无效：{exc}",
                f"Invalid folder name: {exc}")}
        except OSError as exc:
            return {"error": i18n.t(
                f"创建文件夹失败：{exc}",
                f"Failed to create folder: {exc}")}
        return {"ok": True, "folder": folder}

    def move_snapshot_to_folder(self, path: str, folder: str = "") -> dict:
        """把快照移入归纳文件夹；``folder`` 空串表示移回快照根。"""
        with self._diff_lock:
            if self._diff_key and path in self._diff_key:
                self._close_diff()
        try:
            new_path = store.move_snapshot_to_folder(
                str(path or ""), folder if folder is not None else ""
            )
        except ValueError as exc:
            return {"error": str(exc)}
        except store.StoreError as exc:
            return {"error": str(exc)}
        except SnapshotError as exc:
            return {"error": str(exc)}
        except OSError as exc:
            return {"error": i18n.t(
                f"移动失败：{exc}",
                f"Move failed: {exc}")}
        info = store.snapshot_info(new_path)
        return {
            "ok": True,
            "path": new_path,
            "folder": info.folder or "",
        }

    def rename_snapshot_folder(self, old_name: str, new_name: str) -> dict:
        """重命名快照根下的一层归纳文件夹。"""
        try:
            name = store.rename_snapshot_folder(
                str(old_name or ""), str(new_name or "")
            )
        except ValueError as exc:
            return {"error": i18n.t(
                f"文件夹名称无效：{exc}",
                f"Invalid folder name: {exc}")}
        except SnapshotError as exc:
            return {"error": str(exc)}
        except OSError as exc:
            return {"error": i18n.t(
                f"重命名失败：{exc}",
                f"Rename failed: {exc}")}
        return {"ok": True, "folder": name}

    def delete_snapshot_folder(self, name: str, force: bool = False) -> dict:
        """删除归纳文件夹；默认仅空夹，``force`` 时连同内含快照删除。"""
        # 若对比会话持有夹内文件，先关
        with self._diff_lock:
            self._close_diff()
        try:
            store.delete_snapshot_folder(
                str(name or ""), force=bool(force)
            )
        except ValueError as exc:
            return {"error": str(exc)}
        except SnapshotError as exc:
            return {"error": str(exc)}
        except OSError as exc:
            return {"error": i18n.t(
                f"删除文件夹失败：{exc}",
                f"Failed to delete folder: {exc}")}
        return {"ok": True}

    def read_snapshot_infos(self, paths: list | None = None) -> dict:
        """读取任意路径上的快照摘要（用于「从其它位置导入」）。

        Returns:
            ``{"items": [SnapshotInfo dict...], "errors": [{"path", "error"}]}``
        """
        items: list[dict] = []
        errors: list[dict] = []
        for raw in paths or []:
            if not isinstance(raw, str) or not raw.strip():
                continue
            path = os.path.abspath(raw.strip())
            try:
                items.append(store.snapshot_info(path).to_dict())
            except SnapshotError as exc:
                errors.append({"path": path, "error": str(exc)})
            except OSError as exc:
                errors.append({"path": path, "error": str(exc)})
        return {"items": items, "errors": errors}

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

    def choose_snapshot_files(self) -> dict:
        """弹出原生「打开文件」对话框，多选 ``.db`` / ``.dbz``。

        Returns:
            ``{"paths": [...]}``；取消则为空列表。
        """
        if self._window is None:
            return {"paths": []}
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
        file_types = (
            "Snapshot files (*.db;*.dbz)",
            "All files (*.*)",
        )
        try:
            result = self._window.create_file_dialog(
                open_dialog,
                allow_multiple=True,
                file_types=file_types,
            )
        except TypeError:
            # 旧版 pywebview 参数名可能不同
            try:
                result = self._window.create_file_dialog(
                    open_dialog, True, None, file_types
                )
            except Exception as exc:  # noqa: BLE001
                return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
        if not result:
            return {"paths": []}
        # result 可能是 tuple/list
        paths = [str(p) for p in result if p]
        return {"paths": paths}

    def delete_snapshot(self, path: str) -> dict:
        """删除一个快照文件。

        顺序很关键：**先**释放对比会话（它持有该文件的 sqlite 只读连接，
        Windows 上打开的句柄会让删除报 WinError 32），**再**删文件。
        备注写在快照文件内，随文件一起删除。
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

    def set_snapshot_note(self, path: str, note: str) -> dict:
        """把备注写入快照文件本身（``.db`` meta / ``.dbz`` meta.json）。"""
        try:
            text = store.set_note(str(path or ""), note if note is not None else "")
        except ValueError as exc:
            return {"error": str(exc)}
        except (OSError, SnapshotError, CompressError) as exc:
            return {"error": i18n.t(
                f"保存备注失败：{exc}", f"Failed to save note: {exc}")}
        return {"ok": True, "note": text, "path": os.path.abspath(str(path or ""))}

    # ---- 应用日志（内存；默认不落盘） ------------------------------------

    def get_app_log(self, limit: int = 400) -> dict:
        """返回进程内日志（旧→新）。仅内存缓冲，默认不落盘。"""
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = 400
        n = max(1, min(n, 1024))
        return {
            "ok": True,
            "entries": applog.get_entries(n),
            "count": applog.count(),
            "persisted": False,
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
            self._titlebar.set_theme(store.get_theme())
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

        # 日志脱敏：设置项已写入则同步 applog（设置优先于环境变量）
        _sync_log_sanitize_to_applog()
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

    def set_theme(self, theme: str) -> dict:
        """前端切换主题：记入 store（可写 YAML）并同步标题栏。"""
        code = store.set_theme(theme)
        # 标题栏：dark/light
        try:
            self._titlebar.set_theme(code)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "theme": code}

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

    def check_pending_paths(self, items: list | None = None) -> dict:
        """批量评估「加入待删除」候选（不删文件、不入队）。

        供 AI 提议 / 人工确认前过滤：删除白名单、扫描根、盘符根、范围外。
        默认不因路径当前不存在而拒绝（真删时再校验）。

        每项 ``{root, rel|rel_path, name?, is_dir?}``；返回
        ``{ok, allowed: [...], rejected: [...]}``。
        """
        bl = store.get_delete_blacklist()
        raw = items if isinstance(items, list) else []
        allowed: list[dict] = []
        rejected: list[dict] = []
        # 防止一次塞爆
        cap = 200
        for i, item in enumerate(raw[:cap]):
            if not isinstance(item, dict):
                rejected.append(
                    {
                        "index": i,
                        "code": "invalid",
                        "error": _delete_error_message("invalid"),
                    }
                )
                continue
            root = str(item.get("root") or "").strip()
            rel = str(item.get("rel_path") or item.get("rel") or "").strip()
            name = str(item.get("name") or "").strip()
            is_dir = item.get("is_dir")
            ev = fs_delete.evaluate_pending_candidate(
                root, rel, bl, require_exists=False
            )
            row = {
                "index": i,
                "root": ev.get("root") or root,
                "rel": ev.get("rel") if ev.get("rel") is not None else rel,
                "path": ev.get("path") or "",
                "name": name or (ev.get("path") or rel or root),
                "is_dir": bool(is_dir) if is_dir is not None else False,
                "code": ev.get("code") or "invalid",
            }
            if item.get("reason") is not None:
                row["reason"] = str(item.get("reason") or "")
            if ev.get("ok"):
                row["ok"] = True
                if "exists" in ev:
                    row["exists"] = bool(ev.get("exists"))
                allowed.append(row)
            else:
                code = str(ev.get("code") or "invalid")
                row["ok"] = False
                row["error"] = _delete_error_message(code, root=root, rel_path=rel)
                rejected.append(row)
        return {
            "ok": True,
            "allowed": allowed,
            "rejected": rejected,
            "truncated": False if len(raw) <= cap else True,
        }

    def delete_path(
        self,
        root: str,
        rel_path: str = "",
        permanent: bool = False,
    ) -> dict:
        """删除对比树节点对应的真实路径。

        默认进回收站；``permanent=True`` 为永久删除。
        会校验：位于对比根下、非根/盘符根、不在黑名单、路径存在。
        """
        bl = store.get_delete_blacklist()
        try:
            full = fs_delete.assert_deletable(root, rel_path, bl)
            fs_delete.delete_path(full, permanent=bool(permanent))
        except fs_delete.DeleteError as exc:
            code = str(exc.message or exc)
            msg = _delete_error_message(code, root=root, rel_path=rel_path)
            applog.info(f"delete_path denied/failed | code={code} permanent={bool(permanent)}")
            # code 给前端分类标注（missing / blacklist / …）；文案仍走 error
            return {"error": msg, "code": code}
        except OSError as exc:
            applog.exception("delete_path OSError", exc)
            return {
                "error": i18n.t(f"删除失败：{exc}", f"Delete failed: {exc}"),
                "code": "os",
            }
        except Exception as exc:  # noqa: BLE001
            applog.exception("delete_path failed", exc)
            return {
                "error": i18n.t(f"删除失败：{exc}", f"Delete failed: {exc}"),
                "code": "fail",
            }
        applog.info(f"delete_path ok | permanent={bool(permanent)}")
        return {
            "ok": True,
            "path": full,
            "permanent": bool(permanent),
            "recycled": not bool(permanent),
        }

    def set_tool_panel_open(self, open: bool = False, width: int = 340) -> dict:
        """侧栏展开/收起时增减窗口宽度，避免主内容区被挤窄。

        最大化时不改尺寸。收起时只扣回本接口先前叠加的像素，
        用户在展开期间手动缩放窗口仍按叠加量还原。
        """
        try:
            panel_w = max(0, int(width or 0))
        except (TypeError, ValueError):
            panel_w = 340
        desired = panel_w if open else 0
        delta = desired - int(self._tool_panel_boost or 0)
        if delta == 0:
            return {
                "ok": True,
                "boost": int(self._tool_panel_boost or 0),
                "skipped": "noop",
            }
        win = self._window
        if win is None:
            return {"error": i18n.t("窗口未就绪", "Window is not ready")}
        if _window_is_maximized(win):
            # 最大化时不改尺寸；展开请求不记 boost，避免之后还原时误缩
            if not open:
                self._tool_panel_boost = 0
            return {
                "ok": True,
                "boost": int(self._tool_panel_boost or 0),
                "skipped": "maximized",
            }
        try:
            cur_w = int(win.width)
            cur_h = int(win.height)
            cur_x = int(win.x)
            cur_y = int(win.y)
        except Exception as exc:  # noqa: BLE001
            applog.exception("set_tool_panel_open get size failed", exc)
            return {"error": str(exc)}

        min_w, min_h = 820, 560
        new_w = max(min_w, cur_w + delta)
        # 实际可应用的增量（可能因 min_size 被截断）
        applied = new_w - cur_w
        if applied == 0 and delta < 0:
            # 已到最小宽，仍记为收起完成
            self._tool_panel_boost = desired
            return {"ok": True, "boost": desired, "width": cur_w}

        new_x = cur_x
        work = _primary_work_area()
        if work is not None:
            wx, wy, ww, wh = work
            # 右侧放不下时，整体左移，尽量保持完整可见
            right = new_x + new_w
            max_right = wx + ww
            if right > max_right:
                new_x = max(wx, max_right - new_w)
            if new_x < wx:
                new_x = wx
            # 仍超出工作区则压到可用宽度
            if new_w > ww:
                new_w = max(min_w, ww)
                applied = new_w - cur_w
                new_x = wx

        try:
            if new_x != cur_x:
                win.move(new_x, cur_y)
            win.resize(new_w, max(min_h, cur_h))
        except Exception as exc:  # noqa: BLE001
            applog.exception("set_tool_panel_open resize failed", exc)
            return {"error": str(exc)}

        if open:
            self._tool_panel_boost = max(0, int(self._tool_panel_boost or 0) + applied)
        else:
            self._tool_panel_boost = 0
        return {
            "ok": True,
            "boost": int(self._tool_panel_boost or 0),
            "width": new_w,
            "x": new_x,
        }

    # ---- 扫描（后台线程 + 进度推送）------------------------------------

    def start_scan(self, root: str, follow_symlinks: bool = False) -> dict:
        """在后台线程扫描 ``root``，进度与结果通过事件推送。

        事件：``scan-progress`` {files, current} /
        ``scan-done`` {snapshot, elapsed_s} /
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
        # 不记录 root 路径（隐私）；只记并发度等元数据。
        # workers 仅表示常规目录扫描线程数；MFT 解析进程数另按核/活量自推导。
        applog.info(
            f"Scan starting workers={store.get_scan_workers()} "
            f"compress={store.get_compress_snapshots()} "
            f"mft={store.get_use_mft()} follow_symlinks={bool(follow_symlinks)}"
        )
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
        workers = store.get_scan_workers()
        do_compress = store.get_compress_snapshots()
        # 整次扫描墙钟（含可选压缩），用于完成提示「用时」
        t_wall0 = time.perf_counter()
        # 分段计时：exe / 未开 DEBUG·WSMC_SCAN_TIMING 时为空操作；汇总进 applog。
        timer = start_scan_timer(
            root=root, workers=workers, compress_enabled=do_compress
        )

        # 扫描线程内再兜底节流一层（scanner 已节流；压缩阶段等直接 emit 不受影响）
        _prog_lock = threading.Lock()
        _last_emit = [0.0]
        _PROG_GAP = 0.18

        def on_progress(files: int, current: str) -> None:
            now = time.perf_counter()
            with _prog_lock:
                if now - _last_emit[0] < _PROG_GAP:
                    return
                _last_emit[0] = now
            self._emit(
                "scan-progress",
                {"files": int(files), "current": current or ""},
            )

        try:
            timer.span_start("scan_to_snapshot")
            try:
                meta = scan_to_snapshot(
                    root,
                    db_path,
                    follow_symlinks=follow_symlinks,
                    progress=on_progress,
                    cancel=self._cancel.is_set,
                    workers=workers,
                    timer=timer,
                )
            finally:
                timer.span_end("scan_to_snapshot")
            try:
                timer.set_meta(db_bytes=os.path.getsize(db_path))
            except OSError:
                pass
            # 扫完可选压缩：失败时保留 .db，不把整次扫描判失败。
            if do_compress:
                self._emit("scan-progress", {
                    "files": meta.file_count,
                    "current": i18n.t("正在压缩快照", "Compressing snapshot"),
                })
                try:
                    timer.span_start("compress")
                    try:
                        final_path = compress_db(db_path, meta)
                    finally:
                        timer.span_end("compress")
                    try:
                        if final_path.lower().endswith(".dbz"):
                            timer.set_meta(dbz_bytes=os.path.getsize(final_path))
                    except OSError:
                        pass
                except CompressError as exc:
                    applog.exception(
                        f"Snapshot compress failed (file_count={meta.file_count})",
                        exc,
                    )
                    timer.finish(status="compress_failed")
                    elapsed_s = max(0.0, time.perf_counter() - t_wall0)
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
                            "elapsed_s": round(elapsed_s, 2),
                            "warning": i18n.t(
                                f"快照已保存，但压缩失败，已保留未压缩文件：{exc}",
                                f"Snapshot saved, but compression failed; kept uncompressed file: {exc}",
                            ),
                        },
                    )
                    return
        except ScanCancelled:
            store.delete_snapshot(db_path)  # 丢弃不完整快照
            timer.finish(status="cancelled")
            applog.info("Scan cancelled")
            self._emit("scan-cancelled", {})
        except Exception as exc:  # noqa: BLE001 - 兜底，任何异常都不该让线程静默死掉
            store.delete_snapshot(db_path)
            # 不写扫描 root / 当前路径，避免隐私与日志膨胀
            applog.exception("Scan failed", exc)
            timer.finish(status="error")
            self._emit("scan-error", {"message": str(exc)})
        else:
            timer.finish(status="ok")
            elapsed_s = max(0.0, time.perf_counter() - t_wall0)
            applog.info(
                f"Scan done file_count={meta.file_count} "
                f"total_size={meta.total_size} "
                f"skipped={len(meta.skipped)} "
                f"compressed={str(final_path).lower().endswith('.dbz')} "
                f"elapsed_s={elapsed_s:.2f}"
            )
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
                    },
                    "elapsed_s": round(elapsed_s, 2),
                },
            )

    # ---- 对比 / 下钻 ---------------------------------------------------

    def compare_cache_status(self, old_path: str = "", new_path: str = "") -> dict:
        """对比前查询：两侧是否还需解压 ``.dbz``。

        供前端显示「正在解压」还是「对比中」，以进程内会话缓存为准，
        避免同快照再次对比时误提示解压。
        """
        old = str(old_path or "")
        new = str(new_path or "")
        # 同一对已打开的 Diff 会话：连连接都在，更不需要再解压
        key = (old, new) if old and new else None
        if (
            key is not None
            and self._diff is not None
            and self._diff_key == key
        ):
            return {
                "ok": True,
                "need_decompress": False,
                "old_cached": True,
                "new_cached": True,
                "session_ready": True,
            }
        old_cached = is_session_cached(old) if old else True
        new_cached = is_session_cached(new) if new else True
        return {
            "ok": True,
            "need_decompress": not (old_cached and new_cached),
            "old_cached": old_cached,
            "new_cached": new_cached,
            "session_ready": False,
        }

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
            applog.warn(f"Compare rejected: {exc}")
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - 任何异常都要回 JSON，不能让前端悬死
            applog.exception("Compare failed", exc)
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
            applog.warn(f"get_children rejected: {exc}")
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - 同上，兜底成 error 响应
            applog.exception("get_children failed", exc)
            return {"error": i18n.t(
                f"读取子目录失败：{exc}", f"Failed to read subfolder: {exc}")}

    def _get_diff_children_for_modules(
        self, old_path: str, new_path: str, parent: str
    ) -> list:
        """给 AI packing 注入：返回子节点 dict 列表（失败返回 []，不抛）。"""
        try:
            with self._diff_lock:
                self._ensure_diff(old_path, new_path)
                assert self._diff is not None
                nodes = self._diff.compare_children(parent or "")
                out: list = []
                for n in nodes or []:
                    if hasattr(n, "to_dict"):
                        out.append(n.to_dict())
                    elif isinstance(n, dict):
                        out.append(n)
                return out
        except (DiffError, SnapshotError, CompressError) as exc:
            applog.warn(f"module get_diff_children rejected: {exc}")
            return []
        except Exception as exc:  # noqa: BLE001
            applog.exception("module get_diff_children failed", exc)
            return []

    def search_diff(
        self,
        old_path: str,
        new_path: str,
        query: str,
        limit: int = 50,
        offset: int = 0,
        sort: str = "delta-desc",
        case_sensitive: bool = False,
        exact: bool = False,
    ) -> dict:
        """在当前对比会话中按名称/路径搜索，返回匹配的对比节点（可分页）。"""
        try:
            t0 = time.perf_counter()
            with self._diff_lock:
                self._ensure_diff(old_path, new_path)
                assert self._diff is not None
                self._search_active = True
                try:
                    nodes, total = self._diff.search_by_name(
                        query,
                        limit=int(limit or 50),
                        offset=int(offset or 0),
                        sort=str(sort or "delta-desc"),
                        case_sensitive=bool(case_sensitive),
                        exact=bool(exact),
                    )
                finally:
                    self._search_active = False
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            return {
                "nodes": [n.to_dict() for n in nodes],
                "total": total,
                "query": (query or "").strip(),
                "limit": int(limit or 50),
                "offset": int(offset or 0),
                "sort": str(sort or "delta-desc"),
                "case_sensitive": bool(case_sensitive),
                "exact": bool(exact),
                "elapsed_ms": elapsed_ms,
            }
        except SearchCancelled:
            applog.info("search_diff cancelled by user")
            return {"cancelled": True}
        except (DiffError, SnapshotError, CompressError) as exc:
            applog.warn(f"search_diff rejected: {exc}")
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            applog.exception("search_diff failed", exc)
            return {"error": i18n.t(
                f"搜索失败：{exc}", f"Search failed: {exc}")}

    def cancel_search(self) -> dict:
        """强行中断正在进行的搜索，尽快释放 CPU 与对比会话锁。

        不获取 ``_diff_lock``（搜索线程正持有它），直接置取消标记并用
        SQLite ``interrupt()`` 打断执行中的查询；空闲时调用是无害的空操作。
        """
        diff = self._diff
        if self._search_active and diff is not None:
            try:
                diff.cancel_search()
            except Exception:  # noqa: BLE001 - 会话恰好在关闭等边界
                pass
        return {"ok": True}

    def close_diff_session(self) -> dict:
        """关闭当前对比会话：释放搜索索引、SQLite 连接，并丢弃 .dbz 解压临时文件。

        前端清空对比结果时调用；空闲时是无害空操作。
        """
        try:
            with self._diff_lock:
                self._close_diff(drop_decompress_cache=True)
        except Exception:  # noqa: BLE001 - 关闭边界容错
            pass
        return {"ok": True}

    def start_search_preheat(self, old_path: str, new_path: str) -> dict:
        """打开搜索框时触发：按设置决定是否预热内存索引。

        关闭设置时直接返回 skipped；已就绪则补推 ready。
        """
        if not store.get_search_memory_index():
            return {"ok": True, "status": "skipped", "enabled": False}
        try:
            with self._diff_lock:
                self._ensure_diff(old_path, new_path)
                assert self._diff is not None
                st = self._diff.search_preheat_status()
                if st == "ready":
                    self._emit("search-preheat", {"status": "ready"})
                    return {"ok": True, "status": "ready", "enabled": True}
                if st == "started":
                    self._emit("search-preheat", {"status": "started"})
                    return {"ok": True, "status": "started", "enabled": True}
                # 尚未开始：挂回调并启动
                self._preheat_token += 1
                token = self._preheat_token

                def on_preheat_status(payload: dict) -> None:
                    if token != self._preheat_token:
                        return
                    data = dict(payload or {})
                    data.setdefault("status", "")
                    self._emit("search-preheat", data)

                self._diff.start_search_preheat(on_status=on_preheat_status)
                return {"ok": True, "status": "started", "enabled": True}
        except (DiffError, SnapshotError, CompressError) as exc:
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            applog.exception("start_search_preheat failed", exc)
            return {"error": i18n.t(
                f"搜索索引准备失败：{exc}",
                f"Failed to prepare search index: {exc}",
            )}

    def search_preheat_status(self, old_path: str = "", new_path: str = "") -> dict:
        """查询当前会话的搜索预热状态（供前端轮询兜底）。"""
        enabled = store.get_search_memory_index()
        if not enabled:
            return {"ok": True, "status": "skipped", "enabled": False}
        diff = self._diff
        key = (old_path, new_path) if old_path and new_path else None
        if diff is None or (key is not None and self._diff_key != key):
            return {"ok": True, "status": "idle", "enabled": True}
        return {
            "ok": True,
            "status": diff.search_preheat_status(),
            "enabled": True,
        }

    def _ensure_diff(self, old_path: str, new_path: str) -> None:
        """确保当前 :class:`Diff` 会话对应给定的两份快照，否则重开。

        压缩快照（``.dbz``）在这里才解压到临时文件；列表/选择阶段不解压。
        内存搜索索引不在对比时预热，而在打开搜索框时由
        :meth:`start_search_preheat` 触发。

        换一对快照或交换基准/当前时：只丢「新会话不再用到」的解压缓存，
        共用的 ``.dbz`` 临时文件继续复用，避免同快照反复解压。
        """
        key = (old_path, new_path)
        if self._diff is not None and self._diff_key == key:
            return
        prev_key = self._diff_key
        # 先关连接，暂不清解压缓存；下面只丢不再需要的路径
        self._close_diff(drop_decompress_cache=False)
        if prev_key:
            keep = {
                os.path.abspath(old_path or ""),
                os.path.abspath(new_path or ""),
            }
            for path in prev_key:
                try:
                    if path and os.path.abspath(path) not in keep:
                        drop_cache_for(path)
                except Exception:  # noqa: BLE001
                    pass
        try:
            old_db = ensure_db_path(old_path)
            new_db = ensure_db_path(new_path)
        except (CompressError, SnapshotError):
            raise
        self._diff = Diff(old_db, new_db)
        self._diff_key = key

    def _close_diff(self, *, drop_decompress_cache: bool = False) -> None:
        """关闭 Diff 会话。

        ``drop_decompress_cache=True`` 时，顺带删除本会话用过的 ``.dbz``
        解压临时文件（``core.compress`` 进程内登记）。仅在用户清空对比等
        「整段会话结束」时开启；换一对快照时由 :meth:`_ensure_diff` 按需丢弃。
        """
        # 作废旧预热推送，避免关会话后还刷「已就绪」
        self._preheat_token += 1
        key = self._diff_key
        if self._diff is not None:
            self._diff.close()
        self._diff = None
        self._diff_key = None
        if drop_decompress_cache and key:
            for path in key:
                try:
                    drop_cache_for(path)
                except Exception:  # noqa: BLE001 - 清临时文件失败不影响关会话
                    pass

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


def _delete_error_message(code: str, *, root: str = "", rel_path: str = "") -> str:
    """把 fs_delete.DeleteError 机器码映射为中英文案。"""
    c = (code or "").strip()
    if c == "root":
        return i18n.t("无法删除扫描根目录", "Cannot delete the scan root")
    if c == "drive_root":
        return i18n.t("无法删除磁盘根目录", "Cannot delete a drive root")
    if c == "outside":
        return i18n.t("路径不在对比范围内", "Path is outside the compare root")
    if c == "blacklist":
        return i18n.t("该路径在删除白名单中", "This path is on the delete whitelist")
    if c == "missing":
        return i18n.t("路径已不存在", "Path no longer exists")
    if c == "recycle_unsupported":
        return i18n.t(
            "当前系统不支持移到回收站",
            "Recycle Bin is not supported on this system",
        )
    if c == "invalid":
        return i18n.t("无效路径", "Invalid path")
    if c.startswith("recycle:"):
        return i18n.t(
            "移到回收站失败，未执行永久删除",
            "Failed to move to Recycle Bin; permanent delete was not performed",
        )
    if c.startswith("os:"):
        detail = c[3:] or c
        return i18n.t(f"删除失败：{detail}", f"Delete failed: {detail}")
    return i18n.t(f"删除失败：{c}", f"Delete failed: {c}")


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
    return f"WhoShitsOnMyC — {mode}"


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
        applog.warn(f"WebView2 missing (MessageBox failed): {msg}")
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


def _sync_log_sanitize_to_applog() -> bool:
    """把路径脱敏开关同步到 applog。

    优先级：设置项显式值（yaml / 设置页）> 环境变量 ``WSMC_LOG_SANITIZE`` > 默认开。
    返回生效后的开关值。
    """
    if store.is_log_sanitize_explicit():
        enabled = store.get_log_sanitize()
    else:
        env = applog.env_log_sanitize()
        if env is None:
            enabled = store.get_log_sanitize()  # 默认 True
        else:
            enabled = env
            # 仅会话生效，不写 yaml；与 store 内存对齐便于 get_settings 展示
            store._log_sanitize = enabled  # noqa: SLF001
    return applog.set_sanitize_enabled(enabled)


def _settings_field_log_value(key: str, data: dict) -> str:
    """通用设置字段的日志可读值。

    路径保持原文，是否脱敏由 ``applog.log_settings_changed`` 写入管线决定。
    """
    if key == "delete_blacklist":
        bl = data.get("delete_blacklist") or []
        n = len(bl) if isinstance(bl, list) else 0
        return f"{n} rules"
    if key in (
        "compress_snapshots",
        "use_mft",
        "search_memory_index",
        "log_sanitize",
        "snapshot_dir_is_custom",
    ):
        return "true" if bool(data.get(key)) else "false"
    if key == "scan_workers":
        return str(int(data.get(key) or 0))
    if key in ("snapshot_dir", "snapshot_dir_configured"):
        raw = str(data.get(key) or "").strip()
        if not raw:
            return "(builtin)" if key == "snapshot_dir_configured" else "-"
        return raw
    val = data.get(key)
    if val is None or val == "":
        return "-"
    return str(val)


def _diff_common_settings(before: dict, after: dict) -> list[str]:
    """对比通用设置，只返回变更字段的「名: 旧 -> 新」。

    不含 AI 节（AI 由 set_config 单独记 diff）。
    """
    keys = (
        "scan_workers",
        "compress_snapshots",
        "use_mft",
        "search_memory_index",
        "log_sanitize",
        "snapshot_dir_configured",
        "snapshot_dir",
        "delete_blacklist",
    )
    parts: list[str] = []
    for key in keys:
        if key == "delete_blacklist":
            b = before.get("delete_blacklist") or []
            a = after.get("delete_blacklist") or []
            if not isinstance(b, list):
                b = []
            if not isinstance(a, list):
                a = []
            # 规范化比较：path+mode
            def _bl_key(items: list) -> list[tuple[str, str]]:
                out: list[tuple[str, str]] = []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    out.append(
                        (
                            str(it.get("path") or "").strip().lower(),
                            str(it.get("mode") or "prefix").strip().lower(),
                        )
                    )
                out.sort()
                return out

            if _bl_key(b) == _bl_key(a):
                continue
        elif key in (
            "compress_snapshots",
            "use_mft",
            "search_memory_index",
            "log_sanitize",
        ):
            if bool(before.get(key)) == bool(after.get(key)):
                continue
        elif key == "scan_workers":
            if int(before.get(key) or 0) == int(after.get(key) or 0):
                continue
        elif key in ("snapshot_dir", "snapshot_dir_configured"):
            b = str(before.get(key) or "").strip().rstrip("\\/").lower()
            a = str(after.get(key) or "").strip().rstrip("\\/").lower()
            if b == a:
                continue
        else:
            if before.get(key) == after.get(key):
                continue
        parts.append(
            f"{key}: {_settings_field_log_value(key, before)}"
            f" -> {_settings_field_log_value(key, after)}"
        )
    return parts


def main() -> None:
    """创建窗口并启动应用。"""
    # 语言 / 主题：settings.yaml 显式写入则用它；语言缺省用系统语言。
    # 标题栏默认跟 store 主题，避免先按 dark 刷再被前端纠正。
    # 脱敏开关须在 note_startup 之前生效，启动日志才反映真实状态。
    _sync_log_sanitize_to_applog()
    applog.note_startup(APP_VERSION)
    # 语言在 common.lang（旧扁平顶层 lang 亦经 _common_view 合并），
    # 不能写 ``"lang" in _disk``——分节 YAML 顶层只有 common/ai，会误判成「无语言」
    # 从而用系统 UI 语言覆盖配置文件。
    _disk = store._load_settings_yaml(store.settings_path())  # noqa: SLF001
    _common = store._common_view(_disk) if isinstance(_disk, dict) else {}  # noqa: SLF001
    if "lang" in _common:
        # import store 时已 apply；此处只同步运行时 i18n
        i18n.set_lang(store.get_lang())
    else:
        detected = _detect_lang()
        store._lang = detected  # noqa: SLF001 - 启动初始化，不触发 persist
        i18n.set_lang(detected)
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
        background_color=(
            "#ffffff" if store.get_theme() == "light" else "#0f1116"
        ),
    )
    api.set_window(window)
    # 窗口一出现：hook 标题栏 + 图标；主题跟 store（YAML 已加载）走。
    # 前端 set_theme 再对齐一次；启动阶段勿用默认 dark 覆盖用户 light。
    try:
        api._titlebar.dark = store.get_theme() != "light"
    except Exception:  # noqa: BLE001
        pass

    def _on_shown() -> None:
        api._refresh_window_title()
        api._titlebar.hook()
        api._apply_icon(_ICON_PATH)
        # 不 force_nudge：避免启动瞬间抖窗口；后续 set_theme / 延迟刷新补上。
        api._titlebar.apply(api._titlebar.dark, force_nudge=False)

    try:
        window.events.shown += _on_shown
    except Exception:  # noqa: BLE001
        pass
    try:
        window.events.restored += lambda: api._titlebar.apply(
            api._titlebar.dark, force_nudge=False
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        # 页面就绪后再钉一次（前端多半已 set_theme）；只调度少量延迟刷新。
        def _on_loaded() -> None:
            api._titlebar.hook()
            api._titlebar.apply(api._titlebar.dark, force_nudge=False)
            api._titlebar.schedule_refresh(delays_ms=(120,))

        window.events.loaded += _on_loaded
    except Exception:  # noqa: BLE001
        pass
    start_kwargs = {}
    if os.path.exists(_ICON_PATH):
        start_kwargs["icon"] = _ICON_PATH  # 任务栏/GUI 图标
    # debug=True：WebView2 可 F12 / 右键检查；仅开发排查时开
    if _wants_devtools():
        _prepare_devtools_without_http_spam()
        start_kwargs["debug"] = True
        applog.info("DevTools enabled (--devtools / WSMC_DEVTOOLS)")
    try:
        webview.start(**start_kwargs)
    except TypeError:
        # 个别后端不接受 icon/debug 组合，逐步降级
        start_kwargs.pop("icon", None)
        try:
            webview.start(**start_kwargs)
        except TypeError:
            webview.start()


if __name__ == "__main__":
    # Windows + PyInstaller：多进程 spawn 子进程会重新执行本模块入口，
    # freeze_support 让子进程走 worker 路径而非再起 GUI。
    import multiprocessing

    multiprocessing.freeze_support()
    main()
