"""把解析后的 MFT 记录建成邻接表行（根 id=1）。

热路径：
- 优先吃 :class:`~core.mft.parallel.CompactMftTable`（紧凑 meta + names），
  不还原 ``ParsedRecord``；
- 栈 DFS 分配 sid 后，用 ``parent_sid[]`` / ``acc[]`` **反向一遍**上卷目录大小，
  无需 kids 列表与后序栈（父 sid < 子 sid 由先分配再展开保证）；
- ``on_batch`` 流式写出，避免与 writer 双持整表。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from ..models import Entry
from ..snapshot import EntryRow
from .parallel import (
    CompactMftTable,
    F_IN_USE,
    F_IS_DIR,
    F_NONE,
    F_REPARSE,
    META,
    META_SIZE,
)
from .parse import FileNameAttr, ParsedRecord

# NTFS：5 = 卷根目录
MFT_ROOT = 5

Progress = Callable[[int, str], None]
Cancel = Callable[[], bool]
OnBatch = Callable[[list[EntryRow]], None]

# 流式写库时每批行数（与 SnapshotWriter 批大小同量级）
_ON_BATCH_SIZE = 10_000


class MftTreeError(Exception):
    """无法从 MFT 建成树。"""


def _merge_extensions(
    parsed: list[ParsedRecord | None],
) -> dict[int, ParsedRecord]:
    by_num: dict[int, ParsedRecord] = {}
    extensions: list[ParsedRecord] = []

    for rec in parsed:
        if rec is None or not rec.in_use:
            continue
        if rec.base_record != 0 and rec.base_record != rec.number:
            extensions.append(rec)
            continue
        by_num.setdefault(rec.number, rec)

    for ext in extensions:
        base = by_num.get(ext.base_record)
        if base is None:
            continue
        if ext.file_names:
            base.file_names.extend(ext.file_names)
        if ext.data_size > base.data_size:
            base.data_size = ext.data_size
        if ext.mtime and not base.mtime:
            base.mtime = ext.mtime
        if ext.has_reparse:
            base.has_reparse = True
        if ext.is_directory:
            base.is_directory = True
    return by_num


def _name_edges(
    by_num: dict[int, ParsedRecord],
) -> dict[int, list[tuple[int, FileNameAttr]]]:
    """parent_mft → [(child_mft, file_name_attr), ...]。"""
    children: dict[int, list[tuple[int, FileNameAttr]]] = defaultdict(list)
    for num, rec in by_num.items():
        if num == MFT_ROOT:
            continue
        if not rec.file_names:
            continue
        # 跳过纯 DOS 短名（若存在 Win32 名）
        has_win = any(n.namespace in (0, 1, 3) for n in rec.file_names)
        seen: set[tuple[int, str]] = set()
        for fn in rec.file_names:
            if has_win and fn.namespace == 2:
                continue
            key = (fn.parent_ref, fn.name)
            if key in seen:
                continue
            seen.add(key)
            children[fn.parent_ref].append((num, fn))
    return children


def _build_from_base(
    *,
    root_mtime: int,
    is_dir: dict[int, bool] | list[bool] | bytearray,
    data_size: dict[int, int] | list[int],
    mtime: dict[int, int] | list[int],
    children: dict[int, list[tuple[int, str, int]]],
    follow_reparse: bool = False,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    on_batch: OnBatch | None = None,
    timer: object | None = None,
    live: list[bool] | bytearray | None = None,
) -> tuple[list[EntryRow], int, int, int]:
    """共用建树：栈 DFS 分配 sid + 反向上卷 + 产出行。

    ``children[parent_mft] = [(child_mft, name, fn_real_size), ...]``

    ``is_dir`` / ``data_size`` / ``mtime`` 可为 dict（ParsedRecord 路径）
    或按 mft 号下标的平铺数组（紧凑路径）；数组路径须同时传 ``live``。
    """
    _ = follow_reparse  # 预留
    _span_start = getattr(timer, "span_start", None)
    _span_end = getattr(timer, "span_end", None)

    use_arr = live is not None
    if use_arr:
        assert isinstance(is_dir, (list, bytearray))
        assert isinstance(data_size, list)
        assert isinstance(mtime, list)

        def _alive(m: int) -> bool:
            return 0 <= m < len(live) and bool(live[m])  # type: ignore[arg-type]

        def _is_dir(m: int) -> bool:
            return bool(is_dir[m])

        def _dsize(m: int) -> int:
            return data_size[m]

        def _mtime(m: int) -> int:
            return mtime[m]
    else:
        assert isinstance(is_dir, dict)
        assert isinstance(data_size, dict)
        assert isinstance(mtime, dict)

        def _alive(m: int) -> bool:
            return m in is_dir

        def _is_dir(m: int) -> bool:
            return bool(is_dir[m])

        def _dsize(m: int) -> int:
            return data_size.get(m, 0)  # type: ignore[union-attr]

        def _mtime(m: int) -> int:
            return mtime.get(m, 0)  # type: ignore[union-attr]

    if _span_start:
        _span_start("mft_tree_bfs")

    # 平铺数组：下标 = sid（1..n）；0 不用
    # 动态扩容用 list。q 用栈（pop 末尾）= DFS；父 sid < 子 sid 由
    # 「先分配 sid 再展开」保证，与遍历顺序无关。
    parent_sid_arr: list[int | None] = [None, None]  # [0]=pad, [1]=root parent
    name_arr: list[str] = ["", ""]
    is_dir_arr: list[bool] = [False, True]
    mtime_arr: list[int] = [0, root_mtime]
    file_size_arr: list[int] = [0, 0]

    next_id = 2
    primary_sid: dict[int, int] = {MFT_ROOT: 1}
    expanded: set[int] = set()
    q: list[int] = [MFT_ROOT]

    while q:
        if cancel and cancel():
            raise MftTreeError("cancelled")
        mft = q.pop()
        if mft in expanded:
            continue
        expanded.add(mft)
        p_sid = primary_sid[mft]

        for child_mft, cname, fn_real in children.get(mft, []):
            if not _alive(child_mft):
                continue
            c_is_dir = _is_dir(child_mft)
            if c_is_dir:
                fsize = 0
            else:
                fsize = _dsize(child_mft) or int(fn_real or 0)

            sid = next_id
            next_id += 1
            parent_sid_arr.append(p_sid)
            name_arr.append(cname)
            is_dir_arr.append(c_is_dir)
            mtime_arr.append(_mtime(child_mft))
            file_size_arr.append(fsize)

            if c_is_dir and child_mft not in primary_sid:
                primary_sid[child_mft] = sid
                q.append(child_mft)

    if _span_end:
        _span_end("mft_tree_bfs")

    # 反向一遍上卷：sid 递增保证子 > 父
    if _span_start:
        _span_start("mft_tree_agg")
    acc = list(file_size_arr)  # 文件=自身大小；目录先 0 再累加
    for sid in range(next_id - 1, 0, -1):
        if is_dir_arr[sid]:
            # 目录自身 file_size 为 0；acc 仅来自子
            pass
        ps = parent_sid_arr[sid]
        if ps is not None:
            acc[ps] += acc[sid]
    total_size = acc[1] if next_id > 1 else 0
    if _span_end:
        _span_end("mft_tree_agg")

    if _span_start:
        _span_start("mft_tree_emit")
    file_count = 0
    dir_count = 0
    keep_all = on_batch is None
    rows: list[EntryRow] = []
    batch: list[EntryRow] = []
    batch_n = _ON_BATCH_SIZE

    for sid in range(1, next_id):
        if cancel and cancel():
            raise MftTreeError("cancelled")
        if is_dir_arr[sid]:
            dir_count += 1
            row = EntryRow.directory(
                sid, parent_sid_arr[sid], name_arr[sid], acc[sid], mtime_arr[sid]
            )
        else:
            file_count += 1
            row = EntryRow.file(
                sid,
                parent_sid_arr[sid],
                name_arr[sid],
                file_size_arr[sid],
                mtime_arr[sid],
            )
        if keep_all:
            rows.append(row)
        else:
            batch.append(row)
            if len(batch) >= batch_n:
                on_batch(batch)
                batch = []

    if not keep_all and batch:
        on_batch(batch)

    if progress is not None:
        progress(file_count, "")

    if _span_end:
        _span_end("mft_tree_emit")

    return rows, file_count, dir_count, total_size


def build_entry_rows_from_compact(
    table: CompactMftTable,
    *,
    follow_reparse: bool = False,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    on_batch: OnBatch | None = None,
    timer: object | None = None,
) -> tuple[list[EntryRow], int, int, int]:
    """从紧凑表建树（热路径：无 ParsedRecord）。

    merge 优化：
    - 记录号致密 ``0..n``，``live/is_dir/data_size/mtime`` 用平铺数组；
    - ``meta[i*24]`` 先读 flags，free/None 直接跳过，省一半 unpack；
    - name 行按记录序（worker 输出），一遍直建 children，无 ``names_by``。
    """
    _span_start = getattr(timer, "span_start", None)
    _span_end = getattr(timer, "span_end", None)

    n = table.n_records
    meta = table.meta
    if n <= 0 or len(meta) < n * META_SIZE:
        raise MftTreeError("empty compact MFT table")

    if _span_start:
        _span_start("mft_tree_merge")

    # 平铺：下标 = mft 号；live[i]=该号是有效 base（非 free/extension）
    live = bytearray(n)  # 0/1
    is_dir_arr = bytearray(n)  # 0/1
    data_size_arr = [0] * n
    mtime_arr = [0] * n
    # extension：ext → base；多数卷 extension 很少，dict 足够
    base_of: dict[int, int] = {}
    # extension 暂存 (flags, mt, dsz)，第二遍并到 base
    ext_attrs: list[tuple[int, int, int, int]] = []  # ext, flags, mt, dsz

    for i in range(n):
        # flags 在 meta 每条 24B 的第 0 字节
        flags = meta[i * META_SIZE]
        if flags & F_NONE:
            continue
        if not (flags & F_IN_USE):
            continue
        # 完整 unpack 只对 in-use 记录
        _f, _pad, _nnames, base, mt, dsz = META.unpack_from(meta, i * META_SIZE)
        if base != 0 and base != i:
            base_of[i] = base
            ext_attrs.append((i, flags, mt, dsz))
            continue
        live[i] = 1
        is_dir_arr[i] = 1 if (flags & F_IS_DIR) else 0
        data_size_arr[i] = dsz
        mtime_arr[i] = mt
        _ = flags & F_REPARSE  # 预留 follow_reparse

    for ext_num, flags, mt, dsz in ext_attrs:
        base_num = base_of[ext_num]
        if base_num < 0 or base_num >= n or not live[base_num]:
            continue
        if flags & F_IS_DIR:
            is_dir_arr[base_num] = 1
        if dsz > data_size_arr[base_num]:
            data_size_arr[base_num] = dsz
        if mt and not mtime_arr[base_num]:
            mtime_arr[base_num] = mt

    if MFT_ROOT >= n or not live[MFT_ROOT]:
        raise MftTreeError("MFT root record #5 missing")

    # names 按记录序连续；同记录局部 has_win/seen，一遍直建 children
    children: dict[int, list[tuple[int, str, int]]] = defaultdict(list)
    names = table.names
    nn = len(names)
    i = 0
    while i < nn:
        idx0 = names[i][0]
        j = i + 1
        while j < nn and names[j][0] == idx0:
            j += 1
        # [i, j) 同一记录的全部 name 行
        if 0 <= idx0 < n:
            target = base_of.get(idx0, idx0)
            if target != MFT_ROOT and 0 <= target < n and live[target]:
                has_win = False
                for k in range(i, j):
                    if names[k][6] in (0, 1, 3):
                        has_win = True
                        break
                seen: set[tuple[int, str]] = set()
                for k in range(i, j):
                    _idx, parent, _alloc, real, _ff, name, ns = names[k]
                    if has_win and ns == 2:
                        continue
                    key = (parent, name)
                    if key in seen:
                        continue
                    seen.add(key)
                    children[parent].append((target, name, real))
        i = j

    if _span_end:
        _span_end("mft_tree_merge")

    return _build_from_base(
        root_mtime=mtime_arr[MFT_ROOT],
        is_dir=is_dir_arr,
        data_size=data_size_arr,
        mtime=mtime_arr,
        children=children,
        follow_reparse=follow_reparse,
        progress=progress,
        cancel=cancel,
        on_batch=on_batch,
        timer=timer,
        live=live,
    )


def build_entry_rows(
    parsed: list[ParsedRecord | None],
    *,
    follow_reparse: bool = False,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    on_batch: OnBatch | None = None,
    timer: object | None = None,
) -> tuple[list[EntryRow], int, int, int]:
    """从 ParsedRecord 列表建树（兼容 / 测试路径）。"""
    _span_start = getattr(timer, "span_start", None)
    _span_end = getattr(timer, "span_end", None)

    if _span_start:
        _span_start("mft_tree_merge")
    by_num = _merge_extensions(parsed)
    if MFT_ROOT not in by_num:
        raise MftTreeError("MFT root record #5 missing")
    edges = _name_edges(by_num)
    if _span_end:
        _span_end("mft_tree_merge")

    is_dir = {num: rec.is_directory for num, rec in by_num.items()}
    data_size = {num: rec.data_size for num, rec in by_num.items()}
    mtime = {num: rec.mtime for num, rec in by_num.items()}
    children: dict[int, list[tuple[int, str, int]]] = defaultdict(list)
    for parent, lst in edges.items():
        for child_mft, fn in lst:
            children[parent].append((child_mft, fn.name, int(fn.real_size or 0)))

    return _build_from_base(
        root_mtime=by_num[MFT_ROOT].mtime,
        is_dir=is_dir,
        data_size=data_size,
        mtime=mtime,
        children=children,
        follow_reparse=follow_reparse,
        progress=progress,
        cancel=cancel,
        on_batch=on_batch,
        timer=timer,
    )


def build_entries(
    parsed: list[ParsedRecord | None],
    *,
    follow_reparse: bool = False,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
) -> tuple[list[Entry], int, int, int]:
    """兼容旧接口：返回 :class:`Entry` 列表（内部走 :func:`build_entry_rows`）。"""
    rows, file_count, dir_count, total_size = build_entry_rows(
        parsed,
        follow_reparse=follow_reparse,
        progress=progress,
        cancel=cancel,
    )
    entries = [
        Entry(
            id=r.id,
            parent_id=r.parent_id,
            name=r.name,
            size=r.size,
            is_dir=bool(r.is_dir),
            mtime=r.mtime,
        )
        for r in rows
    ]
    return entries, file_count, dir_count, total_size
