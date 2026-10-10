from __future__ import annotations

from __future__ import annotations

import sqlite3
import threading


from .children import _like_contains
from .classify import _SideRow, _row_cols
from .errors import SearchCancelled
from .links import _lineage_path

def _hits_with_paths(
    conn: sqlite3.Connection,
    name_key: str,
    fetch_n: int,
    *,
    cancel: threading.Event | None = None,
) -> dict[str, _SideRow]:
    """名字包含 ``name_key`` 的条目及其相对路径：``{path: (id, size, is_dir, mtime, reparse_tag, link_target)}``。

    最宽匹配：``LIKE`` 包含、ASCII 不区分大小写。更严的选项在内存过滤。

    命中行一次 ``LIKE`` 取回，祖先行按层批量补齐（每层一批 ``IN`` 查询），
    避免逐条回表。最多取 ``fetch_n`` 条命中。``cancel`` 置位时尽快抛
    :class:`SearchCancelled`（执行中的 SQL 由 ``interrupt()`` 打断）。
    """
    rows = conn.execute(
        f"SELECT {_row_cols(conn)} FROM entries "
        "WHERE parent_id IS NOT NULL AND name LIKE ? ESCAPE '\\' "
        "LIMIT ?",
        (_like_contains(name_key), int(fetch_n)),
    ).fetchall()
    if not rows:
        return {}

    # 谱系表 {id: (parent_id, name)}：先放命中行，再逐层补缺失的祖先
    lineage: dict[int, tuple[int | None, str]] = {
        int(r[0]): (r[1], r[2] or "") for r in rows
    }
    pending = {
        int(r[1]) for r in rows
        if r[1] is not None and int(r[1]) not in lineage
    }
    while pending:
        if cancel is not None and cancel.is_set():
            raise SearchCancelled()
        batch = list(pending)
        pending = set()
        for i in range(0, len(batch), 900):
            chunk = batch[i : i + 900]
            marks = ",".join("?" * len(chunk))
            cur = conn.execute(
                "SELECT id, parent_id, name FROM entries "
                f"WHERE id IN ({marks})",
                chunk,
            )
            for eid, pid, nm in cur:
                lineage[int(eid)] = (pid, nm or "")
                if pid is not None:
                    pending.add(int(pid))
        pending -= lineage.keys()

    hits: dict[str, _SideRow] = {}
    for eid, _pid, _name, size, is_dir, mtime, *rest in rows:
        path = _lineage_path(lineage, int(eid))
        if path:
            hits[path] = (
                int(eid), int(size), int(is_dir), int(mtime),
                int(rest[0]) if rest else 0,
                str(rest[1]) if len(rest) > 1 else "",
            )
    return hits


