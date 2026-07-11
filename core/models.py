"""核心数据结构定义。

这些是核心引擎在扫描、存储、对比各环节之间传递的数据契约。
全部使用 dataclass，轻量、可读、易测试。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


# 快照文件格式版本号。结构不兼容变更时递增，加载旧快照时用于校验。
# v2：entries 表新增 mtime 列。
# v3：邻接表结构——不再存完整路径，每行只存本段名字 + 父行 id，
#     体积缩到约 1/5；mtime 改为整数秒。v1/v2 快照仍可列举，但不可对比。
SNAPSHOT_FORMAT_VERSION = 3


@dataclass(slots=True)
class Entry:
    """一条文件或目录记录（对应 SQLite ``entries`` 表的一行，邻接表结构）。

    Attributes:
        id: 行 id，扫描时分配，根目录固定为 1。
        parent_id: 父目录的行 id；根目录为 ``None``。
        name: 本段名字（如 ``a.txt``）；根目录为空字符串 ``""``。
        size: 字节数。文件为其自身大小；目录为其递归聚合总大小。
        is_dir: True=目录，False=文件。
        mtime: 最后修改时间的 Unix 时间戳（整数秒）；获取失败为 0。
    """

    id: int
    parent_id: int | None
    name: str
    size: int
    is_dir: bool
    mtime: int = 0


@dataclass(slots=True)
class SnapshotMeta:
    """一份快照的元信息（对应 SQLite ``meta`` 表）。

    Attributes:
        root: 扫描根的绝对路径（如 ``C:\\`` 或 ``D:\\Games``）。
        scanned_at: 扫描完成的 Unix 时间戳（秒）。
        total_size: 根目录聚合总大小（字节）。
        file_count: 记录的文件数（不含目录）。
        dir_count: 记录的目录数。
        skipped: 扫描时因无权限或出错而跳过的目录（相对路径）列表。
            对比时用于诚实标记「一侧缺数据、不可比较」。
        format_version: 快照格式版本号。
    """

    root: str
    scanned_at: float
    total_size: int = 0
    file_count: int = 0
    dir_count: int = 0
    skipped: list[str] = field(default_factory=list)
    format_version: int = SNAPSHOT_FORMAT_VERSION


class ChangeKind(enum.Enum):
    """一个节点在两份快照之间的变化类型。"""

    GREW = "grew"          # 变大（新旧都有，size 增加）
    SHRANK = "shrank"      # 变小（新旧都有，size 减少）
    ADDED = "added"        # 新增（只在新快照有）
    REMOVED = "removed"    # 删除（只在旧快照有）
    UNCHANGED = "unchanged"  # 大小未变
    INCOMPARABLE = "incomparable"  # 不可比较（一侧因跳过而缺数据）


@dataclass(slots=True)
class DiffNode:
    """变化树中的一个节点（一个目录或文件的前后对比结果）。

    前端据此渲染树：正的 delta 显示红色「变大」，负的显示绿色「变小」。

    Attributes:
        path: 相对扫描根的路径。
        name: 用于显示的名称（path 的最后一段）。
        is_dir: 是否为目录。
        old_size: 旧快照中的大小；不存在时为 0。
        new_size: 新快照中的大小；不存在时为 0。
        delta: ``new_size - old_size``。正=变大，负=变小。
        kind: 变化类型，见 :class:`ChangeKind`。
        has_children: 是否有子节点可下钻（目录且下面有内容）。
            子节点采用懒加载，展开时才查询，故此处不内联 children。
        mtime: 该节点最后已知的修改时间戳——新快照中存在取新侧，
            否则取旧侧；v1 旧快照没有此信息，为 0。供前端按时间排序。
    """

    path: str
    name: str
    is_dir: bool
    old_size: int
    new_size: int
    delta: int
    kind: ChangeKind
    has_children: bool = False
    mtime: float = 0.0

    def to_dict(self) -> dict:
        """转为可 JSON 序列化的 dict，供 pywebview 桥接传给前端。"""
        return {
            "path": self.path,
            "name": self.name,
            "is_dir": self.is_dir,
            "old_size": self.old_size,
            "new_size": self.new_size,
            "delta": self.delta,
            "kind": self.kind.value,
            "has_children": self.has_children,
            "mtime": self.mtime,
        }
