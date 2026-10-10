"""链接侧的处理：链接索引与链接目标的绝对路径还原。"""

from __future__ import annotations

from ..snapshot import LINK_INDEX_SQL
from .errors import SearchCancelled
from .classify import _conn_version
from .links import _links_into_target, _strip_root
from ..link_match import abs_under_root, link_norm
from ..compress import is_session_temp_db
import os
import sqlite3


class DiffLinkMixin:

    def _ensure_link_index(self) -> None:
        """新侧快照缺链接行索引时补建一次，只对本进程解压出的副本动手。

        索引由扫描端在 :meth:`core.snapshot.SnapshotWriter.finalize` 里建；
        这次改动之前扫出来的快照没有它，反链查询要整表扫描。本进程从
        ``.dbz`` 解压出来的临时副本是程序自己生成、会话结束即删的文件，
        可以往上写；用户自己的 ``.db`` 快照不在此列。
        """
        if self._link_index_ready:
            return
        path = self._db_paths[1]
        if not is_session_temp_db(path):
            return
        conn = sqlite3.connect(path)
        try:
            conn.execute(LINK_INDEX_SQL)
            conn.commit()
        finally:
            conn.close()
        self._link_index_ready = True

    def links_to_abs(self, abs_path: str) -> dict:
        """查当前（新）侧快照中目标正好是 abs_path 的目录联接与符号链接。

        目标按绝对路径给，不要求它落在扫描根下：拿别的盘的快照查本盘的
        目录时，目标写不成「相对根」的形式。

        Args:
            abs_path: 被查目录的绝对路径。

        Returns:
            结果字典；快照格式低于 v5（无 ``link_target`` 列）时
            ``unsupported`` 为 True，``links`` 为空。``links`` 每项的
            ``path`` 为相对扫描根的路径（用于在树上定位），``abs`` 为
            该链接的绝对路径（用于在资源管理器里打开）。

        Raises:
            SearchCancelled: 查询期间用户调用了 :meth:`cancel_search`。
        """
        conn = self._new
        ver = _conn_version(conn)
        root = self.new_meta.root or ""
        base = {
            "ok": True,
            "path": abs_path or "",
            "root": self.new_meta.root,
            "format_version": ver,
            "unsupported": False,
            "min_version": 5,
            "links": [],
            "total": 0,
            "unresolved": [],
            "unresolved_total": 0,
        }
        if ver < 5:
            return {**base, "unsupported": True}

        self._ensure_link_index()
        # 上一轮搜索被取消时标记还立着，不清掉本次查询一开始就会退出
        self._cancel_evt.clear()
        target = link_norm(abs_path or "")
        try:
            hits, unresolved_abs = _links_into_target(
                conn, root, target, cancel=self._cancel_evt
            )
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc).lower():
                raise SearchCancelled() from None
            raise

        links: list[dict] = []
        for hit in hits:
            rel = _strip_root(hit["path"], root)
            if rel is None:
                continue
            links.append({
                "path": rel,
                "name": rel.rsplit(os.sep, 1)[-1],
                "kind": hit["kind"],
                "target": hit["target"],
                "abs": abs_under_root(root, rel),
            })

        unresolved_out: list[dict] = []
        for item in unresolved_abs:
            rel = _strip_root(item["path"], root) if item["path"] else ""
            unresolved_out.append({
                "path": "" if rel is None else rel,
                "kind": item["kind"],
                "target": item["target"],
                "reason": item["reason"],
                "abs": item["path"],
            })

        return {
            **base,
            "links": links,
            "total": len(links),
            "unresolved": unresolved_out,
            "unresolved_total": len(unresolved_out),
        }
