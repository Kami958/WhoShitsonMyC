"""快照列举、归纳文件夹、选择对话框、删除与备注（JS-API Mixin）。"""

from __future__ import annotations

import os
import subprocess

import webview  # pywebview

from core import applog, i18n, store, fs_delete
from core.compress import CompressError
from core.snapshot import SnapshotError

from app.bootstrap import _delete_error_message


class SnapshotsMixin:
    """快照文件与归纳文件夹相关方法。"""

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

    def delete_snapshot_folder(
        self,
        name: str,
        force: bool = False,
        permanent: bool = False,
        move_out: bool = False,
    ) -> dict:
        """删除归纳文件夹；默认仅空夹，``force`` 时连同内含快照删除。

        ``permanent`` 仅作用于夹内快照文件：默认进回收站。
        ``move_out`` 时把夹内快照移回快照根（未归类），不删除文件。
        """
        # 若对比会话持有夹内文件，先关
        with self._diff_lock:
            self._close_diff()
        try:
            result = store.delete_snapshot_folder(
                str(name or ""),
                force=bool(force),
                permanent=bool(permanent),
                move_out=bool(move_out),
            )
        except ValueError as exc:
            return {"error": str(exc)}
        except SnapshotError as exc:
            return {"error": str(exc)}
        except fs_delete.DeleteError as exc:
            code = str(exc.message or exc)
            return {
                "error": _delete_error_message(code),
                "code": code,
            }
        except OSError as exc:
            return {"error": i18n.t(
                f"删除文件夹失败：{exc}",
                f"Failed to delete folder: {exc}")}
        return {
            "ok": True,
            "permanent": bool(permanent),
            "count": int(result.get("processed", 0)),
            "renamed": list(result.get("renamed", [])),
        }

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

    def delete_snapshot(self, path: str, permanent: bool = False) -> dict:
        """删除一个快照文件。

        顺序很关键：**先**释放对比会话（它持有该文件的 sqlite 只读连接，
        Windows 上打开的句柄会让删除报 WinError 32），**再**删文件。
        默认进回收站；``permanent=True`` 彻底删除。
        备注写在快照文件内，随文件一起删除。
        """
        with self._diff_lock:
            if self._diff_key and path in self._diff_key:
                self._close_diff()
        try:
            store.delete_snapshot(path, permanent=bool(permanent))
        except fs_delete.DeleteError as exc:
            code = str(exc.message or exc)
            return {
                "error": _delete_error_message(code),
                "code": code,
            }
        except OSError as exc:
            return {"error": i18n.t(
                f"删除失败，文件可能正被其它程序占用：{exc}",
                f"Delete failed; the file may be in use by another program: {exc}",
            )}
        return {
            "ok": True,
            "permanent": bool(permanent),
            "recycled": not bool(permanent),
        }

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

    def set_pinned_snapshots(self, paths: list | None = None) -> dict:
        """整体写回置顶快照列表，返回落盘后的权威值。

        置顶只影响左侧列表在同一文件夹内的排序，不动快照文件本身。
        """
        return {
            "ok": True,
            "pinned_snapshots": store.set_pinned_snapshots(paths or []),
        }

    # ---- 应用日志（内存；默认不落盘） ------------------------------------

    def reveal_path(self, root: str, rel_path: str) -> dict:
        """在资源管理器中定位一个对比节点对应的真实路径。

        目录：直接打开该文件夹（比 /select 更稳，尤其含空格路径）。
        文件：explorer ``/select,`` + 路径分两个参数传入（list 形式，
        空格/单引号由 CreateProcess 原样传递，勿拼成单个带引号字符串）。
        目标已不存在时退而求其次打开父目录。
        """
        full = os.path.join(root, rel_path) if rel_path else root
        full = os.path.normpath(full)
        try:
            if os.path.isdir(full):
                # 对比树多为目录；startfile 走 Shell 关联，空格路径可靠
                os.startfile(full)  # noqa: S606
                return {"ok": True}
            if os.path.isfile(full) or os.path.exists(full):
                # /select, 与路径必须是两个 argv；合成 "/select,path" 或
                # '/select,"path"' 在含空格时会落到「文档」/此电脑。
                # 见 https://stackoverflow.com/questions/281888
                windir = os.environ.get("WINDIR") or r"C:\Windows"
                explorer = os.path.join(windir, "explorer.exe")
                subprocess.Popen(  # noqa: S603
                    [explorer, "/select,", full]
                )
                return {"ok": True}
        except OSError as exc:
            return {
                "error": i18n.t(
                    f"无法打开：{full}\n{exc}",
                    f"Cannot open: {full}\n{exc}",
                )
            }
        parent = os.path.dirname(full)
        if parent and os.path.isdir(parent):
            try:
                os.startfile(parent)  # noqa: S606
            except OSError as exc:
                return {
                    "error": i18n.t(
                        f"无法打开：{parent}\n{exc}",
                        f"Cannot open: {parent}\n{exc}",
                    )
                }
            return {"ok": True, "message": i18n.t(
                "该项已不存在，已打开其所在目录",
                "This item no longer exists; opened its parent folder instead")}
        return {"error": i18n.t(
            f"路径已不存在：{full}", f"Path no longer exists: {full}")}
