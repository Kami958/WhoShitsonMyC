from __future__ import annotations

from __future__ import annotations

import os
import sqlite3
import sys
import threading
from array import array
from bisect import bisect_left, bisect_right


from .classify import _SideRow, _row_cols

class _SideIndex:
    """一侧快照的常驻内存搜索索引（Everything 式）。

    全部名字 casefold 后用 ``\\0`` 拼成一整块字符串，子串搜索走 C 速度的
    ``str.find``；命中位置经 ``starts`` 二分映射回条目下标。原始名字同样
    拼成一块按需切片，避免几百万个小字符串对象的开销。父子关系存条目 id，
    查父行时在有序 ``ids`` 上二分，不建 id→下标的大字典。
    """

    __slots__ = (
        "ids", "parents", "sizes", "mtimes", "dirs", "rtags", "link_targets",
        "blob", "starts", "orig_blob", "orig_starts", "_path_cache",
    )

    def __init__(self) -> None:
        self.ids = array("q")
        self.parents = array("q")   # 父行 id；根行记 -1
        self.sizes = array("q")
        self.mtimes = array("q")
        self.dirs = bytearray()
        self.rtags = array("I")     # reparse_tag（32 位无符号）；v3 快照全 0
        self.link_targets: list[str] = []  # 链接目标（v4 及更早为空串）
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
            f"SELECT {_row_cols(conn)} FROM entries ORDER BY id"
        )
        while True:
            if stop.is_set():
                return None
            rows = cur.fetchmany(50000)
            if not rows:
                break
            # 按列批量 extend（C 级），比逐行 append 快数倍。
            # v5 多 reparse_tag、link_target 两列；v4 只有 reparse_tag；
            # v3 都没有，按默认值补。
            ids, pids, nms, sizes, dirs, mtimes, *rest = zip(*rows)
            idx.ids.extend(ids)
            idx.parents.extend(
                -1 if p is None else p for p in pids
            )
            idx.sizes.extend(sizes)
            idx.mtimes.extend(mtimes)
            idx.dirs.extend(1 if d else 0 for d in dirs)
            idx.rtags.extend(rest[0] if rest else (0 for _ in nms))
            idx.link_targets.extend(
                rest[1] if len(rest) > 1 else ("" for _ in nms)
            )
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
            int(self.rtags[i]), self.link_targets[i],
        )

    def approx_bytes(self) -> int:
        """索引常驻内存的粗略字节数（供日志排查用）。"""
        total = sys.getsizeof(self.blob) + sys.getsizeof(self.orig_blob)
        for arr in (self.ids, self.parents, self.sizes, self.mtimes,
                    self.starts, self.orig_starts, self.rtags):
            total += len(arr) * arr.itemsize
        total += len(self.dirs)
        total += sum(len(s) for s in self.link_targets)
        return total


