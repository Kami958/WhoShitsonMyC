"""紧凑 MFT 表的内存布局：定长 meta 结构、字段标志、打包与解包。"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from .parse import FileNameAttr, ParsedRecord


@dataclass(slots=True)
class CompactMftTable:
    """worker 紧凑产物：整表 meta + 全局文件名行（跳过 ParsedRecord 再膨胀）。"""

    n_records: int
    meta: bytes  # n_records * META_SIZE
    names: list[NameRow] = field(default_factory=list)
    # 链接目标路径是变长字符串，不塞定长 meta；与 names 同样走旁路列表
    link_targets: list[tuple[int, str]] = field(default_factory=list)


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
) -> tuple[bytes, list[NameRow], list[tuple[int, str]]]:
    """把一段 ParsedRecord 压成 meta bytes + 文件名表 + 链接目标表。"""
    buf = bytearray(len(records) * _META_SIZE)
    names: list[NameRow] = []
    link_targets: list[tuple[int, str]] = []
    for i, rec in enumerate(records):
        if rec is None:
            _META.pack_into(buf, i * _META_SIZE, _F_NONE, 0, 0, 0, 0, 0, 0)
            continue
        flags = 0
        if rec.in_use:
            flags |= _F_IN_USE
        if rec.is_directory:
            flags |= _F_IS_DIR
        if rec.has_reparse:
            flags |= _F_REPARSE
        fns = rec.file_names
        nnames = len(fns) if fns else 0
        if nnames > 0xFFFF:
            nnames = 0xFFFF
            fns = fns[:nnames]
        _META.pack_into(
            buf,
            i * _META_SIZE,
            flags,
            0,
            nnames,
            rec.base_record & 0xFFFFFFFFFFFFFFFF,
            rec.mtime & 0xFFFFFFFF,
            rec.data_size & 0xFFFFFFFFFFFFFFFF,
            rec.reparse_tag & 0xFFFFFFFF,
        )
        if rec.link_target:
            link_targets.append((i, rec.link_target))
        for fn in fns or ():
            names.append(
                (
                    i,
                    fn.parent_ref,
                    fn.allocated_size,
                    fn.real_size,
                    fn.flags,
                    fn.name,
                    fn.namespace,
                )
            )
    return bytes(buf), names, link_targets


def _unpack_chunk(
    start: int,
    meta: bytes,
    names: list[tuple[int, int, int, int, int, str, int]],
    link_targets: list[tuple[int, str]] | None = None,
) -> list[ParsedRecord | None]:
    n = len(meta) // _META_SIZE
    out: list[ParsedRecord | None] = [None] * n
    name_buckets: list[list[FileNameAttr]] = [[] for _ in range(n)]
    for rel, parent, alloc, real, flags, name, ns in names:
        if 0 <= rel < n:
            name_buckets[rel].append(
                FileNameAttr(
                    parent_ref=parent,
                    allocated_size=alloc,
                    real_size=real,
                    flags=flags,
                    name=name,
                    namespace=ns,
                )
            )
    target_buckets: list[str] = [""] * n
    for rel, target in link_targets or ():
        if 0 <= rel < n:
            target_buckets[rel] = target
    for i in range(n):
        flags, _pad, nnames, base, mtime, data_size, rtag = _META.unpack_from(
            meta, i * _META_SIZE
        )
        if flags & _F_NONE:
            out[i] = None
            continue
        fns = name_buckets[i]
        _ = nnames
        out[i] = ParsedRecord(
            number=start + i,
            in_use=bool(flags & _F_IN_USE),
            is_directory=bool(flags & _F_IS_DIR),
            base_record=base,
            mtime=mtime,
            file_names=fns,
            data_size=data_size,
            has_reparse=bool(flags & _F_REPARSE),
            reparse_tag=rtag,
            link_target=target_buckets[i],
        )
    return out


# flags byte
_F_IN_USE = 0x01


_F_IS_DIR = 0x02


_F_REPARSE = 0x04


_F_NONE = 0x80  # parse_record 返回 None


# flags, pad, nnames, base_record, mtime, data_size, reparse_tag → 1+1+2+8+4+8+4 = 28
_META = struct.Struct("<BBHQIQI")


_META_SIZE = _META.size


# 全局导出供 tree 紧凑路径使用（避免魔法数分叉）
META = _META


META_SIZE = _META_SIZE


F_IN_USE = _F_IN_USE


F_IS_DIR = _F_IS_DIR


F_REPARSE = _F_REPARSE


F_NONE = _F_NONE


# 文件名行：record_index, parent_ref, alloc, real, flags, name, namespace
NameRow = tuple[int, int, int, int, int, str, int]


# 任务切块：约为每进程活量的 1/3~1/4，imap_unordered 动态领活抹平 P/E 核尾部
_CHUNK_RECORDS = 35_000
