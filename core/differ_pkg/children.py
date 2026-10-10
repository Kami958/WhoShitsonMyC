from __future__ import annotations

from __future__ import annotations

import os
import sqlite3


from .classify import _SearchEntry, _SideRow, _child_path, _side_cols

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
) -> dict[str, _SideRow]:
    """取某父目录下直接子节点：``{name: (id, size, is_dir, mtime, reparse_tag, link_target)}``。

    ``parent_id=None``（该侧没有此目录）时返回空。
    """
    if parent_id is None:
        return {}
    cur = conn.execute(
        f"SELECT {_side_cols(conn)} FROM entries WHERE parent_id = ?",
        (parent_id,),
    )
    out: dict[str, _SideRow] = {}
    for name, eid, size, is_dir, mtime, *rest in cur:
        out[name] = (
            eid, size, is_dir, mtime,
            int(rest[0]) if rest else 0,
            str(rest[1]) if len(rest) > 1 else "",
        )
    return out


def _children_count(conn: sqlite3.Connection, parent_id: int | None) -> int:
    """某父目录下的直接子节点数；parent_id 为 None 时返回 0。"""
    if parent_id is None:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE parent_id = ?", (parent_id,)
    ).fetchone()
    return int(row[0]) if row else 0


def _merge_child_nodes(
    conn_a: sqlite3.Connection,
    parent_a: int | None,
    conn_b: sqlite3.Connection,
    parent_b: int | None,
    make_node,
) -> list:
    """合并两侧直接子节点，只保留较小一侧的完整字典，另一侧流式处理。

    ``make_node(name, old_row, new_row)`` 返回节点或 None；行形状与
    :func:`_children_map` 一致。两侧各自独立分配 id，因此只能按名字合并。
    """
    count_a = _children_count(conn_a, parent_a)
    count_b = _children_count(conn_b, parent_b)
    nodes: list = []

    if count_a <= count_b:
        base_rows = _children_map(conn_a, parent_a)
        if parent_b is not None:
            cur = conn_b.execute(
                f"SELECT {_side_cols(conn_b)} FROM entries "
                "WHERE parent_id = ?",
                (parent_b,),
            )
            for name, eid, size, is_dir, mtime, *rest in cur:
                other_row = (
                    eid, size, is_dir, mtime,
                    int(rest[0]) if rest else 0,
                    str(rest[1]) if len(rest) > 1 else "",
                )
                base_row = base_rows.pop(name, None)
                node = make_node(name, base_row, other_row)
                if node is not None:
                    nodes.append(node)
        for name, base_row in base_rows.items():
            node = make_node(name, base_row, None)
            if node is not None:
                nodes.append(node)
        return nodes

    base_rows = _children_map(conn_b, parent_b)
    if parent_a is not None:
        cur = conn_a.execute(
            f"SELECT {_side_cols(conn_a)} FROM entries "
            "WHERE parent_id = ?",
            (parent_a,),
        )
        for name, eid, size, is_dir, mtime, *rest in cur:
            other_row = (
                eid, size, is_dir, mtime,
                int(rest[0]) if rest else 0,
                str(rest[1]) if len(rest) > 1 else "",
            )
            base_row = base_rows.pop(name, None)
            node = make_node(name, other_row, base_row)
            if node is not None:
                nodes.append(node)
    for name, base_row in base_rows.items():
        node = make_node(name, None, base_row)
        if node is not None:
            nodes.append(node)
    return nodes


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


def _like_contains(query: str) -> str:
    """把用户关键词编成 ``LIKE`` 模式（含转义），匹配「包含」。"""
    esc = (
        query.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{esc}%"


def _entry_matches(
    entry: _SearchEntry,
    norm_q: str,
    *,
    case_sensitive: bool,
    exact: bool,
) -> bool:
    """在最宽候选上应用区分大小写 / 严格匹配（二者都是收窄）。"""
    parent, name, _o, _n = entry
    path = _child_path(parent, name)
    path_mode = os.sep in norm_q.strip(os.sep)
    target = path if path_mode else name
    if exact:
        if case_sensitive:
            return target == norm_q
        return target.casefold() == norm_q.casefold()
    if case_sensitive:
        return norm_q in target
    return norm_q.casefold() in target.casefold()


