"""目录写入监控：迁移期间看住源目录有没有被别人改动。

用 Windows 的 ``ReadDirectoryChangesW`` 把变化通知挂在一个目录句柄上，
内核有变化才唤醒等待的线程，平时线程阻塞在等待上，不轮询、不占 CPU。
过滤器里不带 ``FILE_NOTIFY_CHANGE_LAST_ACCESS``，所以只有写类操作进记录
（新建、内容修改、改名、删除），单纯打开读、保持只读句柄、列目录都不产生
记录，这些行为都已实测确认。

监控的是目录对象本身，所以目录被改名（迁移时原目录让位）之后句柄仍然有效，
继续报同一个目录里的变化。

一个容易踩的地方：句柄开着走异步读，等待超时只是回到循环看一眼停止位，
**不取消挂起的那次请求**，请求一直挂着等下一批变化。要是超时就取消重发，
两次请求之间那点空档里的变化会丢掉。
"""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes

# ---- Win32 常量 ----------------------------------------------------------

_FILE_LIST_DIRECTORY = 0x0001
_FILE_SHARE_ALL = 0x0007
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OVERLAPPED = 0x40000000

# 只订阅写类操作。读访问对应的 NOTIFY_LAST_ACCESS 不在里面，读不会打扰监控。
_NOTIFY_FILE_NAME = 0x00000001
_NOTIFY_DIR_NAME = 0x00000002
_NOTIFY_SIZE = 0x00000008
_NOTIFY_LAST_WRITE = 0x00000010
_NOTIFY_CREATION = 0x00000040
_NOTIFY_FILTER = (
    _NOTIFY_FILE_NAME
    | _NOTIFY_DIR_NAME
    | _NOTIFY_SIZE
    | _NOTIFY_LAST_WRITE
    | _NOTIFY_CREATION
)

# ``FILE_NOTIFY_INFORMATION`` 的三个 DWORD 头，文件名跟在后面。
_INFO_HEADER = 12

_WAIT_OBJECT_0 = 0
_ERROR_IO_PENDING = 997
_ERROR_NOTIFY_ENUM_DIR = 1022
_ERROR_OPERATION_ABORTED = 995
_INVALID_HANDLE = ctypes.c_void_p(-1).value

# 一次能攒下的变化量。内核缓冲区满了会给出「有东西变了但不说是哪个」的信号，
# 所以它只是一个容量，不是完整性保证。
_BUFFER_BYTES = 64 * 1024

# 记下来的路径条数上限。超过之后只累加计数：路径清单的作用是加快补齐，
# 完整性由全树对比保证，所以不必无上限地存。
_MAX_PATHS = 2000

# 等待超时，用来定期回到循环看一眼停止位。
_POLL_MS = 1000


class _OVERLAPPED(ctypes.Structure):
    """``OVERLAPPED``：异步读写要用的结构，这里只填事件句柄。"""

    _fields_ = (
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    )


class _FILE_NOTIFY_INFORMATION(ctypes.Structure):
    """``FILE_NOTIFY_INFORMATION``：一条变化记录的头。"""

    _fields_ = (
        ("NextEntryOffset", wintypes.DWORD),
        ("Action", wintypes.DWORD),
        ("FileNameLength", wintypes.DWORD),
    )


def _kernel32():
    """kernel32 句柄，签名都按指针大小声明（不声明会在 64 位下截断句柄）。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    k32.ReadDirectoryChangesW.restype = wintypes.BOOL
    k32.ReadDirectoryChangesW.argtypes = (
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.CreateEventW.argtypes = (
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    k32.GetOverlappedResult.restype = wintypes.BOOL
    k32.GetOverlappedResult.argtypes = (
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.BOOL,
    )
    k32.CancelIoEx.restype = wintypes.BOOL
    k32.CancelIoEx.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return k32


class ChangeWatcher:
    """盯住一棵目录树里的写类操作。

    用法固定为 ``start(root)`` → 迁移全程随时读 ``take()`` / ``event_count()``
    / ``overflowed`` → ``stop()``。``start()`` 失败返回假，调用方据此降级到
    「不跳过全树对比、切换前一定找人确认」，而不是假装监控可用。
    """

    def __init__(self, *, max_paths: int = _MAX_PATHS) -> None:
        self._max_paths = max(1, int(max_paths))
        self._lock = threading.Lock()
        self._paths: set[str] = set()
        self._events = 0
        self._overflow = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handle: int | None = None
        self._event_handle: int | None = None
        self._started = False

    # ---- 对外接口 --------------------------------------------------------

    def start(self, root: str) -> bool:
        """开始监控 ``root`` 整棵树；成功返回真，任何失败返回假。"""
        if self._started:
            return True
        if os.name != "nt" or not root or not os.path.isdir(root):
            return False
        try:
            k32 = _kernel32()
            handle = k32.CreateFileW(
                root,
                _FILE_LIST_DIRECTORY,
                _FILE_SHARE_ALL,
                None,
                _OPEN_EXISTING,
                _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OVERLAPPED,
                None,
            )
            if not handle or handle == _INVALID_HANDLE:
                return False
            event = k32.CreateEventW(None, False, False, None)
            if not event:
                k32.CloseHandle(handle)
                return False
        except (OSError, AttributeError, ValueError):
            return False
        self._k32 = k32
        self._handle = handle
        self._event_handle = event
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="migrate-watch", daemon=True
        )
        self._started = True
        self._thread.start()
        return True

    def stop(self) -> None:
        """停止监控并释放句柄；重复调用无副作用。"""
        if not self._started:
            return
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)
        k32 = getattr(self, "_k32", None)
        if k32 is not None:
            if self._handle:
                try:
                    k32.CancelIoEx(self._handle, None)
                except (OSError, AttributeError):
                    pass
                k32.CloseHandle(self._handle)
            if self._event_handle:
                k32.CloseHandle(self._event_handle)
        self._handle = None
        self._event_handle = None
        self._thread = None
        self._started = False

    def event_count(self) -> int:
        """从开始监控到现在的写事件累计条数（只增不减）。"""
        with self._lock:
            return self._events

    @property
    def overflowed(self) -> bool:
        """是否发生过「变化太多、内核只报了有变化没报具体是谁」。"""
        with self._lock:
            return self._overflow

    @property
    def started(self) -> bool:
        """监控是否已经在跑。"""
        return self._started

    def has_events(self) -> bool:
        """是否出现过任何写事件或溢出（等价于「这个目录被人碰过」）。"""
        with self._lock:
            return self._events > 0 or self._overflow

    def snapshot(self) -> set[str]:
        """当前记下的变化路径（相对被监控目录），不清空。"""
        with self._lock:
            return set(self._paths)

    def take(self) -> set[str]:
        """取走当前记下的变化路径并清空清单；累计条数不动。"""
        with self._lock:
            out = self._paths
            self._paths = set()
            return out

    # ---- 线程体 ----------------------------------------------------------

    def _mark_overflow(self) -> None:
        """内核说变化太多、没给名字：置标志，让调用方退回全树对比。"""
        with self._lock:
            self._overflow = True

    def _consume(self, buf, size: int) -> None:
        """把一批 ``FILE_NOTIFY_INFORMATION`` 解析成相对路径。"""
        base = ctypes.addressof(buf)
        offset = 0
        names: list[str] = []
        while offset + _INFO_HEADER <= size:
            info = _FILE_NOTIFY_INFORMATION.from_buffer(buf, offset)
            count = int(info.FileNameLength) // ctypes.sizeof(ctypes.c_wchar)
            if count > 0:
                raw = ctypes.wstring_at(base + offset + _INFO_HEADER, count)
                names.append(raw.replace("/", os.sep))
            step = int(info.NextEntryOffset)
            if step <= 0:
                break
            offset += step
        if not names:
            return
        with self._lock:
            self._events += len(names)
            room = self._max_paths - len(self._paths)
            if room > 0:
                self._paths.update(names[:room])

    def _run(self) -> None:
        """监控线程：发起一次异步读，等它完成，处理，再发起下一次。"""
        k32 = self._k32
        handle = self._handle
        event = self._event_handle
        buf = ctypes.create_string_buffer(_BUFFER_BYTES)
        overlapped = _OVERLAPPED()
        overlapped.hEvent = event
        returned = wintypes.DWORD(0)
        try:
            while not self._stop.is_set():
                ctypes.set_last_error(0)
                ok = k32.ReadDirectoryChangesW(
                    handle,
                    buf,
                    _BUFFER_BYTES,
                    True,
                    _NOTIFY_FILTER,
                    ctypes.byref(returned),
                    ctypes.byref(overlapped),
                    None,
                )
                if not ok:
                    code = ctypes.get_last_error()
                    if code == _ERROR_IO_PENDING:
                        pass
                    elif code == _ERROR_NOTIFY_ENUM_DIR:
                        self._mark_overflow()
                        continue
                    else:
                        break
                # 等完成。超时只是回到循环看停止位，挂起的那次请求不动。
                aborted = False
                while True:
                    waited = k32.WaitForSingleObject(event, _POLL_MS)
                    if waited == _WAIT_OBJECT_0:
                        break
                    if self._stop.is_set():
                        k32.CancelIoEx(handle, ctypes.byref(overlapped))
                        k32.WaitForSingleObject(event, _POLL_MS)
                        aborted = True
                        break
                if aborted:
                    break
                got = wintypes.DWORD(0)
                if not k32.GetOverlappedResult(
                    handle, ctypes.byref(overlapped), ctypes.byref(got), False
                ):
                    code = ctypes.get_last_error()
                    if code == _ERROR_OPERATION_ABORTED:
                        break
                    if code == _ERROR_NOTIFY_ENUM_DIR:
                        self._mark_overflow()
                        continue
                    break
                if got.value == 0:
                    # 缓冲区溢出：只说「变了」，不说是哪个
                    self._mark_overflow()
                    continue
                self._consume(buf, int(got.value))
        except (OSError, AttributeError, ValueError):
            # 监控线程出错不往上抛：迁移照走，调用方看 overflowed / started 决定降级
            self._mark_overflow()
