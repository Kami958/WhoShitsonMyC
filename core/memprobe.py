"""测试过程中回传给界面的实时资源占用。

整机 CPU 使用率靠两次系统累计时间的差值算，进程内存靠枚举当前进程树后
逐个取私有内存相加。两者都只在测试进度回调里按需调用，不在扫描热路径上。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import threading
import time

# ---- 系统接口声明 ------------------------------------------------------

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi", use_last_error=True)

_kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
_kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
_kernel32.Process32First.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32)]
_kernel32.Process32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32)]
_kernel32.OpenProcess.restype = ctypes.c_void_p
_kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
_kernel32.GetSystemTimes.argtypes = [
    ctypes.POINTER(wt.FILETIME),
    ctypes.POINTER(wt.FILETIME),
    ctypes.POINTER(wt.FILETIME),
]
_psapi.GetProcessMemoryInfo.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(_PROCESS_MEMORY_COUNTERS_EX),
    wt.DWORD,
]

# ---- 内部状态 ----------------------------------------------------------

_lock = threading.Lock()
_last_cpu: tuple[int, int] | None = None


def _filetime_to_int(value: wt.FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _system_times() -> tuple[int, int] | None:
    """返回 (空闲累计, 总累计)，单位为 100 纳秒。"""
    idle = wt.FILETIME()
    kernel = wt.FILETIME()
    user = wt.FILETIME()
    if not _kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    ):
        return None
    # kernel 已包含 idle，总时间是 kernel 加 user
    return _filetime_to_int(idle), _filetime_to_int(kernel) + _filetime_to_int(user)


def cpu_percent() -> float:
    """整机 CPU 使用率，0 到 100。两次调用之间的差值，首次调用返回 0。"""
    global _last_cpu
    now = _system_times()
    if now is None:
        return 0.0
    with _lock:
        prev = _last_cpu
        _last_cpu = now
    if prev is None:
        return 0.0
    idle_delta = now[0] - prev[0]
    total_delta = now[1] - prev[1]
    if total_delta <= 0:
        return 0.0
    used = (total_delta - idle_delta) / total_delta * 100.0
    return max(0.0, min(100.0, used))


def _process_pairs() -> list[tuple[int, int, str]]:
    """枚举全部进程，返回 (进程号, 父进程号, 可执行文件名)。"""
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return []
    pairs: list[tuple[int, int, str]] = []
    entry = _PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
    try:
        ok = _kernel32.Process32First(snap, ctypes.byref(entry))
        while ok:
            name = bytes(entry.szExeFile).split(b"\x00", 1)[0].decode(
                "mbcs", "ignore"
            )
            pairs.append(
                (
                    int(entry.th32ProcessID),
                    int(entry.th32ParentProcessID),
                    name.lower(),
                )
            )
            ok = _kernel32.Process32Next(snap, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snap)
    return pairs


def _private_of(pid: int) -> int:
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return 0
    try:
        counters = _PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS_EX)
        if not _psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        ):
            return 0
        return int(counters.PrivateUsage)
    finally:
        _kernel32.CloseHandle(handle)


def _tree_bytes(root_pid: int | None, include_root: bool) -> int:
    """根进程（及其后代的）私有内存合计，单位字节。

    include_root 为假时只算后代进程，用于测量解析进程自身的开销：
    主进程在解析阶段自己要收结果、建父子关系表，把这部分摊到每个
    解析进程上会把「每进程内存」算得远远偏大。
    另外只有与主程序同名的后代才算进来：界面框架会拉起自己的一批
    子进程，那部分占用与解析无关，算进去会把每进程内存抬得很高。
    """
    root = os.getpid() if root_pid is None else int(root_pid)
    kids: dict[int, list[int]] = {}
    names: dict[int, str] = {}
    for pid, ppid, name in _process_pairs():
        kids.setdefault(ppid, []).append(pid)
        names[pid] = name
    root_name = names.get(root, "")

    total = _private_of(root) if include_root else 0
    seen = {root}
    stack = list(kids.get(root, []))
    while stack:
        pid = stack.pop()
        # 进程号会被复用，快照里可能出现自指或成环的父子关系，
        # 没有去重就会一直往下展开，把测试线程拖死
        if pid in seen:
            continue
        seen.add(pid)
        # 只累加与主程序同名的进程；界面框架自己拉起的子进程不算解析开销
        if include_root or not root_name or names.get(pid) == root_name:
            total += _private_of(pid)
        stack.extend(kids.get(pid, []))
    return total


def tree_private_bytes(root_pid: int | None = None) -> int:
    """当前进程及其所有后代进程的私有内存合计，单位字节。

    共享内存在每个解析进程里各映射一次，用私有内存累加不会把它重复计算。
    """
    return _tree_bytes(root_pid, True)


def children_private_bytes(root_pid: int | None = None) -> int:
    """只算后代进程的私有内存合计，不含根进程自己。"""
    return _tree_bytes(root_pid, False)


def sample() -> dict:
    """一次采集，供进度事件直接放进载荷。"""
    return {
        "cpu_pct": round(cpu_percent(), 1),
        "mem_mb": round(tree_private_bytes() / 1048576.0, 1),
    }


if __name__ == "__main__":
    # 简易自检：连续采几次看数值是否随时间变化
    _, _ = _system_times()
    cpu_percent()
    for _ in range(5):
        time.sleep(1.0)
        print(sample())
