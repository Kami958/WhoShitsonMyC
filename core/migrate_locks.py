"""查某个目录里的文件正被哪些进程占着。

用 Windows 的重启管理器（Restart Manager）：把一个或多个文件的路径注册进
会话，系统返回当前持有这些文件的进程。不用遍历系统句柄表，普通权限就能调。

系统只回答「这个进程和这批文件有关」，不回答「它具体占着哪一个」，所以
返回的条目只有进程号与程序名，用来告诉用户该关哪个程序。查询失败一律返回
空表：查占用是为了给提示，不能因为查不动就阻断迁移。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

# 会话键缓冲：文档要求 CCH_RM_SESSION_KEY + 1 个字符。
_CCH_RM_SESSION_KEY = 32

# 进程名与服务名的长度上限，结构里是定长数组。
_CCH_RM_MAX_APP_NAME = 255
_CCH_RM_MAX_SVC_NAME = 63

_ERROR_SUCCESS = 0
_ERROR_MORE_DATA = 234

# 一次最多注册多少个路径。这个数字只影响提示里能列出多少细节，
# 不是正确性的一部分，所以不必把整棵树都塞进去。
_MAX_PATHS = 64


class _RM_UNIQUE_PROCESS(ctypes.Structure):
    """``RM_UNIQUE_PROCESS``：进程号加启动时间，两者一起才能唯一确定一个进程。"""

    _fields_ = (
        ("dwProcessId", wintypes.DWORD),
        ("ProcessStartTime", wintypes.FILETIME),
    )


class _RM_PROCESS_INFO(ctypes.Structure):
    """``RM_PROCESS_INFO``：重启管理器报出来的一条占用记录。"""

    _fields_ = (
        ("Process", _RM_UNIQUE_PROCESS),
        ("strAppName", ctypes.c_wchar * (_CCH_RM_MAX_APP_NAME + 1)),
        ("strServiceShortName", ctypes.c_wchar * (_CCH_RM_MAX_SVC_NAME + 1)),
        ("ApplicationType", ctypes.c_int),
        ("AppStatus", wintypes.ULONG),
        ("TSSessionId", wintypes.DWORD),
        ("bRestartable", wintypes.BOOL),
    )


def _rstrtmgr():
    """rstrtmgr 的句柄与签名（不声明签名会把会话号当成返回值的全部）。"""
    lib = ctypes.WinDLL("rstrtmgr", use_last_error=True)
    lib.RmStartSession.restype = wintypes.DWORD
    lib.RmStartSession.argtypes = (
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
        ctypes.c_wchar_p,
    )
    lib.RmRegisterResources.restype = wintypes.DWORD
    lib.RmRegisterResources.argtypes = (
        wintypes.DWORD,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_wchar_p),
        wintypes.UINT,
        ctypes.c_void_p,
        wintypes.UINT,
        ctypes.c_void_p,
    )
    lib.RmGetList.restype = wintypes.DWORD
    lib.RmGetList.argtypes = (
        wintypes.DWORD,
        ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(_RM_PROCESS_INFO),
        ctypes.POINTER(wintypes.LONG),
    )
    lib.RmEndSession.restype = wintypes.DWORD
    lib.RmEndSession.argtypes = (wintypes.DWORD,)
    return lib


def _clean_paths(paths) -> list[str]:
    """去重、取绝对路径、按上限截断，只留下真实存在的项。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in paths or ():
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            path = os.path.abspath(text)
        except (OSError, ValueError):
            continue
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
        if len(out) >= _MAX_PATHS:
            break
    return out


def find_holders(paths) -> list[dict]:
    """返回占用这批路径的进程，形如 ``[{"pid": 1234, "name": "notepad.exe"}]``。

    同一个进程只出现一次。拿不到结果（不支持、权限不足、调用失败）返回空表。
    """
    if os.name != "nt":
        return []
    files = _clean_paths(paths)
    if not files:
        return []
    try:
        lib = _rstrtmgr()
        session = wintypes.DWORD(0)
        key = ctypes.create_unicode_buffer(_CCH_RM_SESSION_KEY + 1)
        if lib.RmStartSession(ctypes.byref(session), 0, key) != _ERROR_SUCCESS:
            return []
        try:
            return _query(lib, session.value, files)
        finally:
            lib.RmEndSession(session.value)
    except (OSError, AttributeError, ValueError):
        return []


def _query(lib, session: int, files: list[str]) -> list[dict]:
    """在已开的会话里注册文件并取进程表；先问需要多大，再按需分配。"""
    array = (ctypes.c_wchar_p * len(files))(*files)
    if (
        lib.RmRegisterResources(session, len(files), array, 0, None, 0, None)
        != _ERROR_SUCCESS
    ):
        return []
    needed = wintypes.UINT(0)
    count = wintypes.UINT(0)
    reboot = wintypes.LONG(0)
    code = lib.RmGetList(
        session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reboot)
    )
    if code == _ERROR_SUCCESS:
        # 一个占用都没有
        return []
    if code != _ERROR_MORE_DATA:
        return []
    room = max(1, int(needed.value))
    rows = (_RM_PROCESS_INFO * room)()
    count = wintypes.UINT(room)
    code = lib.RmGetList(
        session, ctypes.byref(needed), ctypes.byref(count), rows, ctypes.byref(reboot)
    )
    if code != _ERROR_SUCCESS:
        return []
    out: list[dict] = []
    seen: set[int] = set()
    for i in range(min(int(count.value), room)):
        pid = int(rows[i].Process.dwProcessId)
        if pid <= 0 or pid in seen:
            continue
        seen.add(pid)
        name = str(rows[i].strAppName or "").strip()
        service = str(rows[i].strServiceShortName or "").strip()
        out.append({"pid": pid, "name": name or service or f"pid {pid}"})
    return out


def holder_names(paths) -> list[str]:
    """占用者的程序名（去重、保持顺序），供弹窗文案直接使用。"""
    names: list[str] = []
    for item in find_holders(paths):
        name = str(item.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names
