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


def _basis_value(size, actual, source: str) -> int:
    """按取数口径算一条记录的大小（与 :func:`_size_of` 同一规则）。

    ``actual`` 为空表示这份快照没记这一格（旧版本或非快速扫描路径），
    此时回落到逻辑大小，不能当成 0。
    """
    if source == "actual" and actual is not None:
        return int(actual)
    return int(size or 0)


def _size_cols_sql(conn: sqlite3.Connection) -> str:
    """全表扫描时取大小与实际占用两列的片段，旧版本补 NULL。"""
    if _conn_version(conn) >= 6:
        return "size, actual_size"
    return "size, NULL AS actual_size"


def _read_side_path_ids(
    conn: sqlite3.Connection, source: str = "logical"
) -> tuple[array, dict[str, int], array]:
    """流式读一侧快照，返回 (parent_of 数组, {相对路径: id}, 按 id 索引的大小数组)。

    大小按 ``source`` 指定的口径取，与界面显示、节点差值共用同一规则，
    否则「内含变大/变小」筛选会和用户看到的数值方向对不上。

    父行 id 恒小于子行 id，因此按 ``ORDER BY id`` 顺序拼路径即可，无需在
    内存里再建 children 邻接表。``dir_paths`` 只保留目录路径，文件路径进入
    ``path_ids`` 后立即释放。

    大小单独放定长数组，而不是和 id 打包进字典的值：字典值保持纯整数时，
    每条省下一个容器对象与两个整数对象的开销，百万条量级实测内存少八成、
    耗时持平。
    """
    n = _max_entry_id(conn)
    parents = array("q", [-1]) * (n + 1)
    sizes = array("q", [0]) * (n + 1)
    dir_paths: list[str] = [""] * (n + 1)
    path_ids: dict[str, int] = {}
    cur = conn.execute(
        f"SELECT id, parent_id, name, is_dir, {_size_cols_sql(conn)}"
        " FROM entries ORDER BY id"
    )
    for eid, pid, name, is_dir, size, actual in cur:
        parents[eid] = -1 if pid is None else pid
        path = _path_from_parent(dir_paths, parents[eid], name)
        path_ids[path] = eid
        sizes[eid] = _basis_value(size, actual, source)
        if is_dir:
            dir_paths[eid] = path
    return parents, path_ids, sizes


def _read_side_changes(
    conn: sqlite3.Connection,
    old_path_ids: dict[str, int],
    old_sizes: array,
    source: str,
) -> tuple[array, list[int], list[int], list[int]]:
    """流式读另一侧快照，返回 (parent_of 数组, 新增 id, 变大 id, 变小 id)。

    新增条目指路径不在旧侧；共同存在的路径从 ``old_path_ids`` 中移除，
    调用方剩下的键就是已删除条目。变大/变小按两侧同路径条目的字节数相减
    判定，只对两侧都存在的条目成立——单侧存在的条目已归入新增或删除。
    """
    n = _max_entry_id(conn)
    parents = array("q", [-1]) * (n + 1)
    dir_paths: list[str] = [""] * (n + 1)
    added_ids: list[int] = []
    grew_ids: list[int] = []
    shrank_ids: list[int] = []
    cur = conn.execute(
        f"SELECT id, parent_id, name, is_dir, {_size_cols_sql(conn)}"
        " FROM entries ORDER BY id"
    )
    for eid, pid, name, is_dir, size, actual in cur:
        parents[eid] = -1 if pid is None else pid
        path = _path_from_parent(dir_paths, parents[eid], name)
        old_id = old_path_ids.pop(path, None)
        if old_id is None:
            added_ids.append(eid)
        else:
            diff = _basis_value(size, actual, source) - old_sizes[old_id]
            if diff > 0:
                grew_ids.append(eid)
            elif diff < 0:
                shrank_ids.append(eid)
        if is_dir:
            dir_paths[eid] = path
    return parents, added_ids, grew_ids, shrank_ids


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


def _subtree_mark_id_sets(
    old_db: str, new_db: str, source: str = "logical"
) -> tuple[set[int], set[int], set[int], set[int]]:
    """两侧快照对比，得 (新增, 已删除, 变大, 变小) 四组子树 id 集。

    两组快照各自独立分配条目 id，跨快照唯一可比的是相对路径：各自拼出
    id → 路径后做差集，得到新增与已删除；两侧都存在的路径再按字节数相减，
    得到变大与变小。四组结果都把自己和各级祖先一并收进去。

    收入祖先的原因：目录的大小是递归汇总的，深层文件的变化不会让目录自身
    的 kind 跟着变。筛「新增/已删除」时若不带上祖先，整层目录会被滤空，
    用户看不到任何入口。变大/变小同理，而且更容易被忽略——一个目录整体
    变小，不代表它下面没有变大的文件，只看目录自身的净变化量会把这类
    目录整条滤掉，用户永远打不开去看里面的变化。

    一次读两侧各一遍，四组结果共用同一次遍历。
    """
    old = open_readonly(old_db)
    new = open_readonly(new_db)
    try:
        old_parents, old_path_ids, old_sizes = _read_side_path_ids(old, source)
        new_parents, added_ids, grew_ids, shrank_ids = _read_side_changes(
            new, old_path_ids, old_sizes, source
        )
        removed_values = old_path_ids.values()
    finally:
        old.close()
        new.close()
    return (
        _propagate_ancestors(added_ids, new_parents),
        _propagate_ancestors(removed_values, old_parents),
        _propagate_ancestors(grew_ids, new_parents),
        _propagate_ancestors(shrank_ids, new_parents),
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


def _size_of(row: "_SideRow | None", source: str) -> int | None:
    """按所选来源取一条侧边行的大小。

    ``source`` 为 ``actual`` 时优先用实际占用，该行没采到这一格就回落到
    逻辑占用；为 ``logical`` 时直接用逻辑占用。回落是刻意的：旧快照与
    普通目录遍历都没有实际占用，不回落就只能全部显示为空。
    """
    if row is None:
        return None
    if source == "actual":
        a = row[6]
        if a is not None:
            return int(a)
    return int(row[1])


# 搜索候选：(父路径, 名字, 旧侧行, 新侧行)；
# 行为 (id, size, is_dir, mtime, reparse_tag, link_target, actual_size)
# actual_size 为 None 表示这份快照没记这一格（旧版本或非快速扫描路径）。
_SideRow = tuple[int, int, int, int, int, str, "int | None"]
_SearchEntry = tuple[str, str, "_SideRow | None", "_SideRow | None"]

def _conn_version(conn: sqlite3.Connection) -> int:
    """连接对应的快照格式版本（open_readonly 已挂载；缺省最新版）。"""
    return int(getattr(conn, "_wmc_version", SNAPSHOT_FORMAT_VERSION))


def _side_cols(conn: sqlite3.Connection) -> str:
    """按版本返回侧边行的 SELECT 列清单，宽度统一为八列。

    顺序是 name、id、size、is_dir、mtime、reparse_tag、link_target、
    actual_size。旧版本缺的尾部列由查询直接给出 NULL 字面量，
    调用方固定元数解包，不需要逐行判断缺了哪几格。
    """
    ver = _conn_version(conn)
    if ver >= 6:
        return (
            "name, id, size, is_dir, mtime, reparse_tag, link_target, actual_size"
        )
    if ver >= 5:
        return "name, id, size, is_dir, mtime, reparse_tag, link_target, NULL"
    if ver >= 4:
        return "name, id, size, is_dir, mtime, reparse_tag, NULL, NULL"
    return "name, id, size, is_dir, mtime, NULL, NULL, NULL"


def _row_cols(conn: sqlite3.Connection) -> str:
    """``id, parent_id, name, size, is_dir, mtime[, reparse_tag]`` —— 按连接版本分派。"""
    return entry_columns(_conn_version(conn))

# 与前端 SORT_OPTIONS 对齐；未知值回退 delta-desc
_SEARCH_SORT_KEYS = frozenset({
    "delta-desc", "size-desc", "pct-desc", "name-asc", "name-desc", "mtime-desc",
})


