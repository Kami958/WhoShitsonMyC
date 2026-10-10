"""目录层级对比：子节点合并、标记、大文件夹标记继承。"""

from __future__ import annotations

from .errors import DiffError
from ..models import DiffNode
from .classify import (
    _SideRow,
    _child_path,
    _classify,
    _propagate_ancestors,
    _read_side_link_ids,
    _subtree_change_id_sets,
)
from .children import _find_dir_id, _has_children, _merge_child_nodes
import os
from ..i18n import t


class DiffNodeMixin:

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
        nodes = _merge_child_nodes(
            self._old,
            old_pid,
            self._new,
            new_pid,
            lambda name, o, n: self._node_from_sides(parent, name, o, n),
        )

        # 命中删除白名单的节点打标（UI 显示「白名单」徽标）。
        # 白名单读取或匹配失败一律兜底，不影响主流程。
        try:
            from core.fs_delete import path_matches_blacklist
            from core.store import get_delete_blacklist

            bl = get_delete_blacklist()
            if bl:
                root_abs = (self.new_meta.root or self.old_meta.root or "").rstrip("\\/")
                sep = "\\" if os.sep == "\\" else "/"
                for nd in nodes:
                    rel = (nd.path or "").replace("/", sep)
                    abs_path = root_abs + sep + rel if root_abs and rel else (rel or root_abs)
                    nd.whitelisted = path_matches_blacklist(abs_path, bl)
        except Exception:
            pass

        if sort:
            nodes.sort(key=lambda d: abs(d.delta), reverse=True)
        return nodes

    def ensure_marks(self) -> None:
        """启用「递归范围内含新增/已删除/链接」下钻标记，首次一次性计算后复用。

        ``self._subtree_added_ids`` / ``self._subtree_removed_ids``：条目 id
        集合，包含变更条目自身及其祖先。目录的大小是递归汇总的，深层文件的
        新增不会让目录 kind 变成 added；前端筛「新增/已删除」时据此保留目录
        作下钻入口，否则树会整层被滤空。

        ``self._subtree_link_ids``：含链接的子树 id 集合（同样含祖先），
        供前端筛「链接」时保留下钻入口。v4+ 快照才可能有；v3 恒空。

        线性算法，不做递归 CTE：递归 CTE 在 SQLite 里每层迭代都重扫全表，
        大快照下慢一个数量级；这里两侧各一次全表扫描，栈式拼出条目路径，
        按路径差集（两侧条目 id 各自分配，跨快照可比的是路径），再从差异
        条目沿 parent 链向上传播。
        """
        if self._marks_ready:
            return
        old_db, new_db = self._db_paths
        self._subtree_added_ids, self._subtree_removed_ids = _subtree_change_id_sets(
            old_db, new_db
        )
        self._marks_ready = True
        # 链接标记一并就绪（v3 侧无 reparse_tag 列，跳过）
        if self._link_ids_ready:
            return
        link_ids: set[int] = set()
        for conn, meta in ((self._old, self.old_meta), (self._new, self.new_meta)):
            if meta.format_version < 4:
                continue
            parents, ids = _read_side_link_ids(conn)
            link_ids |= _propagate_ancestors(ids, parents)
        self._subtree_link_ids = link_ids
        self._link_ids_ready = True

    def _node_from_sides(
        self,
        parent: str,
        name: str,
        o: _SideRow | None,
        n: _SideRow | None,
    ) -> DiffNode | None:
        """由两侧子行 ``(id, size, is_dir, mtime, reparse_tag, link_target)`` 合成节点。"""
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
        reparse_tag = int(n[4] if n is not None else (o[4] if o is not None else 0))
        link_target = n[5] if n is not None else (o[5] if o is not None else "")

        if is_dir:
            ids = (o[0] if o else None, n[0] if n else None)
            self._dir_ids[path] = ids
            has_children = _has_children(self._old, ids[0]) or _has_children(
                self._new, ids[1]
            )
            has_added = (
                self._marks_ready
                and ids[1] is not None
                and ids[1] in self._subtree_added_ids
            )
            has_removed = (
                self._marks_ready
                and ids[0] is not None
                and ids[0] in self._subtree_removed_ids
            )
            has_link = self._link_ids_ready and (
                (ids[0] is not None and ids[0] in self._subtree_link_ids)
                or (ids[1] is not None and ids[1] in self._subtree_link_ids)
            )
        else:
            has_children = False
            has_added = False
            has_removed = False
            has_link = False

        return DiffNode(
            path=path,
            name=name,
            is_dir=is_dir,
            old_size=old_size,
            new_size=new_size,
            delta=delta,
            kind=kind,
            has_children=has_children,
            has_added=has_added,
            has_removed=has_removed,
            mtime=mtime,
            reparse_tag=reparse_tag,
            has_link=has_link,
            link_target=link_target,
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
