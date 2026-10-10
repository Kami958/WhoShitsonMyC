from __future__ import annotations

from __future__ import annotations

import os
import sqlite3
from array import array

from ..models import ChangeKind
from ..snapshot import (
    SNAPSHOT_FORMAT_VERSION,
    entry_columns,
    open_readonly,
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


def _max_entry_id(conn: sqlite3.Connection) -> int:
    """entries 表最大 id；空表返回 0。"""
    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM entries").fetchone()
    return int(row[0]) if row else 0


def _path_from_parent(dir_paths: list[str], pid: int, name: str) -> str:
    """由父目录路径拼出当前条目相对路径；根直接子项即名字。"""
    if pid < 1:
        return name
    parent_path = dir_paths[pid]
    return name if not parent_path else parent_path + os.sep + name


def _read_side_path_ids(
    conn: sqlite3.Connection,
) -> tuple[array, dict[str, int]]:
    """流式读一侧快照，返回 (parent_of 数组, {相对路径: id})。

    v3 快照由本程序写入，父行 id 恒小于子行 id，因此按 ``ORDER BY id``
    顺序拼路径即可，无需在内存里再建 children 邻接表。``dir_paths`` 只保留
    目录路径，文件路径进入 ``path_ids`` 后立即释放。
    """
    n = _max_entry_id(conn)
    parents = array("q", [-1]) * (n + 1)
    dir_paths: list[str] = [""] * (n + 1)
    path_ids: dict[str, int] = {}
    cur = conn.execute(
        "SELECT id, parent_id, name, is_dir FROM entries ORDER BY id"
    )
    for eid, pid, name, is_dir in cur:
        parents[eid] = -1 if pid is None else pid
        path = _path_from_parent(dir_paths, parents[eid], name)
        path_ids[path] = eid
        if is_dir:
            dir_paths[eid] = path
    return parents, path_ids


def _read_side_added_ids(
    conn: sqlite3.Connection,
    old_path_ids: dict[str, int],
) -> tuple[array, list[int]]:
    """流式读另一侧快照，返回 (parent_of 数组, 新增条目 id 列表)。

    新增条目指路径不在旧侧；共同存在的路径从 ``old_path_ids`` 中移除，
    调用方剩下的键就是已删除条目。
    """
    n = _max_entry_id(conn)
    parents = array("q", [-1]) * (n + 1)
    dir_paths: list[str] = [""] * (n + 1)
    added_ids: list[int] = []
    cur = conn.execute(
        "SELECT id, parent_id, name, is_dir FROM entries ORDER BY id"
    )
    for eid, pid, name, is_dir in cur:
        parents[eid] = -1 if pid is None else pid
        path = _path_from_parent(dir_paths, parents[eid], name)
        if path in old_path_ids:
            old_path_ids.pop(path)
        else:
            added_ids.append(eid)
        if is_dir:
            dir_paths[eid] = path
    return parents, added_ids


def _propagate_ancestors(
    diff_ids: list[int] | set[int],
    parent_of: array,
) -> set[int]:
    """差异条目连同其各级祖先（沿 parent 链）的 id 集合。

    parent_of 为按条目 id 索引的数组，根目录的父 id 记 -1。
    """
    up: set[int] = set()
    stack = list(diff_ids)
    while stack:
        eid = stack.pop()
        if eid in up:
            continue
        up.add(eid)
        pid = parent_of[eid] if eid < len(parent_of) else -1
        if pid > 0:
            stack.append(pid)
    return up


def _subtree_change_id_sets(
    old_db: str, new_db: str
) -> tuple[set[int], set[int]]:
    """两侧快照按相对路径差集，得 (新增侧子树 id 集, 删除侧子树 id 集)。

    两侧快照各自独立分配条目 id，跨快照可比的是相对路径：各自拼出
    id → 路径后做差集；返回的集合同时包含变更条目自身和它各级祖先，
    前端据此保留目录作为「新增/已删除」筛选的下钻入口。一次读两侧各一遍，
    added / removed 两个方向共用。
    """
    old = open_readonly(old_db)
    new = open_readonly(new_db)
    try:
        old_parents, old_path_ids = _read_side_path_ids(old)
        new_parents, added_ids = _read_side_added_ids(new, old_path_ids)
        removed_values = old_path_ids.values()
    finally:
        old.close()
        new.close()
    return (
        _propagate_ancestors(added_ids, new_parents),
        _propagate_ancestors(removed_values, old_parents),
    )


def _read_side_link_ids(conn: sqlite3.Connection) -> tuple[array, list[int]]:
    """读一侧快照的链接条目，返回 (parent_of 数组, 链接条目 id 列表)。

    只扫 ``reparse_tag != 0`` 的行（v4+ 快照；v3 无该列，调用方先按版本跳过）。
    不拼路径，差集不需要；只用于沿 parent 链上卷出「含链接子树」标记。
    """
    n = _max_entry_id(conn)
    parents = array("q", [-1]) * (n + 1)
    ids: list[int] = []
    cur = conn.execute(
        "SELECT id, parent_id, reparse_tag FROM entries WHERE reparse_tag != 0"
    )
    for eid, pid, _tag in cur:
        parents[eid] = -1 if pid is None else pid
        ids.append(eid)
    return parents, ids


# 搜索候选：(父路径, 名字, 旧侧行, 新侧行)；
# 行为 (id, size, is_dir, mtime, reparse_tag, link_target)
_SideRow = tuple[int, int, int, int, int, str]
_SearchEntry = tuple[str, str, "_SideRow | None", "_SideRow | None"]


def _conn_version(conn: sqlite3.Connection) -> int:
    """连接对应的快照格式版本（open_readonly 已挂载；缺省最新版）。"""
    return int(getattr(conn, "_wmc_version", SNAPSHOT_FORMAT_VERSION))


def _side_cols(conn: sqlite3.Connection) -> str:
    """``name, id, size, is_dir, mtime[, reparse_tag[, link_target]]`` —— 按版本分派。"""
    ver = _conn_version(conn)
    if ver >= 5:
        return "name, id, size, is_dir, mtime, reparse_tag, link_target"
    if ver >= 4:
        return "name, id, size, is_dir, mtime, reparse_tag"
    return "name, id, size, is_dir, mtime"


def _row_cols(conn: sqlite3.Connection) -> str:
    """``id, parent_id, name, size, is_dir, mtime[, reparse_tag]`` —— 按连接版本分派。"""
    return entry_columns(_conn_version(conn))

# 与前端 SORT_OPTIONS 对齐；未知值回退 delta-desc
_SEARCH_SORT_KEYS = frozenset({
    "delta-desc", "size-desc", "pct-desc", "name-asc", "name-desc", "mtime-desc",
})


