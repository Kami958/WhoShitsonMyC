from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterable

from ..link_match import (
    abs_under_root,
    is_abs_target,
    link_candidate,
    link_norm,
    target_matches,
)
from ..models import (
    REPARSE_TAG_MOUNT_POINT,
    REPARSE_TAG_SYMLINK,
    DiffNode,
)

from .errors import SearchCancelled

def _lineage_for_ids(
    conn: sqlite3.Connection, ids: Iterable[int], *, chunk: int = 900
) -> dict[int, str]:
    """给一批条目 id，逐层批量取祖先，返回 ``{id: 相对扫描根的路径}``。

    祖先按层用 ``IN`` 批量补齐，避免逐条回表；父链断裂（缺祖先行）的 id
    不出现在结果里。路径拼接复用 :func:`_lineage_path`。
    """
    wanted = {int(i) for i in ids}
    if not wanted:
        return {}
    lineage: dict[int, tuple[int | None, str]] = {}
    pending = set(wanted)
    while pending:
        batch = list(pending)
        pending = set()
        for i in range(0, len(batch), chunk):
            part = batch[i : i + chunk]
            marks = ",".join("?" * len(part))
            cur = conn.execute(
                "SELECT id, parent_id, name FROM entries "
                f"WHERE id IN ({marks})",
                part,
            )
            for eid, pid, name in cur:
                lineage[int(eid)] = (pid, name or "")
                if pid is not None and int(pid) not in lineage:
                    pending.add(int(pid))
        pending -= lineage.keys()
    out: dict[int, str] = {}
    for eid in wanted:
        path = _lineage_path(lineage, eid)
        if path is not None:
            out[eid] = path
    return out


def _strip_root(path_abs: str, root: str) -> str | None:
    """绝对路径相对扫描根的部分；不在根下返回 None。

    比较时大小写不敏感，但返回的片段保留原始大小写，界面才能照磁盘上的
    写法显示。
    """
    r = (root or "").strip().rstrip("/\\")
    if not r:
        return None
    n_path = link_norm(path_abs)
    n_root = link_norm(r)
    if n_path == n_root:
        return ""
    if n_path.startswith(n_root + "\\"):
        return path_abs[len(r):].lstrip("/\\")
    return None


# 反链查询读候选行的 SQL。``reparse_tag <> 0`` 不筛掉任何行（``IN`` 里两个
# 标签都非零），它在语句里是为了让 SQLite 认得出 core.snapshot 建的
# idx_entries_link 是部分索引：部分索引只在查询条件里出现同一个条件时才被
# 采用，去掉这一句就退回整表扫描。改这里之前先看
# tests/test_differ.py::test_links_query_uses_link_index。
_LINK_CANDIDATE_SQL = (
    "SELECT id, reparse_tag, link_target FROM entries "
    "WHERE reparse_tag IN (?, ?) "
    "AND link_target IS NOT NULL AND link_target <> '' "
    "AND reparse_tag <> 0"
)

# 相对目标分批展开谱系时的批大小：一批算完只留命中的路径，峰值内存不随
# 候选条数增长。
_LINK_REL_BATCH = 4096


def _links_into_target(
    conn: sqlite3.Connection,
    root: str,
    target: str,
    *,
    cancel: threading.Event | None = None,
) -> tuple[list[dict], list[dict]]:
    """在一份快照里找指向 ``target`` 的链接。

    绝对目标直接比；相对目标按链接自己所在目录展开后再比。

    Args:
        conn: 该快照的只读连接。
        root: 该快照的扫描根；用来把链接自身的位置拼成绝对路径，也用来
            展开写成 ``..\\foo`` 的相对目标。
        target: 已归一的绝对目标路径（:func:`core.link_match.link_norm` 的结果）。
        cancel: 置位后尽快抛 :class:`SearchCancelled`。

    Returns:
        ``(hits, unresolved)``。``hits`` 每项为
        ``{"path": 链接自身的绝对路径, "kind": 联接/符号链接, "target": 原始目标}``，
        按路径排序。``unresolved`` 每项多一个 ``reason``：``relative``
        相对目标但拼不出链接自身位置、``broken`` 父链断裂。
    """
    # 候选判定（标签、空目标、卷 GUID）与目标比较都走 core.link_match，
    # 与扫盘时边扫边判用的是同一套规则。
    # 逐行迭代游标，不一次性读进内存：整盘扫出来的候选能到十万条量级。
    # 循环体内不发 SQL，与正在迭代的游标不冲突。
    rows = conn.execute(
        _LINK_CANDIDATE_SQL,
        (REPARSE_TAG_MOUNT_POINT, REPARSE_TAG_SYMLINK),
    )

    # eid → (联接/符号链接, 原始目标, 链接自身相对扫描根的路径)。路径要先
    # 算谱系才知道，命中前先留 None。
    hits: dict[int, tuple[str, str, str | None]] = {}
    unresolved: dict[int, tuple[str, str, str]] = {}
    rel_rows: list[tuple[int, str, str, str]] = []

    for n, (eid, tag, raw) in enumerate(rows):
        if cancel is not None and n % 2048 == 0 and cancel.is_set():
            raise SearchCancelled()
        eid = int(eid)
        raw = str(raw or "")
        cand = link_candidate(int(tag), raw)
        if cand is None:
            continue
        kind, t = cand
        if is_abs_target(t):
            # 绝对目标不用知道链接自己在哪就能比
            if target_matches("", t, target):
                hits[eid] = (kind, raw, None)
            continue
        rel_rows.append((eid, kind, raw, t))

    # 相对目标按链接自身所在目录展开：mklink 建的相对符号链接目标是
    # ``..\foo`` 这种形态，不展开会全部漏报。整盘的相对链接能到几万条，
    # 分批展开，每批只把命中的那些路径留下来。
    for start in range(0, len(rel_rows), _LINK_REL_BATCH):
        batch = rel_rows[start : start + _LINK_REL_BATCH]
        batch_paths = _lineage_for_ids(conn, [r[0] for r in batch])
        for eid, kind, raw, t in batch:
            link_rel = batch_paths.get(eid)
            if link_rel is None:
                unresolved[eid] = (kind, raw, "relative")
                continue
            if target_matches(abs_under_root(root, link_rel), t, target):
                hits[eid] = (kind, raw, link_rel)

    # 只有绝对目标的命中还没算过链接自身的位置，一次查完；这类命中的条数
    # 就是结果条数，不跟着候选总数走。
    need = [eid for eid, (_k, _raw, rel) in hits.items() if rel is None]
    if need:
        resolved = _lineage_for_ids(conn, need)
        for eid in need:
            kind, raw, _rel = hits[eid]
            link_rel = resolved.get(eid)
            if link_rel is None:
                hits.pop(eid)
                unresolved[eid] = (kind, raw, "broken")
                continue
            hits[eid] = (kind, raw, link_rel)

    out: list[dict] = []
    for kind, raw, link_rel in hits.values():
        # 取不到自身位置的命中已在上面移进 unresolved
        assert link_rel is not None
        out.append({
            "path": abs_under_root(root, link_rel),
            "kind": kind,
            "target": raw,
        })
    out.sort(key=lambda it: it["path"].casefold())

    # 这些条目的共同点是拼不出自己在快照里的位置，没有路径可显示
    unresolved_out = [
        {"path": "", "kind": kind, "target": raw, "reason": reason}
        for kind, raw, reason in unresolved.values()
    ]
    unresolved_out.sort(key=lambda it: it["target"].casefold())
    return out, unresolved_out


def _lineage_path(
    lineage: dict[int, tuple[int | None, str]], eid: int
) -> str | None:
    """在内存谱系表中沿 parent_id 拼出相对扫描根的路径（根行名字不计入）。"""
    parts: list[str] = []
    seen: set[int] = set()
    cur: int | None = eid
    while cur is not None:
        if cur in seen:
            return None
        seen.add(cur)
        got = lineage.get(cur)
        if got is None:
            return None
        parent_id, name = got
        if parent_id is None:
            break
        parts.append(name)
        cur = int(parent_id)
    parts.reverse()
    return os.sep.join(parts)


def _split_parent(path: str) -> tuple[str, str]:
    """``(parent_rel, name)``；顶层文件 parent 为 ``""``。"""
    path = path.replace("/", os.sep).replace("\\", os.sep)
    if os.sep not in path:
        return "", path
    parent, name = path.rsplit(os.sep, 1)
    return parent, name


def compare_snapshots(old_db: str, new_db: str) -> list[DiffNode]:
    """便捷函数：打开两份快照并返回顶层对比结果。

    仅取顶层；下钻请用 :class:`Diff` 的 :meth:`~Diff.compare_children`。
    """
    from .diff import Diff
    with Diff(old_db, new_db) as diff:
        return diff.compare_children("")
