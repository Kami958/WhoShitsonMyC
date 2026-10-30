"""解析当前运行账户的 Windows 已知文件夹。

数据目录跟随启动程序的账户，不跨账户共享或复制数据。
同一台电脑上不同账户各自使用自己的数据目录，互不影响。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class UserLookupError(OSError):
    """当前账户的已知文件夹不可用。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason or "no_base"


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    """把标准 GUID 文本转成结构体。"""
    parts = text.strip("{}").split("-")
    data4 = bytes.fromhex(parts[3] + parts[4])
    return _GUID(
        int(parts[0], 16),
        int(parts[1], 16),
        int(parts[2], 16),
        (ctypes.c_ubyte * 8)(*data4),
    )


_FOLDER_LOCAL_APPDATA = _guid("F1B32785-6FBA-4FCF-9D55-7B8E7F157091")
_FOLDER_PROFILE = _guid("5E6C858F-0E22-4760-9AFE-EA3317B67173")
_FOLDER_PUBLIC = _guid("DFDF76A2-C82A-4D63-906A-5644AC457385")

_shell32: ctypes.WinDLL | None = None
_ole32: ctypes.WinDLL | None = None


def _windows_api() -> tuple[ctypes.WinDLL, ctypes.WinDLL]:
    """只在需要时加载 Windows API，并固定签名。"""
    global _shell32, _ole32
    if _shell32 is None or _ole32 is None:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(_GUID),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree.restype = None
        _shell32 = shell32
        _ole32 = ole32
    return _shell32, _ole32


def _known_folder(folder: _GUID) -> str:
    """读取当前账户的已知文件夹；失败返回空串。"""
    shell32, ole32 = _windows_api()
    path = ctypes.c_wchar_p()
    result = shell32.SHGetKnownFolderPath(
        ctypes.byref(folder), 0, None, ctypes.byref(path)
    )
    if result != 0:
        return ""
    try:
        return path.value or ""
    finally:
        ole32.CoTaskMemFree(ctypes.cast(path, ctypes.c_void_p))


def _env_path(name: str) -> str:
    """环境变量里的绝对路径；缺失或不是绝对路径时返回空串。"""
    value = os.environ.get(name) or ""
    if not value:
        return ""
    try:
        absolute = os.path.abspath(os.path.expanduser(value))
    except (OSError, ValueError):
        return ""
    return absolute if os.path.isabs(absolute) else ""


def _home_dir() -> str:
    """当前账户主目录；取不到绝对路径时返回空串。"""
    if os.name == "nt":
        home = _known_folder(_FOLDER_PROFILE) or _env_path("USERPROFILE")
    else:
        home = _env_path("HOME")
    if home:
        return home
    try:
        expanded = os.path.expanduser("~")
    except (OSError, RuntimeError):
        return ""
    if not expanded or expanded == "~" or not os.path.isabs(expanded):
        return ""
    return expanded


def _local_app_data(home: str) -> str:
    """当前账户的本地应用数据目录。"""
    if os.name == "nt":
        local = _known_folder(_FOLDER_LOCAL_APPDATA) or _env_path("LOCALAPPDATA")
        if not local and home:
            local = os.path.join(home, "AppData", "Local")
        return local
    local = _env_path("XDG_DATA_HOME")
    if not local and home:
        local = os.path.join(home, ".local", "share")
    return local


def _public_dir(home: str) -> str:
    """当前账户可访问的公共目录，取不到时退回主目录。"""
    if os.name == "nt":
        public = _known_folder(_FOLDER_PUBLIC) or _env_path("PUBLIC")
        if not public:
            drive = os.environ.get("SystemDrive") or "C:"
            public = os.path.join(drive + os.sep, "Users", "Public")
        if os.path.isdir(public):
            return public
    return home


def current_account_name() -> str:
    """当前运行账户名，仅用于界面显示。"""
    return os.environ.get("USERNAME") or os.environ.get("USER") or ""


class CurrentUser:
    """当前运行程序的账户及其本地数据位置。"""

    def __init__(
        self,
        *,
        local_app_data: str,
        home_dir: str,
        public_dir: str,
        account: str,
    ) -> None:
        self.local_app_data = local_app_data
        self.home_dir = home_dir
        self.public_dir = public_dir
        self.account = account

    def accessible_dir(self, path: str) -> bool:
        """检查目录是否可列出。"""
        if not path:
            return False
        try:
            with os.scandir(path) as entries:
                next(entries, None)
            return True
        except OSError:
            return False


def open_current_user() -> CurrentUser:
    """解析当前运行程序的账户位置。

    数据目录跟随这个账户，不再查询桌面会话属于谁。已知文件夹不可用时
    依次回落到环境变量和主目录，全部不可用才报错。
    """
    try:
        home = _home_dir()
        local = _local_app_data(home)
    except Exception as exc:  # noqa: BLE001 - 原始 API 异常不能带到界面
        raise UserLookupError("no_base") from exc
    if not home or not os.path.isabs(home):
        if not local or not os.path.isabs(local):
            raise UserLookupError("no_base")
        home = os.path.dirname(local)
    if not local or not os.path.isabs(local):
        raise UserLookupError("no_base")
    return CurrentUser(
        local_app_data=os.path.normpath(local),
        home_dir=os.path.normpath(home),
        public_dir=os.path.normpath(_public_dir(home)),
        account=current_account_name(),
    )
