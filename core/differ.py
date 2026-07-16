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
import sys
import threading
import time
from array import array
from bisect import bisect_left, bisect_right

from .i18n import t
from . import applog
from .models import ChangeKind, DiffNode, SnapshotMeta
from .snapshot import SnapshotError, open_readonly, read_meta


class DiffError(Exception):
    """对比相关错误。"""


class SearchCancelled(DiffError):
    """搜索被用户取消。"""

    def __init__(self) -> None:
        super().__init__(t("搜索已取消", "Search cancelled"))


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


# 搜索候选：(父路径, 名字, 旧侧行, 新侧行)；行为 (id, size, is_dir, mtime)
_SideRow = tuple[int, int, int, int]
_SearchEntry = tuple[str, str, "_SideRow | None", "_SideRow | None"]

# 与前端 SORT_OPTIONS 对齐；未知值回退 delta-desc
_SEARCH_SORT_KEYS = frozenset({
    "delta-desc", "pct-desc", "name-asc", "name-desc", "mtime-desc",
})


def _entry_delta(entry: _SearchEntry) -> int:
    _parent, _name, o, n = entry
    return (n[1] if n else 0) - (o[1] if o else 0)


def _entry_old_size(entry: _SearchEntry) -> int:
    o = entry[2]
    return o[1] if o else 0


def _entry_mtime(entry: _SearchEntry) -> int:
    _parent, _name, o, n = entry
    if n is not None:
        return n[3]
    if o is not None:
        return o[3]
    return 0


def _entry_pct(entry: _SearchEntry) -> float:
    """|delta| / 旧大小；旧不存在且有变化视为无穷大（与前端 deltaPct 一致）。"""
    delta = _entry_delta(entry)
    old = _entry_old_size(entry)
    if old > 0:
        return abs(delta) / old
    return float("inf") if delta != 0 else 0.0


def _sort_search_entries(
    entries: list[_SearchEntry], sort: str
) -> list[_SearchEntry]:
    """按与变化树相同的规则排序搜索候选（稳定次键用路径）。"""
    if not entries:
        return entries
    if sort == "name-asc":
        return sorted(entries, key=lambda e: (e[1].casefold(), _child_path(e[0], e[1])))
    if sort == "name-desc":
        return sorted(
            entries,
            key=lambda e: (e[1].casefold(), _child_path(e[0], e[1])),
            reverse=True,
        )
    if sort == "mtime-desc":
        return sorted(
            entries,
            key=lambda e: (-_entry_mtime(e), _child_path(e[0], e[1])),
        )
    if sort == "pct-desc":
        return sorted(
            entries,
            key=lambda e: (
                -_entry_pct(e),
                -abs(_entry_delta(e)),
                _child_path(e[0], e[1]),
            ),
        )
    # delta-desc 默认
    return sorted(
        entries,
        key=lambda e: (-abs(_entry_delta(e)), _child_path(e[0], e[1])),
    )


class _SideIndex:
    """一侧快照的常驻内存搜索索引（Everything 式）。

    全部名字 casefold 后用 ``\\0`` 拼成一整块字符串，子串搜索走 C 速度的
    ``str.find``；命中位置经 ``starts`` 二分映射回条目下标。原始名字同样
    拼成一块按需切片，避免几百万个小字符串对象的开销。父子关系存条目 id，
    查父行时在有序 ``ids`` 上二分，不建 id→下标的大字典。
    """

    __slots__ = (
        "ids", "parents", "sizes", "mtimes", "dirs",
        "blob", "starts", "orig_blob", "orig_starts", "_path_cache",
    )

    def __init__(self) -> None:
        self.ids = array("q")
        self.parents = array("q")   # 父行 id；根行记 -1
        self.sizes = array("q")
        self.mtimes = array("q")
        self.dirs = bytearray()
        self.blob = ""
        self.starts = array("q")
        self.orig_blob = ""
        self.orig_starts = array("q")
        self._path_cache: dict[int, str | None] = {}

    @classmethod
    def build(
        cls, conn: sqlite3.Connection, stop: threading.Event
    ) -> "_SideIndex | None":
        """全表顺序读一遍建索引；``stop`` 置位时尽快放弃返回 None。"""
        idx = cls()
        names: list[str] = []
        cur = conn.execute(
            "SELECT id, parent_id, name, size, is_dir, mtime "
            "FROM entries ORDER BY id"
        )
        while True:
            if stop.is_set():
                return None
            rows = cur.fetchmany(50000)
            if not rows:
                break
            # 按列批量 extend（C 级），比逐行 append 快数倍
            ids, pids, nms, sizes, dirs, mtimes = zip(*rows)
            idx.ids.extend(ids)
            idx.parents.extend(
                -1 if p is None else p for p in pids
            )
            idx.sizes.extend(sizes)
            idx.mtimes.extend(mtimes)
            idx.dirs.extend(1 if d else 0 for d in dirs)
            names.extend(nm or "" for nm in nms)
        if stop.is_set():
            return None
        # 两块 blob 各带累计起点表（casefold 可能改变长度，起点表分开记）
        pos = 0
        for nm in names:
            idx.orig_starts.append(pos)
            pos += len(nm) + 1
        idx.orig_starts.append(pos)
        idx.orig_blob = "\0".join(names) + "\0"
        folded = [nm.casefold() for nm in names]
        pos = 0
        for nm in folded:
            idx.starts.append(pos)
            pos += len(nm) + 1
        idx.starts.append(pos)
        idx.blob = "\0".join(folded) + "\0"
        return idx

    def __len__(self) -> int:
        return len(self.ids)

    def name_at(self, i: int) -> str:
        return self.orig_blob[self.orig_starts[i] : self.orig_starts[i + 1] - 1]

    def _index_of_id(self, eid: int) -> int | None:
        i = bisect_left(self.ids, eid)
        if i < len(self.ids) and self.ids[i] == eid:
            return i
        return None

    def find(self, key_cf: str, limit: int) -> list[int]:
        """名字（casefold 后）包含 ``key_cf`` 的条目下标，最多 ``limit`` 个。"""
        if not key_cf or "\0" in key_cf:
            return []
        out: list[int] = []
        blob = self.blob
        starts = self.starts
        pos = 0
        while len(out) < limit:
            hit = blob.find(key_cf, pos)
            if hit < 0:
                break
            i = bisect_right(starts, hit) - 1
            out.append(i)
            pos = starts[i + 1]  # 跳到下一个名字，同名内多次命中只记一次
        return out

    def path_at(self, i: int) -> str | None:
        """条目下标 → 相对扫描根的路径；根行返回 ""（调用方按假值跳过）。"""
        eid = self.ids[i]
        cached = self._path_cache.get(eid)
        if cached is not None or eid in self._path_cache:
            return cached
        parts: list[str] = []
        seen: set[int] = set()
        cur: int | None = i
        result: str | None = None
        while True:
            cur_id = self.ids[cur]
            if cur_id in seen:
                break  # 环：数据异常，放弃
            seen.add(cur_id)
            pid = self.parents[cur]
            if pid == -1:
                parts.reverse()
                result = os.sep.join(parts)
                break
            parts.append(self.name_at(cur))
            nxt = self._index_of_id(pid)
            if nxt is None:
                break  # 父行缺失：数据异常，放弃
            cur = nxt
        self._path_cache[eid] = result
        return result

    def row_at(self, i: int) -> _SideRow:
        return (
            int(self.ids[i]), int(self.sizes[i]),
            int(self.dirs[i]), int(self.mtimes[i]),
        )

    def approx_bytes(self) -> int:
        """索引常驻内存的粗略字节数（供日志排查用）。"""
        total = sys.getsizeof(self.blob) + sys.getsizeof(self.orig_blob)
        for arr in (self.ids, self.parents, self.sizes, self.mtimes,
                    self.starts, self.orig_starts):
            total += len(arr) * arr.itemsize
        total += len(self.dirs)
        return total


def _hits_from_index(
    side: _SideIndex,
    name_key: str,
    fetch_n: int,
    *,
    cancel: threading.Event | None = None,
) -> dict[str, _SideRow]:
    """内存索引版命中收集，返回值形状与 ``_hits_with_paths`` 一致。"""
    hits: dict[str, _SideRow] = {}
    idxs = side.find(name_key.casefold(), fetch_n)
    for n, i in enumerate(idxs):
        if cancel is not None and n % 2048 == 0 and cancel.is_set():
            raise SearchCancelled()
        path = side.path_at(i)
        if path:
            hits[path] = side.row_at(i)
    return hits


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
        # 搜索结果缓存：(关键词 casefold, 最宽候选, {(选项,排序): 排好的列表})。
        # 区分大小写/严格匹配是候选的子集，只在内存过滤，不另查库。
        # 快照只读，会话内无需失效；翻页/换排序直接复用排好的列表。
        self._search_cache: tuple[str, list[_SearchEntry]] | None = None
        self._search_sorted: dict[
            tuple[str, bool, bool, str], list[_SearchEntry]
        ] = {}
        # 搜索取消：置位后批处理循环尽快退出；interrupt() 打断执行中的 SQL
        self._cancel_evt = threading.Event()
        # 内存搜索索引（Everything 式）：后台预热，就绪前搜索走 SQL 兜底。
        # 预热线程用自己的连接（SQLite 连接不能跨线程并发），路径留一份。
        self._db_paths = (old_db, new_db)
        self._sides: tuple[_SideIndex, _SideIndex] | None = None
        self._preheat_thread: threading.Thread | None = None
        self._preheat_stop = threading.Event()
        # 可选：状态回调，供 UI 显示「准备中 / 已就绪」；在预热线程里调用
        self._preheat_on_status = None

    def start_search_preheat(self, on_status=None) -> None:
        """后台把两侧条目读进内存建搜索索引；重复调用是空操作。

        就绪后子串搜索不再全表扫库（首搜也快）；建索引期间的搜索
        自动走原有 SQL 路径。

        Args:
            on_status: 可选 ``callable(dict)``。推送
                ``{"status": "started"|"ready"|"failed"|"aborted", ...}``。
        """
        # 已就绪：无需再预热
        if self._sides is not None:
            if on_status is not None:
                self._preheat_on_status = on_status
                self._report_preheat("ready")
            return
        # 线程仍在跑：只更新回调，不重复开线程
        th = self._preheat_thread
        if th is not None and th.is_alive():
            if on_status is not None:
                self._preheat_on_status = on_status
            return
        # 未就绪且无线程 / 上次失败已结束：允许重新预热
        self._preheat_stop.clear()
        self._preheat_on_status = on_status
        self._preheat_thread = threading.Thread(
            target=self._preheat_worker, name="search-preheat", daemon=True
        )
        self._preheat_thread.start()

    def _report_preheat(self, status: str, **extra) -> None:
        """向 UI 回调推送预热状态；回调异常不影响预热本身。"""
        cb = self._preheat_on_status
        if cb is None:
            return
        payload = {"status": status, **extra}
        try:
            cb(payload)
        except Exception:  # noqa: BLE001
            pass

    def _preheat_worker(self) -> None:
        conns: list[sqlite3.Connection] = []
        t0 = time.perf_counter()
        applog.debug("search preheat started")
        self._report_preheat("started")
        try:
            built: list[_SideIndex] = []
            for path in self._db_paths:
                if self._preheat_stop.is_set():
                    applog.debug("search preheat aborted (session closing)")
                    self._report_preheat("aborted")
                    return
                conn = open_readonly(path)
                conns.append(conn)
                side = _SideIndex.build(conn, self._preheat_stop)
                if side is None:
                    applog.debug("search preheat aborted (session closing)")
                    self._report_preheat("aborted")
                    return
                built.append(side)
            self._sides = (built[0], built[1])
            total_entries = len(built[0]) + len(built[1])
            total_mb = (built[0].approx_bytes() + built[1].approx_bytes()) / 1e6
            elapsed_s = time.perf_counter() - t0
            applog.info(
                f"search preheat done: {total_entries} entries "
                f"(old {len(built[0])} / new {len(built[1])}), "
                f"index ~{total_mb:.0f}MB, {elapsed_s:.1f}s"
            )
            self._report_preheat(
                "ready",
                entries=int(total_entries),
                index_mb=round(total_mb, 1),
                elapsed_s=round(elapsed_s, 2),
            )
        except MemoryError as exc:
            # 内存不足：放弃索引，搜索继续走 SQL 路径
            applog.error(
                "search preheat out of memory, falling back to SQL search",
                exc=exc,
            )
            self._report_preheat("failed", reason="oom")
        except Exception as exc:  # noqa: BLE001 - 预热失败静默回退 SQL 路径
            applog.exception("search preheat failed, falling back to SQL search", exc)
            self._report_preheat("failed", reason="error")
        finally:
            for conn in conns:
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover
                    pass

    def wait_search_preheat(self, timeout: float | None = None) -> bool:
        """等预热线程结束（测试用）；返回索引是否就绪。"""
        th = self._preheat_thread
        if th is not None:
            th.join(timeout)
        return self._sides is not None

    def search_preheat_status(self) -> str:
        """当前预热状态：ready / started / idle（供 UI 补推）。"""
        if self._sides is not None:
            return "ready"
        th = self._preheat_thread
        if th is not None and th.is_alive():
            return "started"
        return "idle"

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
            node = self._node_from_sides(parent, name, old_rows.get(name), new_rows.get(name))
            if node is not None:
                nodes.append(node)

        if sort:
            nodes.sort(key=lambda d: abs(d.delta), reverse=True)
        return nodes

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
            sort: 排序方式，与前端一致：``delta-desc`` / ``pct-desc`` /
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
                    "SELECT name, id, size, is_dir, mtime FROM entries "
                    f"WHERE parent_id = ? AND name IN ({marks})",
                    [pid, *chunk],
                )
                for name, eid, size, is_dir, mtime in cur:
                    hits[_child_path(parent, name)] = (
                        int(eid), int(size), int(is_dir), int(mtime)
                    )

    def _node_from_sides(
        self,
        parent: str,
        name: str,
        o: tuple[int, int, int, int] | None,
        n: tuple[int, int, int, int] | None,
    ) -> DiffNode | None:
        """由两侧子行 ``(id, size, is_dir, mtime)`` 合成 :class:`DiffNode`。"""
        if o is None and n is None:
            return None
        path = _child_path(parent, name)
        is_dir = bool((o or n)[2])
        incomparable = path in self._skipped
        old_size, new_size, delta, kind = _classify(
            o[1] if o else None,
            n[1] if n else None,
            incomparable,
        )
        mtime = n[3] if n is not None else (o[3] if o is not None else 0)

        if is_dir:
            ids = (o[0] if o else None, n[0] if n else None)
            self._dir_ids[path] = ids
            has_children = _has_children(self._old, ids[0]) or _has_children(
                self._new, ids[1]
            )
        else:
            has_children = False

        return DiffNode(
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
        """关闭两份快照连接；停掉未完成的预热线程并等其释放连接。"""
        self._preheat_stop.set()
        th = self._preheat_thread
        if th is not None and th.is_alive():
            th.join()
        self._sides = None
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


def _hits_with_paths(
    conn: sqlite3.Connection,
    name_key: str,
    fetch_n: int,
    *,
    cancel: threading.Event | None = None,
) -> dict[str, _SideRow]:
    """名字包含 ``name_key`` 的条目及其相对路径：``{path: (id, size, is_dir, mtime)}``。

    最宽匹配：``LIKE`` 包含、ASCII 不区分大小写。更严的选项在内存过滤。

    命中行一次 ``LIKE`` 取回，祖先行按层批量补齐（每层一批 ``IN`` 查询），
    避免逐条回表。最多取 ``fetch_n`` 条命中。``cancel`` 置位时尽快抛
    :class:`SearchCancelled`（执行中的 SQL 由 ``interrupt()`` 打断）。
    """
    rows = conn.execute(
        "SELECT id, parent_id, name, size, is_dir, mtime FROM entries "
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
    for eid, _pid, _name, size, is_dir, mtime in rows:
        path = _lineage_path(lineage, int(eid))
        if path:
            hits[path] = (int(eid), int(size), int(is_dir), int(mtime))
    return hits


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
    with Diff(old_db, new_db) as diff:
        return diff.compare_children("")
