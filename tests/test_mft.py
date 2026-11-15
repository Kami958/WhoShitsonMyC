"""MFT 模块：资格判断、解析小工具；全盘集成仅在管理员+显式环境变量下跑。"""

from __future__ import annotations

import os
import struct

import pytest

from core.mft import is_mft_eligible
from core.mft.parse import (
    filetime_to_unix,
    parse_file_name,
    pick_best_file_name,
)
from core.mft.parse import FileNameAttr
from core.mft.scan import _drive_letter_and_root
from core.mft.volume import decode_runlist, parse_boot


def test_drive_letter_only_roots():
    assert _drive_letter_and_root("C:\\") is not None
    assert _drive_letter_and_root("C:/") is not None
    letter, root = _drive_letter_and_root("c:\\")
    assert letter == "C"
    assert root == "C:\\"
    assert _drive_letter_and_root("C:\\Users") is None
    assert _drive_letter_and_root("\\\\server\\share") is None


def test_is_mft_eligible_subdir_false(tmp_path):
    assert is_mft_eligible(str(tmp_path)) is False


def test_mft_on_by_default_flag(monkeypatch):
    """默认 use_mft 开：关开关后盘符根也不走 MFT。"""
    monkeypatch.delenv("WSMC_USE_MFT", raising=False)
    monkeypatch.delenv("WSMC_DISABLE_MFT", raising=False)
    monkeypatch.setattr("core.mft.scan._is_admin", lambda: True)
    from core import store

    store._use_mft = False  # 不经 set_use_mft，避免写真实 settings.yaml
    assert is_mft_eligible("C:\\") is False
    store._use_mft = True


def test_mft_requires_admin(monkeypatch):
    """非管理员即使 use_mft 开也不走 MFT（设置勾选可保留，扫描侧回退）。"""
    monkeypatch.delenv("WSMC_USE_MFT", raising=False)
    monkeypatch.delenv("WSMC_DISABLE_MFT", raising=False)
    monkeypatch.setattr("core.mft.scan._is_admin", lambda: False)
    from core import store

    store._use_mft = True
    assert is_mft_eligible("C:\\") is False


def test_mft_use_flag_required(monkeypatch):
    monkeypatch.setenv("WSMC_USE_MFT", "1")
    monkeypatch.delenv("WSMC_DISABLE_MFT", raising=False)
    monkeypatch.setattr("core.mft.scan._is_admin", lambda: True)
    # 子目录仍 false；盘符根在非 Windows 或非 NTFS 时也可能 false
    assert is_mft_eligible("C:\\Users") is False


def test_mft_store_toggle(monkeypatch):
    """设置页 use_mft 与环境变量等价开启。"""
    monkeypatch.delenv("WSMC_USE_MFT", raising=False)
    monkeypatch.delenv("WSMC_DISABLE_MFT", raising=False)
    monkeypatch.setattr("core.mft.scan._is_admin", lambda: True)
    from core import store

    store._use_mft = False
    assert is_mft_eligible("C:\\Users") is False
    store._use_mft = True
    # 子目录仍不合格
    assert is_mft_eligible("C:\\Users") is False
    store._use_mft = False


def test_mft_disable_overrides_store(monkeypatch):
    monkeypatch.delenv("WSMC_USE_MFT", raising=False)
    monkeypatch.setenv("WSMC_DISABLE_MFT", "1")
    monkeypatch.setattr("core.mft.scan._is_admin", lambda: True)
    from core import store

    store._use_mft = True
    assert is_mft_eligible("C:\\") is False
    store._use_mft = False


def test_filetime_to_unix_epochish():
    # 2020-01-01 approx
    # known: 132223104000000000 is around 2019-2020
    ft = 132223104000000000
    ts = filetime_to_unix(ft)
    assert 1_500_000_000 < ts < 2_000_000_000


def test_decode_runlist_simple():
    # length=1 cluster, offset=+4 → one run at LCN 4, length 1 cluster
    # header: len_size=1, off_size=1 → 0x11, len=1, off=4
    data = bytes([0x11, 0x01, 0x04, 0x00])
    runs = decode_runlist(data, cluster_size=4096)
    assert runs == [(4, 4096)]


def test_parse_boot_rejects_non_ntfs():
    boot = bytearray(512)
    boot[3:11] = b"FAT32   "
    with pytest.raises(Exception):
        parse_boot(bytes(boot))


def test_parse_boot_minimal_ntfs():
    boot = bytearray(512)
    boot[3:11] = b"NTFS    "
    struct.pack_into("<H", boot, 0x0B, 512)  # bps
    boot[0x0D] = 8  # spc → 4096 cluster
    struct.pack_into("<q", boot, 0x30, 1000)  # mft lcn
    boot[0x40] = 0xF6  # -10 → 2^10 = 1024 record size
    info = parse_boot(bytes(boot))
    assert info.cluster_size == 4096
    assert info.mft_lcn == 1000
    assert info.record_size == 1024


def test_parse_file_name_and_pick():
    # minimal FILE_NAME content
    name = "Hello.txt"
    content = bytearray(0x42 + len(name) * 2)
    struct.pack_into("<Q", content, 0, 5)  # parent = root
    struct.pack_into("<q", content, 0x28, 100)
    struct.pack_into("<q", content, 0x30, 80)
    struct.pack_into("<I", content, 0x38, 0)
    content[0x40] = len(name)
    content[0x41] = 1  # WIN32
    content[0x42 : 0x42 + len(name) * 2] = name.encode("utf-16-le")
    fn = parse_file_name(bytes(content))
    assert fn is not None
    assert fn.name == "Hello.txt"
    assert fn.parent_ref == 5
    assert fn.real_size == 80

    dos = FileNameAttr(5, 100, 80, 0, "HELLO~1.TXT", 2)
    best = pick_best_file_name([dos, fn])
    assert best is not None
    assert best.name == "Hello.txt"


def _reparse_buffer(
    tag: int, target: str, *, print_name: str | None = None
) -> bytes:
    """构造驻留 REPARSE_DATA_BUFFER 内容（与 _minimal_file_record 同源逻辑）。"""
    is_symlink = tag == 0xA000000C
    sub = "\\??\\" + target
    sub_b = sub.encode("utf-16-le")
    print_b = (target if print_name is None else print_name).encode("utf-16-le")
    path_off = 20 if is_symlink else 16
    content = bytearray(path_off + len(sub_b) + len(print_b))
    struct.pack_into("<I", content, 0, tag)
    struct.pack_into("<H", content, 4, len(content) - 8)
    struct.pack_into("<H", content, 8, 0)
    struct.pack_into("<H", content, 10, len(sub_b))
    struct.pack_into("<H", content, 12, len(sub_b))
    struct.pack_into("<H", content, 14, len(print_b))
    content[path_off : path_off + len(sub_b)] = sub_b
    content[path_off + len(sub_b) :] = print_b
    return bytes(content)


def test_parse_reparse_target_junction_printname():
    from core.mft.parse import parse_reparse_target

    buf = _reparse_buffer(0xA0000003, r"D:\360download\目标")
    # PrintName 是干净路径，优先取它（不残留 \\??\\ 前缀）
    assert parse_reparse_target(buf, 0xA0000003) == r"D:\360download\目标"


def test_parse_reparse_target_symlink_offset():
    """符号链接 PathBuffer 前多 4 字节 Flags，偏移与 junction 不同。"""
    from core.mft.parse import parse_reparse_target

    buf = _reparse_buffer(0xA000000C, r"D:\tools\link")
    assert parse_reparse_target(buf, 0xA000000C) == r"D:\tools\link"


def test_parse_reparse_target_substitute_fallback():
    """PrintName 为空时回退 SubstituteName 并剥 \\??\\ 前缀。"""
    from core.mft.parse import parse_reparse_target

    sub = "\\??\\D:\\fallback"
    sub_b = sub.encode("utf-16-le")
    path_off = 20
    content = bytearray(path_off + len(sub_b))
    struct.pack_into("<I", content, 0, 0xA000000C)
    struct.pack_into("<H", content, 4, len(content) - 8)
    struct.pack_into("<H", content, 8, 0)
    struct.pack_into("<H", content, 10, len(sub_b))
    struct.pack_into("<H", content, 12, 0)  # PrintNameLength = 0
    struct.pack_into("<H", content, 14, 0)
    content[path_off : path_off + len(sub_b)] = sub_b
    assert parse_reparse_target(bytes(content), 0xA000000C) == r"D:\fallback"


def test_parse_reparse_target_garbage_returns_empty():
    from core.mft.parse import parse_reparse_target

    assert parse_reparse_target(b"\x00" * 4, 0xA0000003) == ""
    assert parse_reparse_target(b"", 0xA0000003) == ""


def test_parse_record_extracts_link_target():
    from core.mft.parse import parse_record

    rec_bytes = _minimal_file_record(
        number=7,
        name="link",
        is_dir=True,
        reparse_tag=0xA0000003,
        link_target=r"D:\360download\目标",
    )
    rec = parse_record(rec_bytes, 7)
    assert rec is not None
    assert rec.has_reparse is True
    assert rec.reparse_tag == 0xA0000003
    assert rec.link_target == r"D:\360download\目标"


def _minimal_file_record(
    *,
    number: int,
    in_use: bool = True,
    is_dir: bool = False,
    name: str = "x.txt",
    parent: int = 5,
    rec_size: int = 1024,
    reparse_tag: int = 0,
    link_target: str = "",
) -> bytes:
    """构造可被 parse_record 识别的最小 FILE 记录。"""
    buf = bytearray(rec_size)
    buf[0:4] = b"FILE"
    # first attr offset
    struct.pack_into("<H", buf, 0x14, 0x38)
    flags = 0
    if in_use:
        flags |= 0x0001
    if is_dir:
        flags |= 0x0002
    struct.pack_into("<H", buf, 0x16, flags)
    struct.pack_into("<I", buf, 0x2C, number)
    # base record ref = 0
    struct.pack_into("<Q", buf, 0x20, 0)

    off = 0x38
    # STANDARD_INFORMATION resident (minimal)
    si_vsize = 0x28
    si_alen = 0x18 + si_vsize  # header-ish + value
    struct.pack_into("<I", buf, off, 0x10)  # type
    struct.pack_into("<I", buf, off + 4, si_alen)
    buf[off + 8] = 0  # resident
    buf[off + 9] = 0  # name_len
    struct.pack_into("<I", buf, off + 0x10, si_vsize)
    struct.pack_into("<H", buf, off + 0x14, 0x18)
    # mtime at value+0x10
    struct.pack_into("<Q", buf, off + 0x18 + 0x10, 132223104000000000)
    off += si_alen

    # FILE_NAME resident
    name_bytes = name.encode("utf-16-le")
    fn_vsize = 0x42 + len(name_bytes)
    fn_alen = 0x18 + fn_vsize
    # pad alen to 8
    if fn_alen % 8:
        fn_alen += 8 - (fn_alen % 8)
    struct.pack_into("<I", buf, off, 0x30)
    struct.pack_into("<I", buf, off + 4, fn_alen)
    buf[off + 8] = 0
    buf[off + 9] = 0
    struct.pack_into("<I", buf, off + 0x10, fn_vsize)
    struct.pack_into("<H", buf, off + 0x14, 0x18)
    v = off + 0x18
    struct.pack_into("<Q", buf, v, parent)
    struct.pack_into("<q", buf, v + 0x28, 100)
    struct.pack_into("<q", buf, v + 0x30, 80)
    struct.pack_into("<I", buf, v + 0x38, 0x10 if is_dir else 0)
    buf[v + 0x40] = len(name)
    buf[v + 0x41] = 1  # WIN32
    buf[v + 0x42 : v + 0x42 + len(name_bytes)] = name_bytes
    off += fn_alen

    if reparse_tag:
        # REPARSE_DATA_BUFFER：tag / datalen / Substitute+Print 名偏移长度 / PathBuffer
        sub = ("\\??\\" + link_target) if link_target else ""
        sub_b = sub.encode("utf-16-le")
        print_b = link_target.encode("utf-16-le")
        is_symlink = reparse_tag == 0xA000000C
        path_off = 20 if is_symlink else 16
        content = bytearray(path_off + len(sub_b) + len(print_b))
        struct.pack_into("<I", content, 0, reparse_tag)
        struct.pack_into("<H", content, 4, len(content) - 8)
        struct.pack_into("<H", content, 8, 0)               # SubstituteNameOffset
        struct.pack_into("<H", content, 10, len(sub_b))     # SubstituteNameLength
        struct.pack_into("<H", content, 12, len(sub_b))     # PrintNameOffset
        struct.pack_into("<H", content, 14, len(print_b))   # PrintNameLength
        content[path_off : path_off + len(sub_b)] = sub_b
        content[path_off + len(sub_b) :] = print_b

        rp_vsize = len(content)
        rp_alen = 0x18 + rp_vsize
        if rp_alen % 8:
            rp_alen += 8 - (rp_alen % 8)
        struct.pack_into("<I", buf, off, 0xC0)  # ATTR_REPARSE_POINT
        struct.pack_into("<I", buf, off + 4, rp_alen)
        buf[off + 8] = 0  # resident
        buf[off + 9] = 0
        struct.pack_into("<I", buf, off + 0x10, rp_vsize)
        struct.pack_into("<H", buf, off + 0x14, 0x18)
        buf[off + 0x18 : off + 0x18 + rp_vsize] = content
        off += rp_alen

    # end marker
    struct.pack_into("<I", buf, off, 0xFFFFFFFF)
    return bytes(buf)


def test_parse_records_serial_and_pack_roundtrip():
    from core.mft import parallel
    from core.mft.parse import parse_record

    rec_size = 1024
    r0 = _minimal_file_record(number=0, name="$MFT", is_dir=False)
    r5 = _minimal_file_record(number=5, name=".", is_dir=True, parent=5)
    r6 = _minimal_file_record(number=6, name="hello.txt", parent=5)
    blob = r0 + bytes(rec_size) * 4 + r5 + r6  # indices 0,5,6 used; 1-4 garbage
    # rebuild contiguous 0..6
    parts = [bytes(rec_size)] * 7
    parts[0] = r0
    parts[5] = r5
    parts[6] = r6
    blob = b"".join(parts)

    parsed = parallel.parse_records_serial(blob, rec_size)
    assert len(parsed) == 7
    assert parsed[0] is not None and parsed[0].number == 0
    assert parsed[5] is not None and parsed[5].is_directory
    assert parsed[6] is not None
    assert any(fn.name == "hello.txt" for fn in parsed[6].file_names)

    # pack/unpack 与 parse_record 一致
    direct = [parse_record(blob[i * rec_size : (i + 1) * rec_size], i) for i in range(7)]
    meta, names, link_targets, tags = parallel._pack_chunk(direct)
    back = parallel._unpack_chunk(0, meta, names, link_targets, tags)
    assert len(back) == 7
    assert back[6] is not None
    assert back[6].file_names[0].name == "hello.txt"
    assert back[6].data_size == direct[6].data_size


def test_parse_records_parallel_matches_serial(monkeypatch):
    """强制走 Pool 路径，结果应与串行一致。"""
    from core.mft import parallel

    rec_size = 1024
    n = 40
    parts = []
    for i in range(n):
        parts.append(
            _minimal_file_record(
                number=i,
                name=f"f{i}.txt",
                parent=5 if i != 5 else 5,
                is_dir=(i == 5),
            )
        )
    blob = b"".join(parts)
    serial = parallel.parse_records_serial(blob, rec_size)
    # workers=2 强制走池（小表默认会串行）
    multi = parallel.parse_records_parallel(blob, rec_size, workers=2)
    assert len(multi) == len(serial) == n
    for a, b in zip(serial, multi):
        if a is None and b is None:
            continue
        assert a is not None and b is not None
        assert a.number == b.number
        assert a.in_use == b.in_use
        assert a.is_directory == b.is_directory
        assert a.base_record == b.base_record
        assert a.mtime == b.mtime
        assert a.data_size == b.data_size
        assert len(a.file_names) == len(b.file_names)
        if a.file_names:
            assert a.file_names[0].name == b.file_names[0].name


def test_worker_parse_range_packs_full_meta():
    """worker 直调：pack_into 的值个数必须与 _META 字段数一致。

    少传一个值会抛 struct.error，而上层 pipeline 捕获后静默回落串行解析——
    功能看起来正常，多进程加速却整段失效。故此处直调 worker，不走会兜底的入口。
    """
    from multiprocessing import shared_memory

    from core.mft import parallel

    rec_size = 1024
    recs = [
        _minimal_file_record(number=0, name="$MFT"),
        bytes(rec_size),  # 非 FILE：走 _F_NONE 分支
        _minimal_file_record(number=2, name="a.txt", in_use=False),  # free 记录
        _minimal_file_record(number=3, name="b.txt"),
    ]
    blob = b"".join(recs)
    n = len(recs)

    shm = shared_memory.SharedMemory(create=True, size=len(blob))
    try:
        shm.buf[: len(blob)] = blob
        start, meta, names, link_targets, tags = parallel._worker_parse_range(
            (shm.name, len(blob), 0, n, rec_size, 0, 4096)
        )
    finally:
        shm.close()
        shm.unlink()

    assert start == 0
    assert len(meta) == n * parallel.META_SIZE
    back = parallel._unpack_chunk(0, meta, names, link_targets, tags)
    assert back[1] is None  # 非 FILE
    assert back[2] is None  # free
    assert back[3] is not None
    assert back[3].file_names[0].name == "b.txt"


def test_worker_parse_range_matches_serial_reparse_tag():
    """worker 与串行解析对 reparse_tag 的结果必须一致。"""
    from multiprocessing import shared_memory

    from core.mft import parallel

    rec_size = 1024
    tag = 0xA0000003  # junction
    recs = [
        _minimal_file_record(number=0, name="$MFT"),
        _minimal_file_record(number=1, name="link", is_dir=True, reparse_tag=tag),
    ]
    blob = b"".join(recs)

    serial = parallel.parse_records_serial(blob, rec_size)
    assert serial[1] is not None
    assert serial[1].reparse_tag == tag

    shm = shared_memory.SharedMemory(create=True, size=len(blob))
    try:
        shm.buf[: len(blob)] = blob
        _start, meta, names, link_targets, tags = parallel._worker_parse_range(
            (shm.name, len(blob), 0, len(recs), rec_size, 0, 4096)
        )
    finally:
        shm.close()
        shm.unlink()

    back = parallel._unpack_chunk(0, meta, names, link_targets, tags)
    assert back[1] is not None
    assert back[1].reparse_tag == tag
    assert back[1].has_reparse is True


def test_choose_mft_procs_formula(monkeypatch):
    from core.mft import parallel

    monkeypatch.delenv("WSMC_MFT_WORKERS", raising=False)
    monkeypatch.delenv("WSMC_MFT_PROCS", raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: 12)
    # min(cpu-1=11, n//50k)：百万条约 20 → 11
    assert parallel.choose_mft_procs(1_000_000) == 11
    # 小卷：5 万以下 → 1
    assert parallel.choose_mft_procs(40_000) == 1
    assert parallel.choose_mft_procs(50_000) == 1
    assert parallel.choose_mft_procs(99_999) == 1
    assert parallel.choose_mft_procs(100_000) == 2
    # 环境变量覆盖
    monkeypatch.setenv("WSMC_MFT_PROCS", "4")
    assert parallel.choose_mft_procs(1_000_000) == 4
    monkeypatch.delenv("WSMC_MFT_PROCS", raising=False)
    monkeypatch.setenv("WSMC_MFT_WORKERS", "0")
    assert parallel.choose_mft_procs(1_000_000) == 1


def test_chunk_ranges_more_than_procs(monkeypatch):
    from core.mft.parallel import _chunk_ranges

    ranges = _chunk_ranges(1_000_000, 8)
    assert len(ranges) >= 8 * 3
    assert ranges[0][0] == 0
    assert ranges[-1][1] == 1_000_000
    # 连续无空洞
    for (a, b), (c, d) in zip(ranges, ranges[1:]):
        assert b == c
        assert a < b


def test_build_entry_rows_on_batch_streams():
    """on_batch 时不返回整表，批次数与行数一致。"""
    from core.mft.parse import ParsedRecord, FileNameAttr
    from core.mft.tree import build_entry_rows

    # #5 根 + 若干文件
    parsed: list = [None] * 20
    parsed[5] = ParsedRecord(
        number=5,
        in_use=True,
        is_directory=True,
        base_record=0,
        mtime=1,
        file_names=[FileNameAttr(5, 0, 0, 0, ".", 1)],
        data_size=0,
        has_reparse=False,
    )
    for i in range(6, 16):
        parsed[i] = ParsedRecord(
            number=i,
            in_use=True,
            is_directory=False,
            base_record=0,
            mtime=1,
            file_names=[FileNameAttr(5, 100, 80, 0, f"f{i}.txt", 1)],
            data_size=80,
            has_reparse=False,
        )
    batches: list[list] = []
    rows, fc, dc, total, tot_actual, unrec = build_entry_rows(
        parsed, on_batch=lambda b: batches.append(list(b))
    )
    assert rows == []
    assert fc == 10
    assert dc == 1
    assert total == 800
    assert sum(len(b) for b in batches) == 11  # 根 + 10 文件


def test_compact_table_tree_matches_parsed():
    """紧凑表直通建树与 ParsedRecord 路径计数/总量一致。"""
    from core.mft import parallel
    from core.mft.parse import ParsedRecord, FileNameAttr
    from core.mft.tree import build_entry_rows, build_entry_rows_from_compact

    parsed: list = [None] * 12
    parsed[5] = ParsedRecord(
        number=5,
        in_use=True,
        is_directory=True,
        base_record=0,
        mtime=10,
        file_names=[FileNameAttr(5, 0, 0, 0, ".", 1)],
        data_size=0,
        has_reparse=False,
    )
    parsed[6] = ParsedRecord(
        number=6,
        in_use=True,
        is_directory=True,
        base_record=0,
        mtime=11,
        file_names=[FileNameAttr(5, 0, 0, 0x10, "sub", 1)],
        data_size=0,
        has_reparse=False,
    )
    parsed[7] = ParsedRecord(
        number=7,
        in_use=True,
        is_directory=False,
        base_record=0,
        mtime=12,
        file_names=[FileNameAttr(6, 100, 50, 0, "a.txt", 1)],
        data_size=50,
        has_reparse=False,
    )
    parsed[8] = ParsedRecord(
        number=8,
        in_use=True,
        is_directory=False,
        base_record=0,
        mtime=13,
        file_names=[FileNameAttr(5, 200, 70, 0, "b.txt", 1)],
        data_size=70,
        has_reparse=False,
    )
    meta, names, link_targets, tags = parallel._pack_chunk(parsed)
    # pack 用相对下标；整表 start=0 即全局
    table = parallel.CompactMftTable(
        n_records=len(parsed), meta=meta, names=names, link_targets=link_targets,
        reparse_tags=tags,
    )
    rows_a, fc_a, dc_a, tot_a, _ta, _ua = build_entry_rows(parsed)
    rows_b, fc_b, dc_b, tot_b, _tb, _ub = build_entry_rows_from_compact(table)
    assert (fc_a, dc_a, tot_a) == (fc_b, dc_b, tot_b) == (2, 2, 120)
    assert len(rows_a) == len(rows_b) == 4
    # 根 size 上卷
    assert rows_a[0].size == rows_b[0].size == 120


def test_own_tag_beats_extension_tag():
    """记录自己带标签、扩展也带时，必须是自己那个赢。"""
    from core.mft import parallel
    from core.mft.parse import ParsedRecord, FileNameAttr
    from core.mft.tree import build_entry_rows_from_compact

    parsed: list = [None] * 12
    parsed[5] = ParsedRecord(
        number=5, in_use=True, is_directory=True, base_record=0, mtime=10,
        file_names=[FileNameAttr(5, 0, 0, 0, ".", 1)], data_size=0,
    )
    parsed[6] = ParsedRecord(
        number=6, in_use=True, is_directory=False, base_record=0, mtime=11,
        file_names=[FileNameAttr(5, 100, 50, 0, "own.bin", 1)], data_size=50,
        has_reparse=True, reparse_tag=0xA0000003,
    )
    parsed[7] = ParsedRecord(
        number=7, in_use=True, is_directory=False, base_record=6, mtime=11,
        file_names=[], data_size=0, has_reparse=True, reparse_tag=0x80000017,
    )
    meta, names, link_targets, tags = parallel._pack_chunk(parsed)
    table = parallel.CompactMftTable(
        n_records=len(parsed), meta=meta, names=names, link_targets=link_targets,
        reparse_tags=tags,
    )
    rows, *_ = build_entry_rows_from_compact(table)
    node = next(r for r in rows if r.name == "own.bin")
    assert node.reparse_tag == 0xA0000003


def test_extension_link_target_reaches_base_record():
    """链接目标只落在扩展记录上时，要回填到主记录。"""
    from core.mft import parallel
    from core.mft.parse import ParsedRecord, FileNameAttr
    from core.mft.tree import build_entry_rows_from_compact

    parsed: list = [None] * 12
    parsed[5] = ParsedRecord(
        number=5, in_use=True, is_directory=True, base_record=0, mtime=10,
        file_names=[FileNameAttr(5, 0, 0, 0, ".", 1)], data_size=0,
    )
    parsed[6] = ParsedRecord(
        number=6, in_use=True, is_directory=True, base_record=0, mtime=11,
        file_names=[FileNameAttr(5, 0, 0, 0x10, "linked", 1)], data_size=0,
    )
    parsed[7] = ParsedRecord(
        number=7, in_use=True, is_directory=False, base_record=6, mtime=11,
        file_names=[], data_size=0, has_reparse=True,
        reparse_tag=0xA0000003, link_target=r"D:\目标目录",
    )
    meta, names, link_targets, tags = parallel._pack_chunk(parsed)
    table = parallel.CompactMftTable(
        n_records=len(parsed), meta=meta, names=names, link_targets=link_targets,
        reparse_tags=tags,
    )
    rows, *_ = build_entry_rows_from_compact(table)
    node = next(r for r in rows if r.name == "linked")
    assert node.link_target == r"D:\目标目录"
    assert node.reparse_tag == 0xA0000003


def test_actual_size_rolls_up_to_root():
    """各文件的实际占用要上卷成目录的合计，未记录的不当 0 参与。"""
    from core.mft import parallel
    from core.mft.parse import ParsedRecord, FileNameAttr
    from core.mft.tree import build_entry_rows_from_compact

    parsed: list = [None] * 12
    parsed[5] = ParsedRecord(
        number=5, in_use=True, is_directory=True, base_record=0, mtime=10,
        file_names=[FileNameAttr(5, 0, 0, 0, ".", 1)], data_size=0,
    )
    parsed[6] = ParsedRecord(
        number=6, in_use=True, is_directory=True, base_record=0, mtime=11,
        file_names=[FileNameAttr(5, 0, 0, 0x10, "sub", 1)], data_size=0,
    )
    parsed[7] = ParsedRecord(
        number=7, in_use=True, is_directory=False, base_record=0, mtime=12,
        file_names=[FileNameAttr(6, 100, 50, 0, "a.bin", 1)], data_size=50,
        actual_size=4096,
    )
    parsed[8] = ParsedRecord(
        number=8, in_use=True, is_directory=False, base_record=0, mtime=13,
        file_names=[FileNameAttr(5, 200, 40, 0, "b.bin", 1)], data_size=40,
        actual_size=8192,
    )
    meta, names, link_targets, tags = parallel._pack_chunk(parsed)
    table = parallel.CompactMftTable(
        n_records=len(parsed), meta=meta, names=names, link_targets=link_targets,
        reparse_tags=tags,
    )
    rows, _fc, _dc, _tot, total_actual, unrecorded = build_entry_rows_from_compact(table)
    by_name = {r.name: r for r in rows}
    assert by_name["sub"].actual_size == 4096
    root_row = rows[0]
    assert root_row.actual_size == 4096 + 8192
    assert total_actual == 4096 + 8192
    assert unrecorded == 0


def test_folded_names_handle_multibyte():
    """名字里有中文时，按字节切片取出的文本要完整，不能切坏。"""
    from core.mft import parallel
    from core.mft.pack import CompactMftTable, CompactNames, FoldNames
    from core.mft.parse import ParsedRecord, FileNameAttr
    from core.mft.tree import build_entry_rows_from_compact

    parsed: list = [None] * 12
    parsed[5] = ParsedRecord(
        number=5, in_use=True, is_directory=True, base_record=0, mtime=10,
        file_names=[FileNameAttr(5, 0, 0, 0, ".", 1)], data_size=0,
    )
    parsed[6] = ParsedRecord(
        number=6, in_use=True, is_directory=False, base_record=0, mtime=11,
        file_names=[FileNameAttr(5, 0, 0, 0, "测试文件.txt", 1)], data_size=50,
    )
    parsed[7] = ParsedRecord(
        number=7, in_use=True, is_directory=False, base_record=0, mtime=12,
        file_names=[FileNameAttr(5, 0, 0, 0, "缓存备份.tar", 1)], data_size=70,
    )

    meta, names, link_targets, tags = parallel._pack_chunk(parsed)
    fold = FoldNames()
    fold.add(names)
    table = CompactMftTable(
        n_records=len(parsed), meta=meta, names=CompactNames(),
        link_targets=link_targets, reparse_tags=tags, fold=fold,
    )
    rows, *_ = build_entry_rows_from_compact(table)
    got = {r.name for r in rows}
    assert "测试文件.txt" in got
    assert "缓存备份.tar" in got


def test_folded_names_reach_entry_rows():
    """逐段折叠的名字表（fold 非空）建出来要有正确名字与大小。

    这条路径以前没有任何测试：建树函数没收到 fold 参数，走的是
    逗号为整表名字表的分支，所以「按需取名字」那段代码一直没被执行。
    """
    from core.mft import parallel
    from core.mft.pack import CompactMftTable, CompactNames, FoldNames
    from core.mft.parse import ParsedRecord, FileNameAttr
    from core.mft.tree import build_entry_rows, build_entry_rows_from_compact

    parsed: list = [None] * 12
    parsed[5] = ParsedRecord(
        number=5, in_use=True, is_directory=True, base_record=0, mtime=10,
        file_names=[FileNameAttr(5, 0, 0, 0, ".", 1)], data_size=0,
    )
    parsed[6] = ParsedRecord(
        number=6, in_use=True, is_directory=True, base_record=0, mtime=11,
        file_names=[FileNameAttr(5, 0, 0, 0x10, "sub", 1)], data_size=0,
    )
    parsed[7] = ParsedRecord(
        number=7, in_use=True, is_directory=False, base_record=0, mtime=12,
        file_names=[FileNameAttr(6, 100, 50, 0, "a.txt", 1)], data_size=50,
    )
    parsed[8] = ParsedRecord(
        number=8, in_use=True, is_directory=False, base_record=0, mtime=13,
        file_names=[FileNameAttr(5, 200, 70, 0, "b.txt", 1)], data_size=70,
    )

    meta, names, link_targets, tags = parallel._pack_chunk(parsed)
    fold = FoldNames()
    fold.add(names)
    table = CompactMftTable(
        n_records=len(parsed), meta=meta, names=CompactNames(),
        link_targets=link_targets, reparse_tags=tags, fold=fold,
    )

    rows_a, fc_a, dc_a, tot_a, _x, _y = build_entry_rows(parsed)
    rows_b, fc_b, dc_b, tot_b, _z, _w = build_entry_rows_from_compact(table)
    assert (fc_a, dc_a, tot_a) == (fc_b, dc_b, tot_b) == (2, 2, 120)
    assert sorted(r.name for r in rows_b) == sorted(r.name for r in rows_a)
    assert {"sub", "a.txt", "b.txt"} <= {r.name for r in rows_b}
    # 名字现切不等于空串，且大小没有丢
    by_name = {r.name: r for r in rows_b}
    assert by_name["a.txt"].size == 50
    assert by_name["b.txt"].size == 70
    assert rows_b[0].size == 120


def test_compact_table_carries_link_target():
    """紧凑路径（build_entry_rows_from_compact）的链接目标必须一路带到 EntryRow。"""
    from core.mft import parallel
    from core.mft.parse import ParsedRecord, FileNameAttr
    from core.mft.tree import build_entry_rows_from_compact

    parsed: list = [None] * 12
    parsed[5] = ParsedRecord(
        number=5, in_use=True, is_directory=True, base_record=0, mtime=10,
        file_names=[FileNameAttr(5, 0, 0, 0, ".", 1)], data_size=0,
    )
    parsed[6] = ParsedRecord(
        number=6, in_use=True, is_directory=True, base_record=0, mtime=11,
        file_names=[FileNameAttr(5, 0, 0, 0x10, "link", 1)], data_size=0,
        has_reparse=True, reparse_tag=0xA0000003,
        link_target=r"D:\360download\目标",
    )
    meta, names, link_targets, tags = parallel._pack_chunk(parsed)
    table = parallel.CompactMftTable(
        n_records=len(parsed), meta=meta, names=names, link_targets=link_targets,
        reparse_tags=tags,
    )
    rows, _fc, _dc, _tot, _ta, _ua = build_entry_rows_from_compact(table)
    link = next(r for r in rows if r.name == "link")
    assert link.reparse_tag == 0xA0000003
    assert link.link_target == r"D:\360download\目标"

    # 同一趟就把链接回调出去；链接在根下，路径从记录号爬父链现算
    seen: list[tuple[str, int, str]] = []
    build_entry_rows_from_compact(
        table, on_link=lambda rel, tag, raw: seen.append((rel, tag, raw))
    )
    assert seen == [("link", 0xA0000003, r"D:\360download\目标")]


def test_huge_base_record_does_not_land_on_neighbour():
    """超出记录总数的基记录号要原样保留，不能截断后挂到别的文件上。"""
    from core.mft import parallel
    from core.mft.parse import ParsedRecord, FileNameAttr
    from core.mft.tree import build_entry_rows_from_compact

    parsed: list = [None] * 12
    parsed[5] = ParsedRecord(
        number=5, in_use=True, is_directory=True, base_record=0, mtime=10,
        file_names=[FileNameAttr(5, 0, 0, 0, ".", 1)], data_size=0,
    )
    parsed[6] = ParsedRecord(
        number=6, in_use=True, is_directory=False, base_record=0, mtime=11,
        file_names=[FileNameAttr(5, 100, 50, 0, "real.txt", 1)], data_size=50,
    )
    # 低 4 字节刚好等于 6：若存成 4 字节就会被当成记录 6 的扩展记录
    parsed[7] = ParsedRecord(
        number=7, in_use=True, is_directory=False,
        base_record=0x1_0000_0006, mtime=12,
        file_names=[FileNameAttr(5, 0, 0, 0, "ghost.txt", 1)], data_size=10,
    )

    meta, names, link_targets, tags = parallel._pack_chunk(parsed)
    back = parallel._unpack_chunk(0, meta, names, link_targets, tags)
    assert back[7] is not None
    assert back[7].base_record == 0x1_0000_0006  # 八字节完整保留

    table = parallel.CompactMftTable(
        n_records=len(parsed), meta=meta, names=names, link_targets=link_targets,
        reparse_tags=tags,
    )
    rows, fc, dc, tot, _ta, _ua = build_entry_rows_from_compact(table)
    names_out = [r.name for r in rows]
    assert "ghost.txt" not in names_out  # 指向不存在的记录，被过滤
    assert "real.txt" in names_out
    assert (fc, dc, tot) == (1, 1, 50)  # 那个假文件既不计数也不计入大小


def test_large_reparse_tag_goes_to_side_list():
    """重解析标签走旁路后仍能回到记录上（扩到 4 字节上限的标签值）。"""
    from core.mft import parallel
    from core.mft.parse import ParsedRecord, FileNameAttr
    from core.mft.tree import build_entry_rows_from_compact

    parsed: list = [None] * 12
    parsed[5] = ParsedRecord(
        number=5, in_use=True, is_directory=True, base_record=0, mtime=10,
        file_names=[FileNameAttr(5, 0, 0, 0, ".", 1)], data_size=0,
    )
    parsed[6] = ParsedRecord(
        number=6, in_use=True, is_directory=False, base_record=0, mtime=11,
        file_names=[FileNameAttr(5, 0, 0, 0, "wof.bin", 1)], data_size=0,
        has_reparse=True, reparse_tag=0x80000017,
    )
    meta, names, link_targets, tags = parallel._pack_chunk(parsed)
    assert tags == [(6, 0x80000017)]
    table = parallel.CompactMftTable(
        n_records=len(parsed), meta=meta, names=names, link_targets=link_targets,
        reparse_tags=tags,
    )
    rows, *_ = build_entry_rows_from_compact(table)
    node = next(r for r in rows if r.name == "wof.bin")
    assert node.reparse_tag == 0x80000017


def test_compact_from_shared_memory_collector_runs():
    """整表多进程收集器（旧接口）能跑通，并把旁路带回表里。"""
    from multiprocessing import shared_memory

    from core.mft import collect

    rec_size = 1024
    recs = []
    for i in range(8):
        recs.append(
            _minimal_file_record(
                number=i,
                name=f"f{i}.txt",
                parent=5,
                reparse_tag=0xA0000003 if i == 3 else 0,
            )
        )
    blob = b"".join(recs)

    shm = shared_memory.SharedMemory(create=True, size=len(blob))
    try:
        shm.buf[: len(blob)] = blob
        table = collect.collect_compact_from_shared_memory(
            shm, len(blob), rec_size, workers=2
        )
    finally:
        shm.close()
        shm.unlink()

    assert table.n_records == 8
    assert table.reparse_tags == [(3, 0xA0000003)]
    assert len(table.names) == 8


def test_streaming_collector_runs_and_keeps_tags():
    """流式收集器（大卷热路径）：按段通知后收齐结果，旁路不丢。"""
    from multiprocessing import shared_memory

    from core.mft import collect, parallel

    rec_size = 1024
    recs = []
    for i in range(8):
        recs.append(
            _minimal_file_record(
                number=i,
                name=f"f{i}.txt",
                parent=5,
                reparse_tag=0xA0000003 if i == 5 else 0,
            )
        )
    blob = b"".join(recs)

    shm = shared_memory.SharedMemory(create=True, size=len(blob))
    try:
        shm.buf[: len(blob)] = blob
        collector = collect.StreamingCompactCollector(
            shm, len(blob), rec_size, workers=2
        )
        assert collector.can_stream
        # 按段通知，模拟读盘逐步写入
        half = (len(blob) // 2) // rec_size * rec_size
        collector.notify_range(0, half)
        collector.notify_range(half, len(blob) - half)
        table = collector.finish()
    finally:
        shm.close()
        shm.unlink()

    assert table.n_records == 8
    assert table.reparse_tags == [(5, 0xA0000003)]
    assert len(table.names) == 8
    back = parallel._unpack_chunk(
        0, table.meta, table.names, table.link_targets, table.reparse_tags
    )
    assert back[5] is not None and back[5].reparse_tag == 0xA0000003


def test_meta_layout_is_pinned():
    """元信息布局钉死：字段顺序或宽度变化都会让这条测试失败。"""
    import struct

    from core.mft import parallel

    assert parallel.META_SIZE == 30
    # 直接锚住布局串：字段换位、宽度互换都会在这里失败，
    # 而「打包再解包得到同一组值」对任何合法结构都恒成立，拦不住这类错误
    assert parallel.META.format == "<BBQIQq"
    assert struct.calcsize("<BBQIQq") == 30
    # 建树时靠「标志在第 0 字节」做快速跳过，单独钉一下
    assert parallel.META.pack(0x81, 0, 0, 0, 0, -1)[0] == 0x81


def test_serial_to_compact_carries_tags():
    """串行打包（小表 / 回退路径）也要把旁路带进表里。"""
    from core.mft import parallel, worker

    rec_size = 1024
    recs = [
        _minimal_file_record(number=5, name=".", is_dir=True, parent=5),
        _minimal_file_record(
            number=6, name="link", is_dir=True, reparse_tag=0xA0000003
        ),
    ]
    table = worker._serial_to_compact(b"".join(recs), rec_size)
    assert table.n_records == 2
    assert table.reparse_tags == [(1, 0xA0000003)]  # 段内相对下标

    back = parallel._unpack_chunk(
        0, table.meta, table.names, table.link_targets, table.reparse_tags
    )
    assert back[1] is not None
    assert back[1].reparse_tag == 0xA0000003


def test_reparse_tag_on_extension_reaches_base_record():
    """标签落在扩展记录上时，要回填到它对应的主记录。"""
    from core.mft import parallel
    from core.mft.parse import ParsedRecord, FileNameAttr
    from core.mft.tree import build_entry_rows_from_compact

    parsed: list = [None] * 12
    parsed[5] = ParsedRecord(
        number=5, in_use=True, is_directory=True, base_record=0, mtime=10,
        file_names=[FileNameAttr(5, 0, 0, 0, ".", 1)], data_size=0,
    )
    parsed[6] = ParsedRecord(
        number=6, in_use=True, is_directory=False, base_record=0, mtime=11,
        file_names=[FileNameAttr(5, 100, 50, 0, "wof.bin", 1)], data_size=50,
    )
    # 扩展记录：自己没名字，带压缩标签，指回记录 6
    parsed[7] = ParsedRecord(
        number=7, in_use=True, is_directory=False, base_record=6, mtime=11,
        file_names=[], data_size=0, has_reparse=True,
        reparse_tag=0x80000017,
    )
    meta, names, link_targets, tags = parallel._pack_chunk(parsed)
    table = parallel.CompactMftTable(
        n_records=len(parsed), meta=meta, names=names, link_targets=link_targets,
        reparse_tags=tags,
    )
    rows, *_ = build_entry_rows_from_compact(table)
    node = next(r for r in rows if r.name == "wof.bin")
    assert node.reparse_tag == 0x80000017


def test_two_extension_tags_pick_lowest_record():
    """同一条主记录挂两条扩展、标签不同时，结果不能随清单到达顺序变。"""
    from core.mft import parallel
    from core.mft.parse import ParsedRecord, FileNameAttr
    from core.mft.tree import build_entry_rows_from_compact

    def build(tag_order: list[int]) -> int:
        parsed: list = [None] * 12
        parsed[5] = ParsedRecord(
            number=5, in_use=True, is_directory=True, base_record=0, mtime=10,
            file_names=[FileNameAttr(5, 0, 0, 0, ".", 1)], data_size=0,
        )
        parsed[6] = ParsedRecord(
            number=6, in_use=True, is_directory=False, base_record=0, mtime=11,
            file_names=[FileNameAttr(5, 100, 50, 0, "dup.bin", 1)], data_size=50,
        )
        for num in (7, 8):
            parsed[num] = ParsedRecord(
                number=num, in_use=True, is_directory=False, base_record=6, mtime=11,
                file_names=[], data_size=0, has_reparse=True,
                reparse_tag=0x80000000 + num,
            )
        meta, names, link_targets, tags = parallel._pack_chunk(parsed)
        # 模拟多进程按完成顺序拼接，则是任意顺序
        by_num = {rel: tag for rel, tag in tags}
        ordered = [(rel, by_num[rel]) for rel in tag_order if rel in by_num]
        table = parallel.CompactMftTable(
            n_records=len(parsed), meta=meta, names=names,
            link_targets=link_targets, reparse_tags=ordered,
        )
        rows, *_ = build_entry_rows_from_compact(table)
        return next(r for r in rows if r.name == "dup.bin").reparse_tag

    # 不管清单先到哪一条，都取记录号最小的那条（与旧路径按记录号升序一致）
    assert build([7, 8]) == build([8, 7]) == 0x80000007


def test_broken_size_above_limit_is_not_written():
    """超过卷容量上限的记录大小不能当成真数字写进快照。"""
    from core.mft import parallel
    from core.mft.parse import ParsedRecord, FileNameAttr
    from core.mft.tree import build_entry_rows_from_compact

    broken = 1 << 63  # 比 4 EiB 上限大一倍，只可能是损坏值
    parsed: list = [None] * 12
    parsed[5] = ParsedRecord(
        number=5, in_use=True, is_directory=True, base_record=0, mtime=10,
        file_names=[FileNameAttr(5, 0, 0, 0, ".", 1)], data_size=0,
    )
    parsed[6] = ParsedRecord(
        number=6, in_use=True, is_directory=False, base_record=0, mtime=11,
        file_names=[FileNameAttr(5, 0, 0, 0, "broken.bin", 1)], data_size=broken,
    )
    parsed[7] = ParsedRecord(
        number=7, in_use=True, is_directory=False, base_record=0, mtime=12,
        file_names=[FileNameAttr(5, 0, 0, 0, "good.bin", 1)], data_size=4096,
    )
    meta, names, link_targets, tags = parallel._pack_chunk(parsed)
    table = parallel.CompactMftTable(
        n_records=len(parsed), meta=meta, names=names, link_targets=link_targets,
        reparse_tags=tags,
    )
    rows, _fc, _dc, total, _ta, _ua = build_entry_rows_from_compact(table)
    by_name = {r.name: r for r in rows}
    # 损坏的那条按未采集记 0，不能变成 8 EiB 这种看着合法的数字
    assert by_name["broken.bin"].size == 0
    assert by_name["good.bin"].size == 4096
    assert total == 4096


def test_huge_sizes_do_not_crash_tree():
    """损坏记录报出接近上限的大小时，建树不能抛异常把整次扫描打回去。"""
    from core.mft import parallel
    from core.mft import tree as tree_mod
    from core.mft.parse import ParsedRecord, FileNameAttr
    from core.mft.tree import build_entry_rows_from_compact

    huge = 1 << 62
    parsed: list = [None] * 12
    parsed[5] = ParsedRecord(
        number=5, in_use=True, is_directory=True, base_record=0, mtime=10,
        file_names=[FileNameAttr(5, 0, 0, 0, ".", 1)], data_size=0,
    )
    for num, nm in ((6, "a.bin"), (7, "b.bin")):
        parsed[num] = ParsedRecord(
            number=num, in_use=True, is_directory=False, base_record=0, mtime=11,
            file_names=[FileNameAttr(5, 100, 50, 0, nm, 1)], data_size=huge,
        )
    meta, names, link_targets, tags = parallel._pack_chunk(parsed)
    table = parallel.CompactMftTable(
        n_records=len(parsed), meta=meta, names=names, link_targets=link_targets,
        reparse_tags=tags,
    )
    rows, fc, _dc, total, _ta, _ua = build_entry_rows_from_compact(table)
    assert fc == 2
    # 两条 2^62 加起来已超 8 字节有符号数组上限，根夹住即可
    assert total == tree_mod._SIZE_MAX
    # 每条文件自己的大小还是原值，不能被夹取改小
    assert sorted(r.size for r in rows if not r.is_dir) == [huge, huge]


def test_parse_serial_with_usa_flag_on_bytes():
    """bytes_per_sector>0 时只读 bytes 可拷贝后 USA（无 USA 数据时仍可解析）。"""
    from core.mft import parallel

    rec_size = 1024
    parts = [
        _minimal_file_record(number=i, name=f"f{i}.txt")
        for i in range(8)
    ]
    blob = b"".join(parts)
    out = parallel.parse_records_serial(
        blob, rec_size, bytes_per_sector=512
    )
    assert len(out) == 8
    assert out[0] is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows only")
def test_mft_live_optional(tmp_path, monkeypatch):
    """仅当 WSMC_TEST_MFT=1 时跑真实 C:\\ MFT 全扫。"""
    if os.environ.get("WSMC_TEST_MFT", "").strip() not in ("1", "true", "yes"):
        pytest.skip("set WSMC_TEST_MFT=1 to run live MFT scan")
    monkeypatch.setenv("WSMC_USE_MFT", "1")
    assert is_mft_eligible("C:\\") is True
    from core.mft import scan_mft_to_snapshot

    db = tmp_path / "mft.db"
    meta = scan_mft_to_snapshot("C:\\", str(db))
    assert meta.file_count > 1000
    assert meta.dir_count > 100
    assert meta.total_size > 0
    assert os.path.isfile(db)


def _rec(number: int, is_dir: bool, parent: int, name: str,
         data: int = 0, actual: int | None = None) -> "object":
    from core.mft.parse import ParsedRecord, FileNameAttr

    return ParsedRecord(
        number=number,
        in_use=True,
        is_directory=is_dir,
        base_record=0,
        mtime=1,
        file_names=[FileNameAttr(parent, 0, 0, 0, name, 1)],
        data_size=data,
        has_reparse=False,
        actual_size=actual,
    )


def test_directory_rollup_actual_size():
    """目录行的实际占用等于子项之和，未记录的项不参与累加。"""
    from core.mft.tree import build_entry_rows

    parsed: list = [None] * 12
    parsed[5] = _rec(5, True, 5, ".", 0, None)          # 根
    parsed[6] = _rec(6, False, 5, "a.bin", 100, 100)
    parsed[7] = _rec(7, False, 5, "b.bin", 200, 200)
    parsed[8] = _rec(8, True, 5, "sub", 0, None)        # 子目录
    parsed[9] = _rec(9, False, 8, "c.bin", 300, 300)
    parsed[10] = _rec(10, False, 5, "unknown.bin", 400, None)

    rows, fc, dc, total, tot_actual, unrec = build_entry_rows(parsed)

    by_name = {r.name: r for r in rows}
    assert by_name["a.bin"].actual_size == 100
    assert by_name["c.bin"].actual_size == 300
    assert by_name["unknown.bin"].actual_size is None
    assert by_name["sub"].actual_size == 300
    root = next(r for r in rows if r.id == 1)
    assert root.actual_size == 600
    assert tot_actual == 600
    assert unrec == 1


def test_directory_rollup_all_unrecorded_stays_none():
    """子项全都未记录时，目录本身也保持未记录，不能记成 0。"""
    from core.mft.tree import build_entry_rows

    parsed: list = [None] * 8
    parsed[5] = _rec(5, True, 5, ".", 0, None)
    parsed[6] = _rec(6, False, 5, "x.bin", 10, None)
    parsed[7] = _rec(7, False, 5, "y.bin", 20, None)

    rows, _fc, _dc, _total, tot_actual, unrec = build_entry_rows(parsed)

    by_name = {r.name: r for r in rows}
    root = next(r for r in rows if r.id == 1)
    assert root.actual_size is None
    assert tot_actual == 0
    assert unrec == 2


def test_extension_merge_picks_actual_by_source_not_by_size():
    """合并扩展记录时，占用跟着来源可信度走，不跟着数据流大小走。

    透明压缩文件的空壳主流比真实数据大，按大小选会选中空壳，
    拿到的就是原体积而不是占用。
    """
    from core.mft import parallel
    from core.mft.parse import (
        ACTUAL_SRC_SPARSE, ACTUAL_SRC_WOF, FileNameAttr, ParsedRecord,
    )
    from core.mft.tree import build_entry_rows, build_entry_rows_from_compact

    def _build():
        parsed: list = [None] * 12
        parsed[5] = ParsedRecord(
            number=5, in_use=True, is_directory=True, base_record=0, mtime=10,
            file_names=[FileNameAttr(5, 0, 0, 0, ".", 1)], data_size=0,
        )
        # 主记录：小数据流、占用虚高
        parsed[6] = ParsedRecord(
            number=6, in_use=True, is_directory=False, base_record=0, mtime=11,
            file_names=[FileNameAttr(5, 0, 0, 0, "a.bin", 1)],
            data_size=50, actual_size=100, actual_src=ACTUAL_SRC_SPARSE,
        )
        # 扩展记录：大数据流、占用较小，应当整体胜出
        parsed[7] = ParsedRecord(
            number=7, in_use=True, is_directory=False, base_record=6, mtime=0,
            file_names=[], data_size=200, actual_size=80,
            actual_src=ACTUAL_SRC_WOF,
        )
        return parsed

    parsed = _build()
    meta, names, link_targets, tags = parallel._pack_chunk(parsed)
    table = parallel.CompactMftTable(
        n_records=len(parsed), meta=meta, names=names, link_targets=link_targets,
        reparse_tags=tags,
    )
    rows_c, *_ = build_entry_rows_from_compact(table)
    rows_p, *_ = build_entry_rows(_build())

    for rows in (rows_c, rows_p):
        node = next(r for r in rows if r.name == "a.bin")
        assert node.size == 200, rows is rows_c
        assert node.actual_size == 80, rows is rows_c


def _data_attr(
    off, *, non_resident, alloc=None, real=None, compressed_size=None,
    compressed=False, lowest_vcn=0, stream=None, sparse=False, runs=None,
):
    """在 buf 逻辑上描述一条 $DATA 属性，返回 (字节, 长度)。

    stream 为 None 表示未命名主流；否则是命名流的 UTF-16LE 名字。
    """
    name_b = b"" if stream is None else stream.encode("utf-16-le")
    name_len = 0 if stream is None else len(stream)
    if non_resident:
        head_len = 0x48 if compressed else 0x40
        data_off = head_len + len(name_b)
        runs_b = runs or b""
        alen = data_off + len(runs_b)
        if alen % 8:
            alen += 8 - (alen % 8)
        b = bytearray(alen)
        struct.pack_into("<I", b, 0x00, 0x80)
        struct.pack_into("<I", b, 0x04, alen)
        b[0x08] = 1
        b[0x09] = name_len
        struct.pack_into("<H", b, 0x0A, head_len if name_b else 0)
        struct.pack_into("<H", b, 0x0C, 0x0001 if compressed else 0)
        if sparse:
            # 稀疏位与压缩位同在 0x0C，可共存
            struct.pack_into(
                "<H", b, 0x0C, struct.unpack_from("<H", b, 0x0C)[0] | 0x8000
            )
        struct.pack_into("<q", b, 0x10, lowest_vcn)
        struct.pack_into("<q", b, 0x18, lowest_vcn)
        struct.pack_into("<H", b, 0x20, data_off if runs_b else 0xFFFF)
        struct.pack_into("<q", b, 0x28, alloc if alloc is not None else 0)
        struct.pack_into("<q", b, 0x30, real if real is not None else 0)
        struct.pack_into("<q", b, 0x38, real if real is not None else 0)
        if compressed:
            struct.pack_into("<q", b, 0x40, compressed_size or 0)
        if name_b:
            b[head_len : head_len + len(name_b)] = name_b
        if runs_b:
            b[data_off : data_off + len(runs_b)] = runs_b
        return bytes(b)
    body = b"" if stream is None else b""
    alen = 0x18 + len(body) + len(name_b)
    if alen % 8:
        alen += 8 - (alen % 8)
    b = bytearray(alen)
    struct.pack_into("<I", b, 0x00, 0x80)
    struct.pack_into("<I", b, 0x04, alen)
    b[0x08] = 0
    b[0x09] = name_len
    struct.pack_into("<H", b, 0x0A, 0x18 if name_b else 0)
    struct.pack_into("<I", b, 0x10, len(body))
    struct.pack_into("<H", b, 0x14, 0x18)
    if name_b:
        b[0x18 : 0x18 + len(name_b)] = name_b
    return bytes(b)


def _record_with_streams(*, number, name="a.bin", parent=5, rec_size=2048,
                         streams=(), reparse_tag=None):
    """构造带若干 $DATA 流的记录，供实际占用采集的各个分支测试。"""
    buf = bytearray(rec_size)
    buf[0:4] = b"FILE"
    struct.pack_into("<H", buf, 0x14, 0x38)
    struct.pack_into("<H", buf, 0x16, 0x0001)
    struct.pack_into("<I", buf, 0x2C, number)
    struct.pack_into("<Q", buf, 0x20, 0)
    off = 0x38

    # STANDARD_INFORMATION
    si_vsize = 0x28
    si_alen = 0x18 + si_vsize
    struct.pack_into("<I", buf, off, 0x10)
    struct.pack_into("<I", buf, off + 4, si_alen)
    struct.pack_into("<I", buf, off + 0x10, si_vsize)
    struct.pack_into("<H", buf, off + 0x14, 0x18)
    off += si_alen

    # FILE_NAME
    name_bytes = name.encode("utf-16-le")
    fn_vsize = 0x42 + len(name_bytes)
    fn_alen = 0x18 + fn_vsize
    if fn_alen % 8:
        fn_alen += 8 - (fn_alen % 8)
    struct.pack_into("<I", buf, off, 0x30)
    struct.pack_into("<I", buf, off + 4, fn_alen)
    struct.pack_into("<I", buf, off + 0x10, fn_vsize)
    struct.pack_into("<H", buf, off + 0x14, 0x18)
    v = off + 0x18
    struct.pack_into("<Q", buf, v, parent)
    buf[v + 0x40] = len(name)
    buf[v + 0x41] = 1
    buf[v + 0x42 : v + 0x42 + len(name_bytes)] = name_bytes
    off += fn_alen

    for st in streams:
        blob = _data_attr(off, **st)
        buf[off : off + len(blob)] = blob
        off += len(blob)

    if reparse_tag:
        content = bytearray(16)
        struct.pack_into("<I", content, 0, reparse_tag)
        struct.pack_into("<H", content, 4, 8)
        rp_alen = 0x18 + len(content)
        if rp_alen % 8:
            rp_alen += 8 - (rp_alen % 8)
        struct.pack_into("<I", buf, off, 0xC0)
        struct.pack_into("<I", buf, off + 4, rp_alen)
        struct.pack_into("<I", buf, off + 0x10, len(content))
        struct.pack_into("<H", buf, off + 0x14, 0x18)
        buf[off + 0x18 : off + 0x18 + len(content)] = content
        off += rp_alen

    struct.pack_into("<I", buf, off, 0xFFFFFFFF)
    return bytes(buf)


def test_actual_size_plain_nonresident():
    """普通文件取主流分配大小。"""
    from core.mft.parse import parse_record

    rec = _record_with_streams(
        number=6, streams=[dict(non_resident=True, alloc=4096, real=1000)]
    )
    p = parse_record(rec, 6)
    assert p.actual_size == 4096
    assert p.data_size == 1000


def test_actual_size_ignores_continuation_segment():
    """非首段的 0x28 不是真实值，不能当占用。"""
    from core.mft.parse import parse_record

    rec = _record_with_streams(
        number=6,
        streams=[dict(non_resident=True, alloc=0xDEAD, real=1000, lowest_vcn=7)],
    )
    p = parse_record(rec, 6)
    assert p.actual_size is None


def test_actual_size_resident_stream_is_zero():
    """数据直接写在记录里的小文件确实占 0 个簇。"""
    from core.mft.parse import parse_record

    rec = _record_with_streams(
        number=6, streams=[dict(non_resident=False)]
    )
    p = parse_record(rec, 6)
    assert p.actual_size == 0


def test_actual_size_compressed_reads_extended_header():
    """NTFS 压缩流必须读扩展头的压缩后大小，不是 0x28。"""
    from core.mft.parse import parse_record

    rec = _record_with_streams(
        number=6,
        streams=[dict(non_resident=True, alloc=4096, real=3200000,
                      compressed=True, compressed_size=204800)],
    )
    p = parse_record(rec, 6)
    assert p.actual_size == 204800


def test_actual_size_compressed_continuation_is_none():
    """压缩标志在非首段出现时不读扩展头。"""
    from core.mft.parse import parse_record

    rec = _record_with_streams(
        number=6,
        streams=[dict(non_resident=True, alloc=4096, real=1000, compressed=True,
                      compressed_size=100, lowest_vcn=3)],
    )
    p = parse_record(rec, 6)
    assert p.actual_size is None


def test_actual_size_wof_uses_attached_stream():
    """透明压缩文件取附属流分配，不能取被掏空的主流。"""
    from core.mft.parse import parse_record

    rec = _record_with_streams(
        number=6,
        streams=[
            dict(non_resident=True, alloc=131072, real=67464),
            dict(non_resident=True, alloc=45056, real=42735,
                 stream="WofCompressedData"),
        ],
    )
    p = parse_record(rec, 6)
    assert p.actual_size == 45056


def test_actual_size_ignores_other_named_stream():
    """名字长度相同但不是 WofCompressedData 的流不参与判定。"""
    from core.mft.parse import parse_record

    rec = _record_with_streams(
        number=6,
        streams=[
            dict(non_resident=True, alloc=8192, real=8000),
            dict(non_resident=True, alloc=999, real=999, stream="SomeOtherStream17"),
        ],
    )
    p = parse_record(rec, 6)
    assert p.actual_size == 8192


def test_actual_size_wof_without_allocation_is_unrecorded():
    """附属流存在但读不到分配大小时记未采集，不能回落到虚高的主流。"""
    from core.mft.parse import parse_record

    rec = _record_with_streams(
        number=6,
        streams=[
            dict(non_resident=True, alloc=131072, real=67464),
            dict(non_resident=True, alloc=45056, real=42735,
                 stream="WofCompressedData", lowest_vcn=2),
        ],
    )
    p = parse_record(rec, 6)
    # 附属流不是首段，取不到它的分配大小；此时主流那个 131072 是空壳名义值
    assert p.actual_size is None


def test_wof_stream_in_extension_record_keeps_attached_alloc():
    """透明压缩文件的附属流落在扩展记录时，占用要取附属流那份。

    基础记录只有主流，它是空洞的名义值；按数据流大小挑赢家的规则会让
    扩展记录输掉，于是名义值被当成真实占用。
    """
    from core.mft.parse import IO_REPARSE_TAG_WOF, FileNameAttr, ParsedRecord
    from core.mft.tree import build_entry_rows

    def rec(number, *, base_record=0, data=0, actual=None, name="a.bin"):
        return ParsedRecord(
            number=number, in_use=True, is_directory=False,
            base_record=base_record, mtime=0.0,
            file_names=[FileNameAttr(
                parent_ref=5, allocated_size=0, real_size=data,
                flags=0, name=name, namespace=1)],
            data_size=data, has_reparse=bool(actual is None),
            reparse_tag=IO_REPARSE_TAG_WOF if number == 6 else 0,
            link_target="", actual_size=actual,
        )

    root = ParsedRecord(
        number=5, in_use=True, is_directory=True, base_record=0, mtime=0.0,
        file_names=[], data_size=0, has_reparse=False, reparse_tag=0,
        link_target="", actual_size=None,
    )
    records = [
        root,
        rec(6, data=67464, actual=None),        # 主流在基础记录，占用未采集
        rec(7, base_record=6, data=42735, actual=45056),  # 附属流在扩展记录
    ]
    rows = []
    build_entry_rows(records, on_batch=lambda b: rows.extend(b))
    f = next(r for r in rows if r.name == "a.bin")
    assert f.size == 67464
    assert f.actual_size == 45056


def test_sparse_main_stream_uses_runlist_not_nominal():
    """稀疏文件的占用要数分配清单里非空洞的簇，不能拿属性头那个名义值。

    属性头那个数对一个稀疏文件来说是整个文件范围，真实落盘的簇
    往往只有它的一半，拿它当占用会系统性高估。
    """
    from core.mft.parse import ACTUAL_SRC_SPARSE, parse_record

    # 两条：100 簇真占落盘，50 簇是空洞（偏移字段为空）
    runs = bytes([0x11, 100, 0x01]) + bytes([0x01, 50])
    rec = _record_with_streams(
        number=7,
        streams=[dict(non_resident=True, alloc=1048576, real=1048576,
                      sparse=True, runs=runs)],
    )
    p = parse_record(rec, 7, 4096)
    assert p.data_size == 1048576
    assert p.actual_size == 100 * 4096
    assert p.actual_src == ACTUAL_SRC_SPARSE


def test_merge_does_not_clear_known_actual():
    """赢家自己没采到占用时，不能把另一条记录已经算对的占用清掉。

    透明压缩的文件，基础记录里那条附属流算出的才是真实占用；
    扩展记录里往往是更长的稀疏主流，它赢下大小却给不出占用，
    旧规则会把对的占用换未知。
    """
    from core.mft.parse import FileNameAttr, ParsedRecord
    from core.mft.tree import build_entry_rows

    def rec(number, *, base_record=0, data=0, actual=None, name="a.bin"):
        return ParsedRecord(
            number=number, in_use=True, is_directory=False,
            base_record=base_record, mtime=0.0,
            file_names=[FileNameAttr(
                parent_ref=5, allocated_size=0, real_size=data,
                flags=0, name=name, namespace=1)],
            data_size=data, has_reparse=False, reparse_tag=0,
            link_target="", actual_size=actual,
        )

    root = ParsedRecord(
        number=5, in_use=True, is_directory=True, base_record=0, mtime=0.0,
        file_names=[], data_size=0, has_reparse=False, reparse_tag=0,
        link_target="", actual_size=None,
    )
    rows = []
    build_entry_rows(
        [root,
         rec(6, data=67464, actual=45056),      # 已算出真实占用
         rec(7, base_record=6, data=80000, actual=None)],  # 更长的稀疏主流
        on_batch=lambda b: rows.extend(b),
    )
    f = next(r for r in rows if r.name == "a.bin")
    assert f.size == 80000      # 大小跟赢家走
    assert f.actual_size == 45056  # 占用保留已采到的那个


def test_compressed_actual_beats_bigger_sparse_main():
    """空壳主流更大，但旁路流那个来源更可信，占用要听旁路流的。

    这正是透明压缩文件的典型布局：基础记录里是被揭空的主数据流，
    扩展记录里是真正装数据的旁路流。按数据流大小选会选中空壳。
    """
    from core.mft import parallel
    from core.mft.parse import (
        ACTUAL_SRC_SPARSE, ACTUAL_SRC_WOF, FileNameAttr, ParsedRecord,
    )
    from core.mft.tree import build_entry_rows, build_entry_rows_from_compact

    def _build():
        parsed: list = [None] * 12
        parsed[5] = ParsedRecord(
            number=5, in_use=True, is_directory=True, base_record=0, mtime=10,
            file_names=[FileNameAttr(5, 0, 0, 0, ".", 1)], data_size=0,
        )
        # 空壳主流：大、来自稀疏，不可信
        parsed[6] = ParsedRecord(
            number=6, in_use=True, is_directory=False, base_record=0, mtime=11,
            file_names=[FileNameAttr(5, 0, 0, 0, "a.bin", 1)],
            data_size=67464, actual_size=131072, actual_src=ACTUAL_SRC_SPARSE,
        )
        # 旁路流：小、但是真数据
        parsed[7] = ParsedRecord(
            number=7, in_use=True, is_directory=False, base_record=6, mtime=0,
            file_names=[], data_size=42735, actual_size=45056,
            actual_src=ACTUAL_SRC_WOF,
        )
        return parsed

    parsed = _build()
    meta, names, link_targets, tags = parallel._pack_chunk(parsed)
    table = parallel.CompactMftTable(
        n_records=len(parsed), meta=meta, names=names, link_targets=link_targets,
        reparse_tags=tags,
    )
    rows_c, *_ = build_entry_rows_from_compact(table)
    rows_p, *_ = build_entry_rows(_build())

    for rows in (rows_c, rows_p):
        node = next(r for r in rows if r.name == "a.bin")
        assert node.size == 67464
        assert node.actual_size == 45056


def test_sparse_segments_across_records_are_summed():
    """稀疏文件拆在两条记录里时，两段的真实分配要相加，不能只取其中一段。

    每段自己算的是自己那段非空洞的簇，合起来才是整个文件的占用。
    """
    from core.mft import parallel
    from core.mft.parse import (
        ACTUAL_SRC_SPARSE, FileNameAttr, ParsedRecord,
    )
    from core.mft.tree import build_entry_rows, build_entry_rows_from_compact

    def _build():
        parsed: list = [None] * 12
        parsed[5] = ParsedRecord(
            number=5, in_use=True, is_directory=True, base_record=0, mtime=10,
            file_names=[FileNameAttr(5, 0, 0, 0, ".", 1)], data_size=0,
        )
        # 首段在基础记录：100 簇
        parsed[6] = ParsedRecord(
            number=6, in_use=True, is_directory=False, base_record=0, mtime=11,
            file_names=[FileNameAttr(5, 0, 0, 0, "a.bin", 1)],
            data_size=409600, actual_size=100 * 4096,
            actual_src=ACTUAL_SRC_SPARSE,
        )
        # 续段在扩展记录：另外 40 簇
        parsed[7] = ParsedRecord(
            number=7, in_use=True, is_directory=False, base_record=6, mtime=0,
            file_names=[], data_size=163840, actual_size=40 * 4096,
            actual_src=ACTUAL_SRC_SPARSE,
        )
        return parsed

    parsed = _build()
    meta, names, link_targets, tags = parallel._pack_chunk(parsed)
    table = parallel.CompactMftTable(
        n_records=len(parsed), meta=meta, names=names, link_targets=link_targets,
        reparse_tags=tags,
    )
    rows_c, *_ = build_entry_rows_from_compact(table)
    rows_p, *_ = build_entry_rows(_build())

    for rows in (rows_c, rows_p):
        node = next(r for r in rows if r.name == "a.bin")
        assert node.actual_size == 140 * 4096


def test_prefer_overlapped_read_off_by_default(monkeypatch):
    """异步读默认关闭，只有显式置位且在固态上才启用。"""
    from core.mft import volume

    monkeypatch.delenv("WSMC_OVERLAPPED", raising=False)
    monkeypatch.delenv("WSMC_NO_OVERLAPPED", raising=False)
    assert volume.prefer_overlapped_read("C") is False
    assert volume.prefer_overlapped_read(None) is False

    monkeypatch.setenv("WSMC_OVERLAPPED", "1")
    monkeypatch.setenv("WSMC_NO_OVERLAPPED", "1")
    assert volume.prefer_overlapped_read("C") is False


def test_fill_mft_buffer_default_path_uses_sync(monkeypatch):
    """默认不启用异步时，整表读走同步分支且不依赖未定义的名字。"""
    from core.mft import volume

    monkeypatch.delenv("WSMC_OVERLAPPED", raising=False)
    boot = volume.BootInfo(
        bytes_per_sector=512,
        sectors_per_cluster=8,
        cluster_size=4096,
        mft_lcn=4,
        record_size=1024,
    )
    calls = []
    monkeypatch.setattr(
        volume, "read_at_into", lambda h, off, dest, doff, size: calls.append(size)
    )
    dest = bytearray(4096)
    n = volume.fill_mft_buffer(
        None,
        boot,
        dest,
        runs=[(4, 4096)],
        usable=4096,
        drive_letter=None,
        apply_usa_now=False,
    )
    assert n == 1024
    assert calls == [4096]


def test_prefer_overlapped_read_positive_when_enabled(monkeypatch):
    """置位开关且盘是固态时才返回真，机械盘与判不出盘型都返回假。"""
    from core.mft import volume

    monkeypatch.setenv("WSMC_OVERLAPPED", "1")
    monkeypatch.delenv("WSMC_NO_OVERLAPPED", raising=False)
    monkeypatch.setattr(
        "core.store.is_rotational_drive", lambda drive: False, raising=False
    )
    assert volume.prefer_overlapped_read("C") is True
    monkeypatch.setattr(
        "core.store.is_rotational_drive", lambda drive: True, raising=False
    )
    assert volume.prefer_overlapped_read("C") is False


def test_fill_mft_buffer_picks_async_when_gate_on(monkeypatch):
    """门控打开时整表读必须走异步提交，而不是继续用同步读。"""
    from core.mft import volume

    boot = volume.BootInfo(
        bytes_per_sector=512,
        sectors_per_cluster=8,
        cluster_size=4096,
        mft_lcn=4,
        record_size=1024,
    )
    sync_calls = []
    async_calls = []
    monkeypatch.setattr(volume, "prefer_overlapped_read", lambda drive: True)
    monkeypatch.setattr(
        volume, "open_volume", lambda drive, overlapped=False: 1234
    )
    monkeypatch.setattr(volume, "close_handle", lambda handle: None)
    monkeypatch.setattr(
        volume, "read_at_into", lambda h, off, dest, doff, size: sync_calls.append(size)
    )
    monkeypatch.setattr(
        volume,
        "read_segments_overlapped",
        lambda h, segs, dest, base=0, **kw: async_calls.append(list(segs)),
    )
    # 异步分支只在切成多段时启用，取 40MB 让 16MB 一段切成三段
    total = 40 * 1024 * 1024
    dest = bytearray(total)
    volume.fill_mft_buffer(
        None,
        boot,
        dest,
        runs=[(4, total)],
        usable=total,
        drive_letter="C",
        apply_usa_now=False,
    )
    assert async_calls, "门控打开却没有走异步读"
    assert not sync_calls, "门控打开时不应再用同步读"


def test_complete_head_pops_on_failure(monkeypatch):
    """队首等待失败后必须出队，否则收尾会对已关闭的事件句柄再等一次。"""
    from core.mft import volume

    monkeypatch.setattr(
        volume,
        "_wait_overlapped",
        lambda h, ov, buf, size: (_ for _ in ()).throw(
            volume.MftIoError("overlapped read failed, winerr=87")
        ),
    )
    pending = [(object(), b"", 8, 0, 0), (object(), b"", 8, 8, 8)]
    dest = bytearray(16)
    try:
        volume._complete_head(1, pending, dest, 0, None)
    except volume.MftIoError:
        pass
    else:
        raise AssertionError("失败时应当抛出")
    assert len(pending) == 1, "失败的队首没有出队"
