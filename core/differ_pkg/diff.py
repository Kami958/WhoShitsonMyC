from __future__ import annotations

import os
import sqlite3
import threading
import time

from ..i18n import t
from .. import applog
from ..compress import is_session_temp_db
from ..link_match import abs_under_root, link_norm
from ..models import DiffNode, SnapshotMeta
from ..snapshot import (
    LINK_INDEX_SQL,
    SnapshotError,
    open_readonly,
    read_meta,
)

from .children import _entry_matches, _find_dir_id, _has_children, _merge_child_nodes, _norm_root, _root_id
from .classify import _SEARCH_SORT_KEYS, _SearchEntry, _SideRow, _child_path, _classify, _conn_version, _propagate_ancestors, _read_side_link_ids, _side_cols, _subtree_mark_id_sets
from .errors import DiffError, DiffRootMismatch, SearchCancelled
from .hitspaths import _hits_with_paths
from .links import _links_into_target, _split_parent, _strip_root
from .searchhits import _hits_from_index
from .sideindex import _SideIndex
from .sortnames import _sort_search_entries
from .preheat import DiffPreheatMixin
from .search import DiffSearchMixin
from .nodes import DiffNodeMixin
from .linkops import DiffLinkMixin


class Diff(DiffPreheatMixin, DiffSearchMixin, DiffNodeMixin, DiffLinkMixin):
        """一次对比会话：持有两份快照的只读连接，按需逐层对比。

        用法::

            with Diff(old_db, new_db) as diff:
                top = diff.compare_children("")        # 顶层
                sub = diff.compare_children("Users")   # 展开某目录时再取

        记得用 ``with`` 或手动 :meth:`close` 释放数据库连接。
        """

        """一次对比会话：持有两份快照的只读连接，按需逐层对比。

        用法::

            with Diff(old_db, new_db) as diff:
                top = diff.compare_children("")        # 顶层
                sub = diff.compare_children("Users")   # 展开某目录时再取

        记得用 ``with`` 或手动 :meth:`close` 释放数据库连接。
        """

        def __init__(
            self, old_db: str, new_db: str, size_source: str | None = None
        ) -> None:
            """打开两份快照并校验根一致、格式为 v3。

            Args:
                old_db: 旧（基准）快照路径。
                new_db: 新（当前）快照路径。
                size_source: 大小取数口径，``actual`` 实际占用或 ``logical``
                    逻辑占用；不传则读当前设置。

            Raises:
                DiffRootMismatch: 两份快照的扫描根不同。
                SnapshotError: 任一快照无法读取、版本不符或格式过旧。
            """
            self.old_meta: SnapshotMeta = read_meta(old_db)
            self.new_meta: SnapshotMeta = read_meta(new_db)

            # 取数口径：显式传入优先，否则用当前设置。
            # 只有两侧都记录了实际占用才能用实际口径：一侧有、一侧没有时，
            # 两侧总量与差值会变成两种单位相减，得出无意义的数字。
            # 这种情况整场对比统一降级为逻辑占用，并在界面说明原因。
            if size_source is None:
                try:
                    from ..store import get_size_source

                    size_source = get_size_source()
                except Exception:
                    size_source = "actual"
            requested = str(size_source or "actual")
            both_recorded = bool(
                self.old_meta.actual_size_recorded
                and self.new_meta.actual_size_recorded
            )
            if requested == "actual" and not both_recorded:
                self._size_source = "logical"
                self._size_fallback = True
            else:
                self._size_source = requested
                self._size_fallback = False

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
            # 「递归范围内含新增/已删除/变大/变小」下钻标记。默认关闭：差集要
            # 全量扫两侧快照，放首屏会让解压到出树的时长上一个量级；前端筛这些
            # 条件时才由接口参数触发（见 :meth:`ensure_marks`），首次一次性计算
            # 后复用。变大/变小也靠祖先标记：目录整体变小不代表下面没有变大的
            # 文件，只看目录自身净变化量会把这类目录整条滤掉。
            self._marks_ready = False
            self._subtree_added_ids: set[int] = set()
            self._subtree_removed_ids: set[int] = set()
            self._subtree_grew_ids: set[int] = set()
            self._subtree_shrank_ids: set[int] = set()
            # 「递归范围内含链接」下钻标记。v4+ 快照才可能有；v3 恒空。
            self._link_ids_ready = False
            self._subtree_link_ids: set[int] = set()
            # 搜索结果缓存：(关键词 casefold, 最宽候选, {(选项,排序): 排好的列表})。
            # 区分大小写/严格匹配是候选的子集，只在内存过滤，不另查库。
            # 快照只读，会话内无需失效；翻页/换排序直接复用排好的列表。
            self._search_cache: tuple[str, list[_SearchEntry]] | None = None
            # 最近一次搜索是否撞到单侧候选上限；供界面提示结果可能不完整
            self.search_truncated = False
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
            # 反链查询要用的链接行索引是否已在本次会话里补建过
            # （见 :meth:`_ensure_link_index`）。
            self._link_index_ready = False

        def side_total(self, meta: SnapshotMeta) -> int:
            """按本次对比生效的口径取一侧的总量。

            生效口径在构造时就已统一降级（两侧不齐时全走逻辑占用），
            所以这里不再逐侧回落，保证两侧总量与差值同单位。
            """
            if self._size_source == "actual" and meta.actual_size_recorded:
                return meta.actual_size_total
            return meta.total_size

        @property
        def total_delta(self) -> int:
            """新旧总量之差（新 − 旧），供界面顶部概览显示。"""
            return self.side_total(self.new_meta) - self.side_total(self.old_meta)

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

