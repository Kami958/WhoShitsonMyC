"""两份快照的对比：生成变化树。

对外主类是 :class:`Diff`。它按「目录层级、懒加载」的方式工作——
每次只对比某个父目录下的**直接子节点**（:meth:`Diff.compare_children`），
用户在界面上展开哪一层才对比哪一层。这样即便快照有上百万条记录，
对比也永远只处理当前可见的一层，UI 不会卡（见设计文档第 6 节）。

v3 快照是邻接表结构（只存本段名字 + 父行 id）。对外接口仍收发
**路径字符串**（前端无感知）：会话内维护 ``{相对路径 → (旧侧 id, 新侧 id)}``
缓存，下钻时把父路径解析成两侧的 parent_id，各查一次子节点、按名字合并。

对比结果的诚实性：

- **根不同不给比**：两份快照扫的根不一样时直接报错，避免驴唇不对马嘴。
- **格式过旧不给比**：v1/v2 快照缺少邻接表结构，提示重新扫描。
- **跳过目录标记为不可比较**：某目录在一侧因无权限被跳过、另一侧扫到了，
  不谎报成「暴涨」，而是标 :attr:`~core.models.ChangeKind.INCOMPARABLE`。
"""

from __future__ import annotations

import os
import sqlite3

from .i18n import t
from .models import ChangeKind, DiffNode, SnapshotMeta
from .snapshot import SnapshotError, open_readonly, read_meta


class DiffError(Exception):
    """对比相关错误。"""


class DiffRootMismatch(DiffError):
    """两份快照的扫描根不一致，无法对比。"""

    def __init__(self, old_root: str, new_root: str) -> None:
        self.old_root = old_root
        self.new_root = new_root
        super().__init__(
            t(f"两份快照扫描的根目录不同，无法对比：\n  旧：{old_root} \n  新：{new_root}",
              f"The two snapshots scanned different root folders and cannot be compared:"
              f"\n  Base: {old_root} \n  Current: {new_root}")
        )


def _classify(
    old_size: int | None,
    new_size: int | None,
    incomparable: bool,
) -> tuple[int, int, int, ChangeKind]:
    """由前后大小得出 ``(old_size, new_size, delta, kind)``。

    ``None`` 表示该侧不存在此路径。
    """
    o = old_size or 0
    n = new_size or 0
    delta = n - o
    if incomparable:
        return o, n, delta, ChangeKind.INCOMPARABLE
    if old_size is None:
        return o, n, delta, ChangeKind.ADDED
    if new_size is None:
        return o, n, delta, ChangeKind.REMOVED
    if delta > 0:
        return o, n, delta, ChangeKind.GREW
    if delta < 0:
        return o, n, delta, ChangeKind.SHRANK
    return o, n, delta, ChangeKind.UNCHANGED


def _child_path(parent: str, name: str) -> str:
    """拼出子项的相对路径。根（parent == ""）的子项即其名字。"""
    return name if parent == "" else parent + os.sep + name


class Diff:
    """一次对比会话：持有两份快照的只读连接，按需逐层对比。

    用法::

        with Diff(old_db, new_db) as diff:
            top = diff.compare_children("")        # 顶层
            sub = diff.compare_children("Users")   # 展开某目录时再取

    记得用 ``with`` 或手动 :meth:`close` 释放数据库连接。
    """

    def __init__(self, old_db: str, new_db: str) -> None:
        """打开两份快照并校验根一致、格式为 v3。

        Args:
            old_db: 旧（基准）快照路径。
            new_db: 新（当前）快照路径。

        Raises:
            DiffRootMismatch: 两份快照的扫描根不同。
            SnapshotError: 任一快照无法读取、版本不符或格式过旧。
        """
        self.old_meta: SnapshotMeta = read_meta(old_db)
        self.new_meta: SnapshotMeta = read_meta(new_db)

        for meta, path in ((self.old_meta, old_db), (self.new_meta, new_db)):
            if meta.format_version < 3:
                raise SnapshotError(
                    t(f"快照格式过旧（v{meta.format_version}），"
                      f"请用当前版本重新扫描后再对比：{os.path.basename(path)}",
                      f"Snapshot format is too old (v{meta.format_version}); "
                      f"please rescan with the current version before comparing: "
                      f"{os.path.basename(path)}")
                )

        if _norm_root(self.old_meta.root) != _norm_root(self.new_meta.root):
            raise DiffRootMismatch(self.old_meta.root, self.new_meta.root)

        self._old = open_readonly(old_db)
        self._new = open_readonly(new_db)
        # 目录路径 → (旧侧 id, 新侧 id)；None 表示该侧没有此目录。
        # 根目录两侧都是 parent_id IS NULL 的那一行。
        self._dir_ids: dict[str, tuple[int | None, int | None]] = {
            "": (_root_id(self._old), _root_id(self._new))
        }
        # 跳过目录集合：命中即标不可比较。
        self._skipped: set[str] = set(self.old_meta.skipped) | set(
            self.new_meta.skipped
        )

    def compare_children(
        self, parent: str, *, sort: bool = True
    ) -> list[DiffNode]:
        """对比某父目录下的直接子节点。

        Args:
            parent: 父目录相对路径；顶层用空字符串 ``""``。
            sort: 是否按 ``|delta|`` 从大到小排序（默认是）。

        Returns:
            该层每个子节点的 :class:`DiffNode` 列表。

        Raises:
            DiffError: ``parent`` 在两份快照中都不存在。
        """
        old_pid, new_pid = self._resolve_dir(parent)

        old_rows = _children_map(self._old, old_pid)
        new_rows = _children_map(self._new, new_pid)

        nodes: list[DiffNode] = []
        for name in old_rows.keys() | new_rows.keys():
            o = old_rows.get(name)  # (id, size, is_dir, mtime)
            n = new_rows.get(name)
            path = _child_path(parent, name)
            is_dir = bool((o or n)[2])
            incomparable = path in self._skipped
            old_size, new_size, delta, kind = _classify(
                o[1] if o else None,
                n[1] if n else None,
                incomparable,
            )
            # 最后已知修改时间：优先取新侧（还存在的状态），删除项取旧侧。
            mtime = n[3] if n is not None else (o[3] if o is not None else 0)

            if is_dir:
                # 顺手缓存两侧 id，供下一层下钻直接命中。
                ids = (o[0] if o else None, n[0] if n else None)
                self._dir_ids[path] = ids
                has_children = _has_children(self._old, ids[0]) or _has_children(
                    self._new, ids[1]
                )
            else:
                has_children = False

            nodes.append(
                DiffNode(
                    path=path,
                    name=name,
                    is_dir=is_dir,
                    old_size=old_size,
                    new_size=new_size,
                    delta=delta,
                    kind=kind,
                    has_children=has_children,
                    mtime=mtime,
                )
            )

        if sort:
            nodes.sort(key=lambda d: abs(d.delta), reverse=True)
        return nodes

    def _resolve_dir(self, path: str) -> tuple[int | None, int | None]:
        """把目录相对路径解析成两侧的行 id（走缓存，缺则逐段解析补缓存）。

        Raises:
            DiffError: 路径在两份快照中都不存在。
        """
        cached = self._dir_ids.get(path)
        if cached is not None:
            return cached
        # 正常下钻总是命中缓存（父层对比时已写入）；逐段解析仅兜底
        # 「前端传来未见过的路径」这类异常情况。
        parts = path.split(os.sep)
        cur = ""
        for part in parts:
            parent_ids = self._dir_ids[cur]  # cur 必在缓存（自根起逐段建立）
            child = _child_path(cur, part)
            if child not in self._dir_ids:
                self._dir_ids[child] = (
                    _find_dir_id(self._old, parent_ids[0], part),
                    _find_dir_id(self._new, parent_ids[1], part),
                )
            cur = child
        ids = self._dir_ids[path]
        if ids == (None, None):
            raise DiffError(
                t(f"目录在两份快照中都不存在：{path}",
                  f"Folder does not exist in either snapshot: {path}")
            )
        return ids

    @property
    def total_delta(self) -> int:
        """新旧总大小之差（新 − 旧），供界面顶部概览显示。"""
        return self.new_meta.total_size - self.old_meta.total_size

    def close(self) -> None:
        """关闭两份快照连接。"""
        for conn in (self._old, self._new):
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass

    def __enter__(self) -> "Diff":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _norm_root(root: str) -> str:
    """归一化根路径用于比较（Windows 上大小写不敏感）。"""
    return os.path.normcase(os.path.normpath(root))


def _root_id(conn: sqlite3.Connection) -> int | None:
    """取根目录（parent_id IS NULL）的行 id。"""
    row = conn.execute(
        "SELECT id FROM entries WHERE parent_id IS NULL"
    ).fetchone()
    return row[0] if row else None


def _children_map(
    conn: sqlite3.Connection, parent_id: int | None
) -> dict[str, tuple[int, int, int, int]]:
    """取某父目录下直接子节点：``{name: (id, size, is_dir, mtime)}``。

    ``parent_id=None``（该侧没有此目录）时返回空。
    """
    if parent_id is None:
        return {}
    cur = conn.execute(
        "SELECT name, id, size, is_dir, mtime FROM entries WHERE parent_id = ?",
        (parent_id,),
    )
    return {name: (eid, size, is_dir, mtime) for name, eid, size, is_dir, mtime in cur}


def _find_dir_id(
    conn: sqlite3.Connection, parent_id: int | None, name: str
) -> int | None:
    """在某父目录下按名字找子目录的行 id；不存在返回 None。"""
    if parent_id is None:
        return None
    row = conn.execute(
        "SELECT id FROM entries WHERE parent_id = ? AND name = ? AND is_dir = 1",
        (parent_id, name),
    ).fetchone()
    return row[0] if row else None


def _has_children(conn: sqlite3.Connection, dir_id: int | None) -> bool:
    """判断某目录行下在该快照中是否有任何子节点。"""
    if dir_id is None:
        return False
    cur = conn.execute(
        "SELECT 1 FROM entries WHERE parent_id = ? LIMIT 1", (dir_id,)
    )
    return cur.fetchone() is not None


def compare_snapshots(old_db: str, new_db: str) -> list[DiffNode]:
    """便捷函数：打开两份快照并返回顶层对比结果。

    仅取顶层；下钻请用 :class:`Diff` 的 :meth:`~Diff.compare_children`。
    """
    with Diff(old_db, new_db) as diff:
        return diff.compare_children("")
