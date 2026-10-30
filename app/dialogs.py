"""原生文件与文件夹对话框的统一入口。

所有对话框都从这里调用，并且每次都传入一个真实存在且可访问的初始目录。
pywebview 的 WinForms 后端在初始目录为空时会直接读取 HOMEPATH，环境变量缺失
就抛 KeyError；传入有效目录可以绕开这条分支。对话框自身的异常也在这里收口，
不让原始异常穿过前端桥接。
"""

from __future__ import annotations

import os
from typing import Any

from core import applog, store


class DialogError(Exception):
    """对话框无法打开；``code`` 供调用方选择固定文案，不直接展示给用户。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _usable_dir(path: str | None) -> str:
    """路径是真实存在且可访问的目录时返回绝对路径，否则返回空串。"""
    if not path:
        return ""
    try:
        candidate = os.path.abspath(os.path.expanduser(str(path).strip()))
    except (OSError, ValueError, TypeError):
        return ""
    if not candidate or not os.path.isdir(candidate):
        return ""
    try:
        if not os.access(candidate, os.R_OK | os.X_OK):
            return ""
        with os.scandir(candidate) as entries:
            next(entries, None)
    except OSError:
        return ""
    return candidate


def _current_drive_root() -> str:
    """当前工作目录所在盘的根目录；取不到时返回空串。"""
    try:
        cwd = os.getcwd()
    except OSError:
        return ""
    try:
        drive, _ = os.path.splitdrive(cwd)
    except (OSError, ValueError):
        drive = ""
    if drive:
        return _usable_dir(drive + os.sep)
    return _usable_dir(os.path.abspath(os.sep))


def pick_initial_dir(preferred_dir: str = "") -> str:
    """按优先级选一个可用的初始目录，全部不可用时返回空串。

    顺序为调用方偏好、当前运行账户能访问的位置、当前快照目录、当前盘符
    根目录。其他账户的主目录不会作为候选，快照目录也要先确认当前账户
    能访问。
    """
    candidates: list[str] = [preferred_dir]
    try:
        candidates.append(store.preferred_pick_dir())
    except Exception:  # noqa: BLE001 - 身份解析失败时继续尝试后续候选
        pass
    try:
        candidates.append(store.get_snapshot_dir())
    except Exception:  # noqa: BLE001 - 快照目录不可用时继续尝试后续候选
        pass
    candidates.append(_current_drive_root())

    seen: set[str] = set()
    for raw in candidates:
        path = _usable_dir(raw)
        if not path:
            continue
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        return path
    return ""


def _dialog_type(kind: str) -> Any:
    """把 ``folder`` / ``open`` / ``save`` 转成当前 pywebview 的对话框枚举。"""
    import webview

    attr_names = {
        "folder": ("FOLDER", "FOLDER_DIALOG"),
        "open": ("OPEN", "OPEN_DIALOG"),
        "save": ("SAVE", "SAVE_DIALOG"),
    }.get(str(kind or "").lower())
    if not attr_names:
        raise DialogError("unsupported")
    file_dialog = getattr(webview, "FileDialog", None)
    for name in attr_names:
        value = getattr(file_dialog, name, None)
        if value is not None:
            return value
    for name in attr_names:
        value = getattr(webview, name, None)
        if value is not None:
            return value
    raise DialogError("unsupported")


def _dialog_result(result: Any) -> tuple[str, ...]:
    """统一整理 pywebview 的返回值，丢弃空项。"""
    if not result:
        return ()
    if isinstance(result, (str, os.PathLike)):
        return (os.fspath(result),)
    try:
        return tuple(str(item) for item in result if item)
    except TypeError:
        return (str(result),)


def create_file_dialog(
    window: Any,
    kind: str,
    *,
    preferred_dir: str = "",
    allow_multiple: bool = False,
    save_filename: str = "",
    file_types: tuple[str, ...] | list[str] = (),
) -> tuple[str, ...]:
    """打开一个原生对话框并返回选中的路径。

    ``kind`` 取 ``folder``、``open``、``save``。取消时返回空元组；
    无法打开时抛 :class:`DialogError`，由调用方转成 ``{"error": 文案}``。
    """
    if window is None:
        raise DialogError("window")
    try:
        dialog_type = _dialog_type(kind)
        initial_dir = pick_initial_dir(preferred_dir)
    except DialogError:
        raise
    except Exception as exc:  # noqa: BLE001 - 解析对话框类型或初始目录也不能抛出
        applog.exception(f"native dialog setup failed | kind={kind}", exc)
        raise DialogError("dialog_failed") from exc
    if not initial_dir:
        raise DialogError("no_initial_dir")
    try:
        try:
            result = window.create_file_dialog(
                dialog_type,
                directory=initial_dir,
                allow_multiple=bool(allow_multiple),
                save_filename=str(save_filename or ""),
                file_types=tuple(file_types or ()),
            )
        except TypeError:
            # 兼容旧版 pywebview 的位置参数签名。
            result = window.create_file_dialog(
                dialog_type,
                initial_dir,
                bool(allow_multiple),
                str(save_filename or ""),
                tuple(file_types or ()),
            )
        return _dialog_result(result)
    except Exception as exc:  # noqa: BLE001 - 对话框异常不能穿过桥接
        applog.exception(f"native dialog failed | kind={kind}", exc)
        raise DialogError("dialog_failed") from exc
