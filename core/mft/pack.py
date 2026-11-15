"""紧凑 MFT 表的内存布局：定长 meta 结构、字段标志、打包与解包。"""

from __future__ import annotations

import struct
from array import array
from dataclasses import dataclass, field
from .parse import FileNameAttr, ParsedRecord


class CompactNames:
    """文件名行的紧凑存储：定长列加名字文本池。

    一条名字行由各列的一格和文本池里的一段组成，不再为每条造七元组与
    独立整数对象。百万级记录下，每条从约 328 字节降到几十字节；
    分配大小与标志仍保留，回退路径需要它们重建完整记录。
    """

    __slots__ = ("idx", "parent", "alloc", "real", "flags", "ns", "blob", "starts")

    def __init__(self) -> None:
        self.idx = array("i")      # 全局记录号
        # 父记录号与名字起点都是非负且远小于 42.9 亿的数，
        # 用 4 字节无符号槽位，百万级记录下比 8 字节省一半
        self.parent = array("I")   # 父记录号
        self.alloc = array("q")    # 分配大小
        self.real = array("q")     # 真实大小
        self.flags = bytearray()
        self.ns = bytearray()
        self.blob = bytearray()    # UTF-8 名字首尾相接
        self.starts = array("I", [0])

    def __len__(self) -> int:
        return len(self.idx)

    def add(
        self,
        idx: int,
        parent: int,
        alloc: int,
        real: int,
        flags: int,
        namespace: int,
        name: str,
    ) -> None:
        # 父记录号是 48 位字段，损坏的记录会溢出 4 字节无符号槽位；
        # 夹到上限指向一条不存在的记录，建树时被过滤，不会挂错位置
        if parent < 0 or parent > 0xFFFFFFFF:
            parent = 0xFFFFFFFF
        self.idx.append(idx)
        self.parent.append(parent)
        self.alloc.append(alloc)
        self.real.append(real)
        self.flags.append(flags & 0xFF)
        self.ns.append(namespace & 0xFF)
        self.blob.extend(name.encode("utf-8", "replace"))
        self.starts.append(len(self.blob))

    def extend(self, other: "CompactNames") -> None:
        if not other.idx:
            return
        base = len(self.blob)
        self.idx.extend(other.idx)
        self.parent.extend(other.parent)
        self.alloc.extend(other.alloc)
        self.real.extend(other.real)
        self.flags.extend(other.flags)
        self.ns.extend(other.ns)
        self.blob.extend(other.blob)
        self.starts.extend(s + base for s in other.starts[1:])

    def name_at(self, i: int) -> str:
        return self.blob[self.starts[i] : self.starts[i + 1]].decode(
            "utf-8", "replace"
        )


class FoldNames:
    """逐段折叠的文件名行：解析完一段就写进来，段对象随即可以丢掉。

    名字表本身不再整表驻留。每条名字行只存四个数字又一段文本：
    子记录号、父记录号、文本起点与长度、真实大小，共 22 字节；
    建树时按父记录号把这些行串成链表，既不用排序也不用第二遍。
    """

    __slots__ = ("child", "parent", "off", "length", "real", "blob")

    def __init__(self) -> None:
        self.child = array("i")     # 子记录号（扩展记录待建树时换成基记录）
        self.parent = array("i")    # 父记录号
        self.off = array("I")       # 文本在 blob 里的起点
        self.length = array("H")    # 文本字节数
        self.real = array("q")      # 真实大小
        self.blob = bytearray()

    def __len__(self) -> int:
        return len(self.child)

    def add(self, names: CompactNames) -> None:
        """把一段名字行折进来。同一条记录的名字行在段内是相邻的。"""
        nn = len(names)
        i = 0
        while i < nn:
            idx0 = names.idx[i]
            j = i + 1
            while j < nn and names.idx[j] == idx0:
                j += 1
            # 有正式名时丢掉 DOS 短名，与整表路径的取舍保持一致
            has_win = False
            for k in range(i, j):
                if names.ns[k] in (0, 1, 3):
                    has_win = True
                    break
            seen: set[tuple[int, str]] = set()
            for k in range(i, j):
                if has_win and names.ns[k] == 2:
                    continue
                parent = names.parent[k]
                # 父记录号是 48 位字段，损坏的记录会溢出 4 字节槽位；
                # 夹到上限指向一条不存在的记录，建树时被过滤，不会挂错
                if parent < 0 or parent > 0x7FFFFFFF:
                    parent = 0x7FFFFFFF
                name = names.name_at(k)
                key = (parent, name)
                if key in seen:
                    continue
                seen.add(key)
                enc = name.encode("utf-8", "replace")
                self.child.append(idx0)
                self.parent.append(parent)
                self.off.append(len(self.blob))
                self.length.append(len(enc))
                self.real.append(names.real[k])
                self.blob.extend(enc)
            i = j

    def name_at(self, k: int) -> str:
        start = self.off[k]
        return self.blob[start : start + self.length[k]].decode("utf-8", "replace")


@dataclass(slots=True)
class CompactMftTable:
    """worker 紧凑产物：整表 meta + 全局文件名行（跳过 ParsedRecord 再膨胀）。"""

    n_records: int
    meta: bytes  # n_records * META_SIZE
    names: CompactNames = field(default_factory=CompactNames)
    # 分块路径逐段折叠后的文件名行；非空时建树只看它，names 不再整表驻留
    fold: "FoldNames | None" = None
    # 链接目标路径是变长字符串，不塞定长 meta；与 names 同样走旁路列表
    link_targets: list[tuple[int, str]] = field(default_factory=list)
    # 重解析标签只有链接与挂载点有，占比极低，不占主表宽度
    reparse_tags: list[tuple[int, int]] = field(default_factory=list)


def _chunk_ranges(n: int, procs: int) -> list[tuple[int, int]]:
    """把 ``[0, n)`` 切成约 ``procs * 3~4`` 块（每块约 ``_CHUNK_RECORDS``）。"""
    if n <= 0:
        return []
    if procs <= 1:
        return [(0, n)]
    # 目标块数：进程数的 3~4 倍，同时单块不超过 _CHUNK_RECORDS
    target_chunks = max(procs * 3, (n + _CHUNK_RECORDS - 1) // _CHUNK_RECORDS)
    target_chunks = min(n, max(procs, target_chunks))
    chunk = max(1, (n + target_chunks - 1) // target_chunks)
    ranges: list[tuple[int, int]] = []
    s = 0
    while s < n:
        e = min(s + chunk, n)
        ranges.append((s, e))
        s = e
    return ranges


def _pack_chunk(
    records: list[ParsedRecord | None],
) -> tuple[bytes, CompactNames, list[tuple[int, str]], list[tuple[int, int]]]:
    """把一段 ParsedRecord 压成 meta bytes + 文件名表 + 链接目标表 + 重解析标签旁路。

    写进名字表与两张旁路的下标是本段内的相对位置（``enumerate`` 得到），
    不是全局记录号。目前调用方只有单进程回退路径且整表从 0 开始，两者等价；
    要让某段从非零开始，必须同时给 ``_unpack_chunk`` 传同样的 ``start``。
    多进程路径走的是 ``worker._worker_parse_range``，那边写的是全局记录号。
    """
    buf = bytearray(len(records) * _META_SIZE)
    names = CompactNames()
    link_targets: list[tuple[int, str]] = []
    tags: list[tuple[int, int]] = []
    for i, rec in enumerate(records):
        if rec is None:
            _META.pack_into(
                buf, i * _META_SIZE, _F_NONE, 0, 0, 0, 0, _ACTUAL_NONE
            )
            continue
        flags = 0
        if rec.in_use:
            flags |= _F_IN_USE
        if rec.is_directory:
            flags |= _F_IS_DIR
        if rec.has_reparse:
            flags |= _F_REPARSE
        fns = rec.file_names
        # 名字行数不再入表，只用来截断过长的名字列表
        if fns and len(fns) > 0xFFFF:
            fns = fns[:0xFFFF]
        base = rec.base_record
        _META.pack_into(
            buf,
            i * _META_SIZE,
            flags,
            rec.actual_src,
            base,
            rec.mtime & 0xFFFFFFFF,
            rec.data_size & 0xFFFFFFFFFFFFFFFF,
            _ACTUAL_NONE if rec.actual_size is None else rec.actual_size,
        )
        if rec.reparse_tag:
            tags.append((i, rec.reparse_tag & 0xFFFFFFFF))
        if rec.link_target:
            link_targets.append((i, rec.link_target))
        for fn in fns or ():
            names.add(
                i,
                fn.parent_ref,
                fn.allocated_size,
                fn.real_size,
                fn.flags,
                fn.namespace,
                fn.name,
            )
    return bytes(buf), names, link_targets, tags


def _unpack_chunk(
    start: int,
    meta: bytes,
    names: CompactNames,
    link_targets: list[tuple[int, str]] | None = None,
    tags: list[tuple[int, int]] | None = None,
) -> list[ParsedRecord | None]:
    """把一段 meta 解回 ParsedRecord 列表。

    ``start`` 是该段首条记录的全局记录号，名字表与两张旁路里的下标统一按
    全局记录号理解（子进程就是这么写的）。本地打包函数产出的是段内相对下标，
    只在段从 0 开始时两者等价，所以别拿非零 ``start`` 搭配本地打包的结果。
    """
    n = len(meta) // _META_SIZE
    out: list[ParsedRecord | None] = [None] * n
    name_buckets: list[list[FileNameAttr]] = [[] for _ in range(n)]
    for k in range(len(names)):
        rel = names.idx[k] - start
        if 0 <= rel < n:
            name_buckets[rel].append(
                FileNameAttr(
                    parent_ref=names.parent[k],
                    allocated_size=names.alloc[k],
                    real_size=names.real[k],
                    flags=names.flags[k],
                    name=names.name_at(k),
                    namespace=names.ns[k],
                )
            )
    target_buckets: list[str] = [""] * n
    for rel, target in link_targets or ():
        rel -= start
        if 0 <= rel < n:
            target_buckets[rel] = target
    tag_buckets: dict[int, int] = {}
    for rel, tag in tags or ():
        rel -= start
        if 0 <= rel < n:
            tag_buckets[rel] = tag
    for i in range(n):
        flags, actual_src, base, mtime, data_size, actual = _META.unpack_from(
            meta, i * _META_SIZE
        )
        if flags & _F_NONE:
            out[i] = None
            continue
        fns = name_buckets[i]
        out[i] = ParsedRecord(
            number=start + i,
            in_use=bool(flags & _F_IN_USE),
            is_directory=bool(flags & _F_IS_DIR),
            base_record=base,
            mtime=mtime,
            file_names=fns,
            data_size=data_size,
            has_reparse=bool(flags & _F_REPARSE),
            reparse_tag=tag_buckets.get(i, 0),
            link_target=target_buckets[i],
            actual_size=None if actual == _ACTUAL_NONE else actual,
            actual_src=actual_src,
        )
    return out


# flags byte
_F_IN_USE = 0x01


_F_IS_DIR = 0x02


_F_REPARSE = 0x04


_F_NONE = 0x80  # parse_record 返回 None


# flags, src, base_record(8), mtime(4), data_size(8), actual_size(8)
# → 1+1+8+4+8+8 = 30
# nnames 只是截断用的临时量，解包侧从来不读，整列去掉；
# 基记录号保留 8 字节：它是 48 位字段，收窄会在伪造或损坏的记录上截断成
# 一个恰好存在的记录号，把扩展记录挂到别的文件上，风险不值得几兆内存。
# 重解析标签只有链接与挂载点有，走与链接目标一样的旁路列表，不占主表宽度。
# actual_size 用有符号 8 字节，故意留一个哨兵值区分「没采到」与「确实占 0 字节」。
_META = struct.Struct("<BBQIQq")

# 未采集的哨兵值。不能用 0：驻留小文件确实占 0 个簇，两者必须分开。
_ACTUAL_NONE = -1


_META_SIZE = _META.size


# 全局导出供 tree 紧凑路径使用（避免魔法数分叉）
META = _META


META_SIZE = _META_SIZE


F_IN_USE = _F_IN_USE


F_IS_DIR = _F_IS_DIR


F_REPARSE = _F_REPARSE


F_NONE = _F_NONE


ACTUAL_NONE = _ACTUAL_NONE


# 文件名行：record_index, parent_ref, alloc, real, flags, name, namespace
# 兼容别名：文件名行统一由 CompactNames 承载
NameRow = CompactNames


# 任务切块：约为每进程活量的 1/3~1/4，imap_unordered 动态领活抹平 P/E 核尾部
_CHUNK_RECORDS = 15_000
