"""按名字搜索：取消、分批取条目、按侧填行。"""

from __future__ import annotations

from .errors import DiffError, SearchCancelled
from ..models import DiffNode
from .classify import _SEARCH_SORT_KEYS, _SearchEntry, _child_path, _side_cols
from .children import _entry_matches
from .searchhits import _hits_from_index
from .hitspaths import _hits_with_paths
from .sortnames import _sort_search_entries
from .links import _split_parent
import os
import sqlite3


class DiffSearchMixin:

    def search_by_name(
        self,
        query: str,
        *,
        limit: int = 50,
        offset: int = 0,
        sort: str = "delta-desc",
        case_sensitive: bool = False,
        exact: bool = False,
    ) -> tuple[list[DiffNode], int]:
        """按名称（及路径子串）在两份快照中搜索，返回对比节点。

        - 默认：文件/文件夹 **名字** 包含关键词（SQLite ``LIKE``，ASCII 不区分大小写）
        - ``exact`` / ``case_sensitive``：在最宽候选上内存过滤（是子集，不重查库）
        - 关键词含路径分隔符时：按最后一段搜名字，再要求完整相对路径包含该关键词
        - 两侧去重；结果按 ``sort`` 排序后再 ``offset/limit`` 分页
        - 第二个返回值为命中路径总数（可大于本页条数）

        性能：最宽候选集批量查询一次并按关键词缓存；切换匹配选项只过滤+排序切片，
        :class:`DiffNode`（含 has_children 探测）只对本页构建。

        Args:
            query: 搜索关键词；去空白后为空则返回空结果。
            limit: 本页最多条数，范围 1–200，默认 50。
            offset: 跳过前多少条，从 0 起。
            sort: 排序方式，与前端一致：``delta-desc`` / ``size-desc`` / ``pct-desc`` /
                ``name-asc`` / ``name-desc`` / ``mtime-desc``。
            case_sensitive: 是否区分大小写，默认否。
            exact: 是否严格整名匹配，默认否（包含匹配）。
        """
        q = (query or "").strip()
        if not q:
            return [], 0
        lim = max(1, min(int(limit), 200))
        off = max(0, int(offset))
        sort_key = sort if sort in _SEARCH_SORT_KEYS else "delta-desc"
        cs = bool(case_sensitive)
        ex = bool(exact)

        norm_q = q.replace("/", os.sep).replace("\\", os.sep)
        # 新一轮搜索开始：清掉上一轮遗留的取消标记
        self._cancel_evt.clear()
        try:
            # 始终缓存最宽匹配；选项是子集过滤
            cache_key = norm_q.casefold()
            if self._search_cache is not None and self._search_cache[0] == cache_key:
                entries = self._search_cache[1]
            else:
                entries = self._search_entries(norm_q)
                self._search_cache = (cache_key, entries)
                self._search_sorted = {}

            # 区分大小写时不同大小写的关键词过滤结果不同，键须带原词
            sorted_key = (norm_q if cs else cache_key, cs, ex, sort_key)
            ordered = self._search_sorted.get(sorted_key)
            if ordered is None:
                if cs or ex:
                    entries = [
                        e for e in entries
                        if _entry_matches(e, norm_q, case_sensitive=cs, exact=ex)
                    ]
                ordered = _sort_search_entries(entries, sort_key)
                self._search_sorted[sorted_key] = ordered

            total = len(ordered)
            nodes: list[DiffNode] = []
            for parent, name, o, n in ordered[off : off + lim]:
                node = self._node_from_sides(parent, name, o, n)
                if node is not None:
                    nodes.append(node)
            return nodes, total
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc).lower():
                raise SearchCancelled() from None
            raise

    def cancel_search(self) -> None:
        """取消正在进行的搜索（可从其他线程调用）。

        置位取消标记让批处理循环尽快退出，并用 SQLite ``interrupt()``
        打断正在执行的全表扫描；被打断的搜索抛 :class:`SearchCancelled`。
        """
        self._cancel_evt.set()
        for conn in (self._old, self._new):
            try:
                conn.interrupt()
            except sqlite3.Error:  # pragma: no cover - 连接已关闭等边界
                pass

    def _check_cancelled(self) -> None:
        if self._cancel_evt.is_set():
            raise SearchCancelled()

    def _search_entries(self, norm_q: str) -> list[_SearchEntry]:
        """算出一个关键词的最宽候选（不区分大小写、包含匹配；未排序）。

        每侧一次 ``LIKE`` 批量取命中行，再按层批量补祖先行拼路径；
        单侧命中的路径批量查另一侧是否存在，保证变化类型判断正确。
        """
        # 含分隔符时：用最后一段做 name 检索，再用整段过滤路径
        name_key = norm_q
        path_filter = None
        if os.sep in norm_q.strip(os.sep):
            parts = [p for p in norm_q.split(os.sep) if p]
            if not parts:
                return []
            name_key = parts[-1]
            path_filter = norm_q.casefold()

        # 候选上限：够分页展示，又避免全库扫爆
        fetch_n = 20000
        sides = self._sides
        if sides is not None:
            # 内存索引已就绪：不碰数据库，扫内存 blob
            old_hits = _hits_from_index(
                sides[0], name_key, fetch_n, cancel=self._cancel_evt
            )
            self._check_cancelled()
            new_hits = _hits_from_index(
                sides[1], name_key, fetch_n, cancel=self._cancel_evt
            )
        else:
            old_hits = _hits_with_paths(
                self._old, name_key, fetch_n, cancel=self._cancel_evt
            )
            self._check_cancelled()
            new_hits = _hits_with_paths(
                self._new, name_key, fetch_n, cancel=self._cancel_evt
            )
        self._check_cancelled()

        paths = set(old_hits) | set(new_hits)
        if path_filter is not None:
            paths = {p for p in paths if path_filter in p.casefold()}

        # 名字命中只落在一侧时，该路径在另一侧可能同样存在——补查，
        # 否则会把「两侧都有」误判成新增/删除
        self._fill_side(self._old, 0, old_hits, paths - old_hits.keys())
        self._fill_side(self._new, 1, new_hits, paths - new_hits.keys())

        entries: list[_SearchEntry] = []
        for path in paths:
            o = old_hits.get(path)
            n = new_hits.get(path)
            if o is None and n is None:
                continue
            parent, name = _split_parent(path)
            entries.append((parent, name, o, n))
        return entries

    def _fill_side(
        self,
        conn: sqlite3.Connection,
        side: int,
        hits: dict[str, tuple[int, int, int, int]],
        missing: set[str],
    ) -> None:
        """把 ``missing`` 中在该侧存在的路径批量补进 ``hits``。

        按父目录分组：父目录解析一次 id，子名字用 ``IN`` 一批查完。
        """
        if not missing:
            return
        by_parent: dict[str, list[str]] = {}
        for p in missing:
            parent, name = _split_parent(p)
            by_parent.setdefault(parent, []).append(name)
        for parent, names in by_parent.items():
            self._check_cancelled()
            try:
                pid = self._resolve_dir(parent)[side]
            except DiffError:
                continue
            if pid is None:
                continue
            for i in range(0, len(names), 500):
                chunk = names[i : i + 500]
                marks = ",".join("?" * len(chunk))
                cur = conn.execute(
                    f"SELECT {_side_cols(conn)} FROM entries "
                    f"WHERE parent_id = ? AND name IN ({marks})",
                    [pid, *chunk],
                )
                for name, eid, size, is_dir, mtime, *rest in cur:
                    hits[_child_path(parent, name)] = (
                        int(eid), int(size), int(is_dir), int(mtime),
                        int(rest[0]) if rest else 0,
                    )
