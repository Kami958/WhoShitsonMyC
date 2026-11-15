"""解析单条 MFT 文件记录：STANDARD_INFORMATION / FILE_NAME / 属性迭代。

热路径尽量用 ``memoryview`` + ``struct.unpack_from``，避免为每条属性
再切一份 ``bytes``；``AttrView`` 仍保留给 runlist 等慢路径。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from ..models import clean_link_target

# Attribute types
ATTR_STANDARD_INFORMATION = 0x10
ATTR_ATTRIBUTE_LIST = 0x20
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
ATTR_REPARSE_POINT = 0xC0

# 重解析点标记：透明压缩（CompactGUI 一类工具用的是这个）
IO_REPARSE_TAG_WOF = 0x80000017

# FILE_NAME.flags
FN_NAMESPACE_POSIX = 0
FN_NAMESPACE_WIN32 = 1
FN_NAMESPACE_DOS = 2
FN_NAMESPACE_WIN32_DOS = 3

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# 实际占用采集用的常量。
# WOF（Windows 叠层过滤，CompactGUI 等工具用的就是它）把真实数据搬到名为
# WofCompressedData 的附属流里，主数据流被掏成稀疏区，那格分配大小是虚的。
_NONRES_FLAG_COMPRESSED = 0x0001  # 非驻留属性头偏移 0x0C 的压缩标志
_SHAPE_SPARSE = 0x8000  # 同一位置的稀疏标志
_WOF_STREAM_BYTES = "WofCompressedData".encode("utf-16-le")  # 17 字符 = 34 字节


def _run_bytes(mv, start: int, end: int, cluster_size: int) -> int:
    """从分配清单算出真正落盘的字节数，标为空洞的段不计入。

    稀疏文件在属性头里写的分配值是名义范围（文件看起来占了一大片），
    真实占用只能从分配清单本身数：每条记录写明这一段占几个簇，
    偏移字段为空的那条是空洞，它不占任何簇。
    """
    p = start
    total = 0
    limit = end if end < len(mv) else len(mv)
    while p < limit:
        head = mv[p]
        if head == 0:
            break
        len_size = head & 0x0F
        off_size = head >> 4
        p += 1
        if len_size == 0 or p + len_size + off_size > limit:
            break
        run_len = int.from_bytes(mv[p : p + len_size], "little")
        p += len_size + off_size
        if off_size:
            total += run_len
    return total * cluster_size

# 一笔占用数字的来源。两条记录合并时要靠它决定听谁的：
# 压缩文件的空壳主数据流比真实数据大，按大小猜会选中空壳。
ACTUAL_SRC_NONE = 0  # 没采到
ACTUAL_SRC_ALLOC = 1  # 主数据流分配（普通文件，准）
ACTUAL_SRC_COMPRESSED = 2  # NTFS 压缩后大小（准）
ACTUAL_SRC_WOF = 3  # 旁路数据流的分配（准，压缩文件就该听这个）
ACTUAL_SRC_RESIDENT = 4  # 驻留，占 0 簇
ACTUAL_SRC_SPARSE = 5  # 稀疏主数据流的分配（真稀疏文件就这个数，但空壳也长这样）

# 可信度排序，数值大的更可信。WOF 最可信：一个文件同时有旁路流和空壳时，
# 旁路流才是真占用。主数据流与稀疏主数据流同分，它们不会同时出现在一处。
ACTUAL_SRC_RANK = {
    ACTUAL_SRC_NONE: 0,
    ACTUAL_SRC_RESIDENT: 1,
    ACTUAL_SRC_ALLOC: 2,
    ACTUAL_SRC_SPARSE: 2,
    ACTUAL_SRC_COMPRESSED: 3,
    ACTUAL_SRC_WOF: 4,
}

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
    reparse_tag: int = 0  # 驻留 $REPARSE_POINT 的 tag；非驻留（长目标）为 0
    link_target: str = ""  # 驻留 reparse buffer 里的目标路径；取不到为空
    # 实际占用字节数。None = 没采到（旧快照、或数据流在扩展记录里），
    # 0 = 确实不占簇（驻留小文件），两者不可混为一谈。
    actual_size: int | None = None
    # 这个数字是从哪里读来的，见 ACTUAL_SRC_*。建树合并时按它决定
    # 该听谁的，而不是按数据流大小猜：压缩文件的空壳主流比真实数据大，
    # 按大小猜会选中空壳，拿到的就是原体积而不是占用。
    actual_src: int = 0
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


# REPARSE_DATA_BUFFER 内的名字区起点：符号链接前面多 4 字节 Flags。
_REPARSE_PATH_OFF_JUNCTION = 16
_REPARSE_PATH_OFF_SYMLINK = 20
_TAG_SYMLINK = 0xA000000C


def parse_reparse_target(buf: bytes | memoryview, tag: int) -> str:
    """从驻留 REPARSE_DATA_BUFFER 里取链接目标路径。

    结构（``buf`` 为属性内容起点）：0 为 tag，8/10 是 SubstituteName 的
    偏移与长度，12/14 是 PrintName 的。名字区起点 junction 为 16、
    符号链接为 20（前面多 4 字节 Flags），偏移都相对名字区起点。

    优先取 PrintName（干净的显示路径）；为空时回退 SubstituteName，
    它带 ``\\??\\`` 前缀，用 :func:`clean_link_target` 剥掉。解不出返回空串。
    """
    try:
        n = len(buf)
        if n < 16:
            return ""
        path_off = (
            _REPARSE_PATH_OFF_SYMLINK if tag == _TAG_SYMLINK
            else _REPARSE_PATH_OFF_JUNCTION
        )
        sub_off = _u16(buf, 8)
        sub_len = _u16(buf, 10)
        print_off = _u16(buf, 12)
        print_len = _u16(buf, 14)
        for off, length in ((print_off, print_len), (sub_off, sub_len)):
            if length <= 0:
                continue
            s = path_off + off
            e = s + length
            if s < path_off or e > n:
                continue
            text = bytes(buf[s:e]).decode("utf-16-le", errors="replace").strip("\x00")
            if text:
                return clean_link_target(text)
        return ""
    except (ValueError, struct.error, UnicodeDecodeError):
        return ""


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


def parse_record(
    record: bytes | memoryview, index: int, cluster_size: int = 4096
) -> ParsedRecord | None:
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
    reparse_tag = 0
    link_target = ""
    # 实际占用的三个候选值，循环结束后按优先级定夺
    main_alloc: int | None = None
    main_compressed: int | None = None
    wof_alloc: int | None = None
    # 主数据流是否为稀疏：稀疏流的分配值是名义值，不是真实占用
    main_sparse = False
    # 稀疏主数据流各段的分配清单位置，稍后用它算真实占用
    sparse_segs: list[tuple[int, int]] = []
    # 附属流是否出现：出现了但读不到占用时要记未采集，不能回落到
    # 那个被掏空的主流（它是虚高的名义值，比真实占用大得多）。
    wof_seen = False
    has_resident_data = False

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
        elif atype == ATTR_DATA:
            if name_len == 0:
                # 未命名数据流
                if non_res == 0:
                    vsize = _u32(mv, off + 0x10)
                    if vsize > data_size:
                        data_size = vsize
                    has_resident_data = True
                elif alen >= 0x40 and off + 0x40 <= n:
                    rs = _i64(mv, off + 0x30)
                    if rs > data_size:
                        data_size = rs
                    # 分配大小只在首段（lowest_vcn=0）有意义。非首段这个字段
                    # 不是真实值，拿它会把续段的垃圾字节当成占用记下来。
                    vcn = _u64(mv, off + 0x10)
                    if main_alloc is None and vcn == 0:
                        a = _i64(mv, off + 0x28)
                        # 负数是解析出了错，宁可记未采集也不记成「占 0 簇」
                        if a >= 0:
                            main_alloc = a
                            # 稀疏主数据流的分配值是名义值：文件看起来占了一大片，
                            # 实际并不占。透明压缩的文件就是这样，真实数据在附属流，
                            # 附属流也可能落在另一条记录里，这里必须把名义值拒掉，
                            # 交给合并步骤去补，否则会把压缩节省当作不存在。
                            if _u32(mv, off + 0x0C) & _SHAPE_SPARSE:
                                main_sparse = True
                    # 稀疏段不论第几段都要记下分配清单的位置：真实占用是
                    # 各段里非空洞的簇加起来。
                    if _u32(mv, off + 0x0C) & _SHAPE_SPARSE:
                        main_sparse = True
                        ro = _u16(mv, off + 0x20)
                        if ro and off + ro < n:
                            sparse_segs.append((off + ro, off + alen))
                    # 压缩流：标志位 0x0001 且首段，扩展头才有压缩后大小。
                    # 仅凭压缩单元非零不够：稀疏文件也带压缩单元。
                    if _u32(mv, off + 0x0C) & _NONRES_FLAG_COMPRESSED and vcn == 0:
                        # 标准非驻留头 0x40 字节，压缩时延长到 0x48                        if alen >= 0x48 and off + 0x48 <= n:
                            c = _i64(mv, off + 0x40)
                            if c >= 0:
                                main_compressed = c
            elif name_len == 17:
                # 属性名长度按 UTF-16 字符计（17），落到字节是 34。
                # 只有 WofCompressedData 是这个长度且重要，直接比字节串，不解码。
                name_off = _u16(mv, off + 0x0A)
                s = off + name_off
                if name_off and s + 34 <= n and mv[s : s + 34] == _WOF_STREAM_BYTES:
                    wof_seen = True
                    if non_res == 0:
                        wof_alloc = 0
                    elif alen >= 0x30 and off + 0x30 <= n and _u64(mv, off + 0x10) == 0:
                        a = _i64(mv, off + 0x28)
                        if a >= 0:
                            wof_alloc = a
        elif atype == ATTR_REPARSE_POINT:
            has_reparse = True
            # 驻留：tag 在内容前 4 字节（IO_REPARSE_TAG_*），目标路径在
            # REPARSE_DATA_BUFFER 里。非驻留（长目标路径）需要 runlist 读盘，
            # 这里不读，tag 留 0、目标留空（该条不标记为链接）。
            if non_res == 0:
                vsize = _u32(mv, off + 0x10)
                voff = _u16(mv, off + 0x14)
                if voff and vsize >= 4 and off + voff + 4 <= n:
                    reparse_tag = _u32(mv, off + voff)
                if voff and vsize >= 16 and off + voff + vsize <= n:
                    link_target = parse_reparse_target(
                        mv[off + voff : off + voff + vsize], reparse_tag
                    )

        off += alen

    # 实际占用按这几种情况定：WOF 附属流最优先，因为主数据流那格是
    # 被掳空的稀疏区，比真实占用大得多；其次是 NTFS 压缩流的压缩后大小，
    # 因为主数据流那格记的是压缩前大小；然后是普通文件的分配大小；
    # 数据直接写在记录里的驻留文件确实占 0 个簇。
    # 没有 $DATA 属性时留 None：这种记录的数据流在扩展记录里，由建树阶段
    # 合并扩展记录时补齐（那里按「大小取自哪条，占用就取哪条」处理）。
    if is_dir:
        actual_size = None
        actual_src = ACTUAL_SRC_NONE
    elif wof_seen or reparse_tag == IO_REPARSE_TAG_WOF:
        # 透明压缩文件的真实数据在附属流里，主数据流那格是空洞的名义值。
        # 附属流可能落在同一条记录的后面，也可能整条落在扩展记录里：
        # 后者这里读到就是空，必须如实留空交给合并步骤，绝不能退回主流。
        actual_size = wof_alloc
        actual_src = ACTUAL_SRC_WOF if wof_alloc is not None else ACTUAL_SRC_NONE
    elif main_compressed is not None:
        actual_size = main_compressed
        actual_src = ACTUAL_SRC_COMPRESSED
    elif main_sparse:
        # 稀疏主数据流属性头里记的分配值是名义范围，不是真实占用，
        # 只能从分配清单里数非空洞的簇。各段可能分散在好几条记录里，
        # 每条记录算出自己那段，建树合并时相加。
        if sparse_segs:
            total = 0
            for seg_start, seg_end in sparse_segs:
                total += _run_bytes(mv, seg_start, seg_end, cluster_size)
            actual_size = total
            actual_src = ACTUAL_SRC_SPARSE
        else:
            actual_size = None
            actual_src = ACTUAL_SRC_NONE
    elif main_alloc is not None:
        actual_size = main_alloc
        actual_src = ACTUAL_SRC_ALLOC
    elif has_resident_data:
        actual_size = 0
        actual_src = ACTUAL_SRC_RESIDENT
    else:
        actual_size = None
        actual_src = ACTUAL_SRC_NONE

    return ParsedRecord(
        number=number,
        in_use=in_use,
        is_directory=is_dir,
        base_record=base,
        mtime=mtime,
        file_names=file_names,
        data_size=data_size,
        has_reparse=has_reparse,
        reparse_tag=reparse_tag,
        link_target=link_target,
        actual_size=actual_size,
        actual_src=actual_src,
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
