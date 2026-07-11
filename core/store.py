"""快照的存放位置、命名与列举。

负责「快照存哪、叫什么名、有哪些」这类应用层事务，与纯遍历/对比逻辑分开。
快照固定存于用户数据目录（Windows 为
``%LOCALAPPDATA%\\WhoShitsOnMyC\\snapshots``），其它平台回落到
``~/.local/share`` 或 ``~``，不提供迁移——位置唯一、行为可预期。

会话级设置（扫描线程数、是否压缩快照）仅存在于内存、随进程结束丢弃，
不写任何配置文件。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

from .compress import (
    drop_cache_for,
    is_compressed_path,
    is_snapshot_filename,
    read_meta_any,
)
from .snapshot import SnapshotError

_APP_DIR_NAME = "WhoShitsOnMyC"
# 文件名中不安全的字符统一替换为下划线。
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")

# 扫描线程数的允许范围（上限防手滑，不是性能上限）。
_WORKERS_MIN, _WORKERS_MAX = 1, 32


class StoreError(Exception):
    """快照存储管理相关错误。"""


@dataclass(slots=True)
class SnapshotInfo:
    """列举快照时返回的摘要（读取每份快照的 meta 得到）。"""

    path: str          # 快照文件绝对路径（``.db`` 或 ``.dbz``）
    root: str          # 该快照扫描的根
    scanned_at: float  # 扫描时间戳
    total_size: int    # 根聚合总大小
    file_count: int    # 文件数
    skipped_count: int # 跳过的目录数
    compressed: bool = False  # 是否为压缩包（``.dbz``）
    file_size: int = 0        # 磁盘上该文件的字节数

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "root": self.root,
            "scanned_at": self.scanned_at,
            "total_size": self.total_size,
            "file_count": self.file_count,
            "skipped_count": self.skipped_count,
            "compressed": self.compressed,
            "file_size": self.file_size,
        }


def _app_base_dir() -> str:
    """返回应用数据根目录（必要时创建）。默认快照目录就在这。"""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get(
            "XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share")
        )
    path = os.path.join(base, _APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def default_snapshot_dir() -> str:
    """返回默认的快照存放目录（必要时创建）。"""
    path = os.path.join(_app_base_dir(), "snapshots")
    os.makedirs(path, exist_ok=True)
    return path


# ---- 会话级设置（仅本次进程，不落盘）------------------------------------


def default_scan_workers() -> int:
    """扫描线程数的默认值：``max(1, CPU 核数)``，即用满所有核心。"""
    return max(1, os.cpu_count() or 1)


# 当前会话的扫描线程数。只存在于内存中，进程退出即丢弃，不写任何配置文件。
_scan_workers = default_scan_workers()

# 扫描完成后是否把 ``.db`` 压成 ``.dbz``。默认关闭（优先对比速度）。
_compress_snapshots = False


def get_scan_workers() -> int:
    """返回本次会话当前生效的扫描线程数（默认 :func:`default_scan_workers`）。"""
    return _scan_workers


def set_scan_workers(n: int) -> int:
    """设置本次会话的扫描线程数（越界自动收拢到允许范围），返回生效值。

    仅在内存中生效，重启程序后回到默认值——不持久化到磁盘。
    """
    global _scan_workers
    _scan_workers = max(_WORKERS_MIN, min(_WORKERS_MAX, int(n)))
    return _scan_workers


def get_compress_snapshots() -> bool:
    """返回是否在扫描完成后压缩快照（默认 False）。"""
    return _compress_snapshots


def set_compress_snapshots(enabled: bool) -> bool:
    """设置是否在扫描完成后压缩快照，返回生效值。仅本次会话。"""
    global _compress_snapshots
    _compress_snapshots = bool(enabled)
    return _compress_snapshots


def _root_label(root: str) -> str:
    """由扫描根生成一段文件名友好的标签。

    例如 ``C:\\`` → ``C``，``D:\\Games`` → ``D_Games``。
    """
    drive, tail = os.path.splitdrive(root)
    drive = drive.rstrip(":\\/")
    tail = tail.strip("\\/")
    label = drive if not tail else f"{drive}_{tail}" if drive else tail
    label = _UNSAFE_RE.sub("_", label).strip("_")
    return label or "root"


def new_snapshot_path(root: str, when: float | None = None, out_dir: str | None = None) -> str:
    """为一次新扫描生成快照文件路径，形如 ``C_2026-07-10_1530.db``。

    扫描过程始终先写未压缩的 ``.db``；若开启压缩，扫完后再换成 ``.dbz``。

    Args:
        root: 扫描根。
        when: 时间戳，默认当前时间（仅测试会传入固定值）。
        out_dir: 存放目录，默认 :func:`default_snapshot_dir`。
    """
    out_dir = out_dir or default_snapshot_dir()
    stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(when or time.time()))
    name = f"{_root_label(root)}_{stamp}.db"
    return os.path.join(out_dir, name)


def list_snapshots(out_dir: str | None = None) -> list[SnapshotInfo]:
    """列举某目录下所有可读的快照，按扫描时间从新到旧排序。

    同时支持 ``.db`` 与 ``.dbz``。``.dbz`` 只读 zip 内 ``meta.json``，不解压整库。
    无法读取（损坏/版本不符）的文件会被跳过，不影响其余。
    """
    out_dir = out_dir or default_snapshot_dir()
    if not os.path.isdir(out_dir):
        return []

    infos: list[SnapshotInfo] = []
    for name in os.listdir(out_dir):
        if not is_snapshot_filename(name):
            continue
        path = os.path.join(out_dir, name)
        try:
            meta = read_meta_any(path)
        except SnapshotError:
            continue
        try:
            file_size = os.path.getsize(path)
        except OSError:
            file_size = 0
        infos.append(
            SnapshotInfo(
                path=path,
                root=meta.root,
                scanned_at=meta.scanned_at,
                total_size=meta.total_size,
                file_count=meta.file_count,
                skipped_count=len(meta.skipped),
                compressed=is_compressed_path(path),
                file_size=file_size,
            )
        )
    infos.sort(key=lambda i: i.scanned_at, reverse=True)
    return infos


def delete_snapshot(path: str) -> None:
    """删除一个快照文件；若是 ``.dbz``，顺带清掉解压缓存。文件不存在时静默返回。"""
    try:
        drop_cache_for(path)
    except Exception:  # noqa: BLE001 - 清缓存失败不影响删除本体
        pass
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
