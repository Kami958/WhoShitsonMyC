"""解析单条 MFT 文件记录：STANDARD_INFORMATION / FILE_NAME / 属性迭代。

热路径尽量用 ``memoryview`` + ``struct.unpack_from``，避免为每条属性
再切一份 ``bytes``；``AttrView`` 仍保留给 runlist 等慢路径。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# Attribute types
ATTR_STANDARD_INFORMATION = 0x10
ATTR_ATTRIBUTE_LIST = 0x20
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
ATTR_REPARSE_POINT = 0xC0

# FILE_NAME.flags
FN_NAMESPACE_POSIX = 0
FN_NAMESPACE_WIN32 = 1
FN_NAMESPACE_DOS = 2
FN_NAMESPACE_WIN32_DOS = 3

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# MFT record flags
RECORD_IN_USE = 0x0001
RECORD_IS_DIRECTORY = 0x0002

_FILETIME_EPOCH = 116444736000000000  # 1601→1970 in 100ns
_U32 = struct.Struct("<I")
_U16 = struct.Struct("<H")
_U64 = struct.Struct("<Q")
_I64 = struct.Struct("<q")


@dataclass(slots=True)
class AttrView:
    type_code: int
    name: str
    non_resident: bool
    content: bytes | None  # resident value
    runlist: bytes | None  # raw runlist for non-resident
    real_size: int  # data real size if known


@dataclass(slots=True)
class FileNameAttr:
    parent_ref: int  # parent MFT record number (low 48 bits of ref)
    allocated_size: int
    real_size: int
    flags: int
    name: str
    namespace: int


@dataclass(slots=True)
class ParsedRecord:
    number: int
    in_use: bool
    is_directory: bool
    base_record: int  # 0 = base; else extension of that record
    mtime: int  # unix seconds from STANDARD_INFORMATION if any
    file_names: list[FileNameAttr] = field(default_factory=list)
    data_size: int = 0  # unnamed $DATA real size (files)
    has_reparse: bool = False
    # 扩展记录：把 FILE_NAME 挂到 base 时由 tree 合并


def _u64(b: bytes | memoryview, off: int) -> int:
    return _U64.unpack_from(b, off)[0]


def _u32(b: bytes | memoryview, off: int) -> int:
    return _U32.unpack_from(b, off)[0]


def _u16(b: bytes | memoryview, off: int) -> int:
    return _U16.unpack_from(b, off)[0]


def _i64(b: bytes | memoryview, off: int) -> int:
    return _I64.unpack_from(b, off)[0]


def filetime_to_unix(ft: int) -> int:
    """Windows FILETIME (100ns since 1601) → Unix 秒；无效则 0。"""
    if ft <= 0:
        return 0
    try:
        return max(0, (ft - _FILETIME_EPOCH) // 10_000_000)
    except Exception:
        return 0


def record_number(record: bytes | memoryview, index: int) -> int:
    """优先用记录内 MFT 编号字段，否则用数组下标。"""
    if len(record) >= 0x30 + 4:
        # offset 0x2C: MFT record number (low 32 on older; full varies)
        n = _u32(record, 0x2C)
        if n != 0 or index == 0:
            return n if n != 0 else index
    return index


def _attr_name(record: memoryview, off: int, n: int, name_len: int, name_off: int) -> str:
    if not name_len or not name_off:
        return ""
    end = off + name_off + name_len * 2
    if end > n:
        return ""
    try:
        return bytes(record[off + name_off : end]).decode("utf-16-le", errors="replace")
    except Exception:
        return ""


def iter_attributes(record: bytes | memoryview):
    """Yield :class:`AttrView` from a FILE record (USA 已应用）。"""
    mv = record if isinstance(record, memoryview) else memoryview(record)
    n = len(mv)
    if n < 0x18 or mv[0:4] != b"FILE":
        return
    first = _u16(mv, 0x14)
    off = first
    while off + 8 <= n:
        atype = _u32(mv, off)
        if atype == 0xFFFFFFFF:
            break
        alen = _u32(mv, off + 4)
        if alen < 8 or off + alen > n:
            break
        non_res = mv[off + 8]
        name_len = mv[off + 9]
        name_off = _u16(mv, off + 10)
        name = _attr_name(mv, off, n, name_len, name_off)

        content = None
        runlist = None
        real_size = 0
        if non_res == 0:
            vsize = _u32(mv, off + 0x10)
            voff = _u16(mv, off + 0x14)
            if voff and off + voff + vsize <= n:
                # 保留 bytes：兼容 parse_file_name / 旧调用方
                content = bytes(mv[off + voff : off + voff + vsize])
                real_size = vsize
        else:
            if off + 0x40 <= n:
                real_size = _i64(mv, off + 0x30)
                if real_size < 0:
                    real_size = 0
                run_off = _u16(mv, off + 0x20)
                if run_off and off + run_off < off + alen:
                    runlist = bytes(mv[off + run_off : off + alen])

        yield AttrView(
            type_code=atype,
            name=name,
            non_resident=bool(non_res),
            content=content,
            runlist=runlist,
            real_size=int(real_size),
        )
        off += alen


def parse_file_name(content: bytes | memoryview) -> FileNameAttr | None:
    if content is None or len(content) < 0x42:
        return None
    parent = _u64(content, 0x00) & 0x0000FFFFFFFFFFFF
    alloc = _i64(content, 0x28)
    real = _i64(content, 0x30)
    flags = _u32(content, 0x38)
    name_len = content[0x40]
    namespace = content[0x41]
    end = 0x42 + name_len * 2
    if end > len(content):
        return None
    try:
        if isinstance(content, memoryview):
            name = bytes(content[0x42:end]).decode("utf-16-le", errors="replace")
        else:
            name = content[0x42:end].decode("utf-16-le", errors="replace")
    except Exception:
        return None
    if alloc < 0:
        alloc = 0
    if real < 0:
        real = 0
    return FileNameAttr(
        parent_ref=parent,
        allocated_size=alloc,
        real_size=real,
        flags=flags,
        name=name,
        namespace=namespace,
    )


def parse_record(record: bytes | memoryview, index: int) -> ParsedRecord | None:
    """解析一条 MFT FILE 记录。

    热路径：直接走属性表循环，不为每条属性建 ``AttrView``；
    resident 内容用 memoryview 切片喂给 FILE_NAME 解析。
    """
    mv = record if isinstance(record, memoryview) else memoryview(record)
    n = len(mv)
    if n < 0x30 or mv[0:4] != b"FILE":
        return None

    flags = _u16(mv, 0x16)
    in_use = bool(flags & RECORD_IN_USE)
    # free 记录：直接 None，不扫属性、不建骨架（tree / merge 都跳过）
    if not in_use:
        return None
    is_dir = bool(flags & RECORD_IS_DIRECTORY)
    base = _u64(mv, 0x20) & 0x0000FFFFFFFFFFFF
    number = record_number(mv, index)

    mtime = 0
    file_names: list[FileNameAttr] = []
    data_size = 0
    has_reparse = False

    off = _u16(mv, 0x14)
    while off + 8 <= n:
        atype = _u32(mv, off)
        if atype == 0xFFFFFFFF:
            break
        alen = _u32(mv, off + 4)
        if alen < 8 or off + alen > n:
            break
        non_res = mv[off + 8]
        name_len = mv[off + 9]

        if atype == ATTR_STANDARD_INFORMATION and non_res == 0:
            vsize = _u32(mv, off + 0x10)
            voff = _u16(mv, off + 0x14)
            if voff and off + voff + min(vsize, 0x28) <= n:
                base_off = off + voff
                if vsize >= 0x18:
                    mtime = filetime_to_unix(_u64(mv, base_off + 0x10))
                if vsize >= 0x28:
                    fa = _u32(mv, base_off + 0x20)
                    if fa & FILE_ATTRIBUTE_REPARSE_POINT:
                        has_reparse = True
                    if fa & FILE_ATTRIBUTE_DIRECTORY:
                        is_dir = True
        elif atype == ATTR_FILE_NAME and non_res == 0:
            vsize = _u32(mv, off + 0x10)
            voff = _u16(mv, off + 0x14)
            if voff and off + voff + vsize <= n:
                fn = parse_file_name(mv[off + voff : off + voff + vsize])
                if fn is not None:
                    file_names.append(fn)
                    if fn.flags & FILE_ATTRIBUTE_REPARSE_POINT:
                        has_reparse = True
                    if fn.flags & FILE_ATTRIBUTE_DIRECTORY:
                        is_dir = True
        elif atype == ATTR_DATA and name_len == 0:
            # 未命名数据流
            if non_res == 0:
                vsize = _u32(mv, off + 0x10)
                if vsize > data_size:
                    data_size = vsize
            elif off + 0x40 <= n:
                rs = _i64(mv, off + 0x30)
                if rs > data_size:
                    data_size = rs
        elif atype == ATTR_REPARSE_POINT:
            has_reparse = True

        off += alen

    return ParsedRecord(
        number=number,
        in_use=in_use,
        is_directory=is_dir,
        base_record=base,
        mtime=mtime,
        file_names=file_names,
        data_size=data_size,
        has_reparse=has_reparse,
    )


def pick_best_file_name(names: list[FileNameAttr]) -> FileNameAttr | None:
    """优先 Win32 名，跳过纯 DOS 短名（若有更好的）。"""
    if not names:
        return None
    # 过滤：若有 WIN32 或 WIN32+DOS，不要单独的 DOS
    win = [
        n
        for n in names
        if n.namespace
        in (FN_NAMESPACE_WIN32, FN_NAMESPACE_WIN32_DOS, FN_NAMESPACE_POSIX)
    ]
    pool = win if win else names
    best = pool[0]
    for n in pool[1:]:
        if len(n.name) > len(best.name):
            best = n
    return best
