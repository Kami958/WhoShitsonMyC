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
    meta, names, link_targets = parallel._pack_chunk(direct)
    back = parallel._unpack_chunk(0, meta, names, link_targets)
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
        start, meta, names, link_targets = parallel._worker_parse_range(
            (shm.name, len(blob), 0, n, rec_size, 0)
        )
    finally:
        shm.close()
        shm.unlink()

    assert start == 0
    assert len(meta) == n * parallel.META_SIZE
    back = parallel._unpack_chunk(0, meta, names, link_targets)
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
        _start, meta, names, link_targets = parallel._worker_parse_range(
            (shm.name, len(blob), 0, len(recs), rec_size, 0)
        )
    finally:
        shm.close()
        shm.unlink()

    back = parallel._unpack_chunk(0, meta, names, link_targets)
    assert back[1] is not None
    assert back[1].reparse_tag == tag
    assert back[1].has_reparse is True


def test_choose_mft_procs_formula(monkeypatch):
    from core.mft import parallel

    monkeypatch.delenv("WSMC_MFT_WORKERS", raising=False)
    monkeypatch.delenv("WSMC_MFT_PROCS", raising=False)
    monkeypatch.setattr(parallel.os, "cpu_count", lambda: 12)
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
    rows, fc, dc, total = build_entry_rows(
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
    meta, names, link_targets = parallel._pack_chunk(parsed)
    # pack 用相对下标；整表 start=0 即全局
    table = parallel.CompactMftTable(
        n_records=len(parsed), meta=meta, names=names, link_targets=link_targets
    )
    rows_a, fc_a, dc_a, tot_a = build_entry_rows(parsed)
    rows_b, fc_b, dc_b, tot_b = build_entry_rows_from_compact(table)
    assert (fc_a, dc_a, tot_a) == (fc_b, dc_b, tot_b) == (2, 2, 120)
    assert len(rows_a) == len(rows_b) == 4
    # 根 size 上卷
    assert rows_a[0].size == rows_b[0].size == 120


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
    meta, names, link_targets = parallel._pack_chunk(parsed)
    table = parallel.CompactMftTable(
        n_records=len(parsed), meta=meta, names=names, link_targets=link_targets
    )
    rows, _fc, _dc, _tot = build_entry_rows_from_compact(table)
    link = next(r for r in rows if r.name == "link")
    assert link.reparse_tag == 0xA0000003
    assert link.link_target == r"D:\360download\目标"


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
