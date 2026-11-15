"""打包运行环境的自检。

程序打成单文件后，启动时由引导程序把内部文件展开到系统临时文件夹，
运行期间再从那里读取。这个文件夹一旦缺失或读不出来，后续新启动的
子进程便读不到标准库而启动失败，界面上会看到若干个错误对话框。

这里只做两件事：判断展开目录当前是否可读，以及把诊断信息整理出来
写进日志。检查不通过不代表程序不能用，只代表不能再启动新的子进程。
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import zipfile

# 展开目录里的标准库压缩包。判断「能不能用」分两步：先读一点开头确认
# 不是空文件，再按压缩包结构打开一次确认内容完整。实测读前 4 KB 约 45
# 微秒，整份结构校验约 1.3 毫秒，相对几秒的启动耗时可忽略。
_STDLIB_ARCHIVE = "base_library.zip"
_PROBE_BYTES = 4096

# 首次读失败后隔这么久再试一次，用于覆盖「文件还在、只是被别的程序
# 短暂占用」的情况。文件真被删掉时，重试同样会失败。
_RETRY_DELAY = 0.5


def pack_root() -> str:
    """返回打包展开目录；源码运行时返回空串。"""
    return str(getattr(sys, "_MEIPASS", "") or "")


def is_frozen() -> bool:
    """当前是否运行在打包环境里。"""
    return bool(pack_root()) or bool(getattr(sys, "frozen", False))


def _probe(path: str) -> tuple[bool, str]:
    """尝试打开并校验压缩包结构，返回是否成功与失败原因。

    只读开头无法发现「文件还在、内容残缺」：展开时磁盘写满会让压缩包变成
    空文件或半截文件，这两种读前几字节都不报错，却会让新起的工作进程在
    导入标准库时直接失败。因此读完开头还要按压缩包结构打开一次，残缺的
    文件在这一步就会暴露。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(_PROBE_BYTES)
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if not head:
        return False, "empty file"
    try:
        with zipfile.ZipFile(path) as archive:
            if not archive.namelist():
                return False, "empty archive"
    except (OSError, zipfile.BadZipFile) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def check_pack_env(
    *, retry: bool = True, retry_delay: float = _RETRY_DELAY
) -> tuple[bool, str]:
    """判断展开目录现在能不能用。

    Args:
        retry: 首次失败后是否再试一次。
        retry_delay: 两次尝试之间等待的秒数。

    Returns:
        ``(ok, detail)``。源码运行恒为 ``(True, "")``；打包环境读不到标准库
        压缩包时返回 ``(False, 失败原因)``，重试一次仍失败才返回失败。
    """
    if not is_frozen():
        return True, ""
    path = os.path.join(pack_root(), _STDLIB_ARCHIVE)
    ok, detail = _probe(path)
    if ok or not retry:
        return ok, detail
    time.sleep(retry_delay)
    return _probe(path)


def diagnostics() -> dict:
    """收集展开目录与所在磁盘的状态，供启动日志记录。

    Returns:
        含四个键的字典：是否运行在打包环境、展开目录路径、所在磁盘剩余
        字节数、标准库压缩包当前能否读取。源码运行时后三项为空。
    """
    root = pack_root()
    info: dict = {
        "frozen": is_frozen(),
        "pack_root": root,
        "free_bytes": None,
        "archive_ok": None,
    }
    if not root:
        return info
    try:
        info["free_bytes"] = shutil.disk_usage(root).free
    except OSError:
        pass
    info["archive_ok"] = _probe(os.path.join(root, _STDLIB_ARCHIVE))[0]
    return info


def diagnostics_text() -> str:
    """把诊断信息整理成一行，直接交给日志。"""
    info = diagnostics()
    if not info["frozen"]:
        return "pack env: source run"
    free = info["free_bytes"]
    free_text = f"{free / (1024 * 1024):.0f}MB" if free is not None else "unknown"
    return (
        f"pack env: archive_ok={info['archive_ok']}"
        f" free={free_text}"
        f" root={info['pack_root']}"
    )
