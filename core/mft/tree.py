"""把解析后的 MFT 记录建成邻接表行（根 id=1）。

热路径：
- 优先吃 :class:`~core.mft.parallel.CompactMftTable`（紧凑 meta + names），
  不还原 ``ParsedRecord``；
- 栈 DFS 分配 sid 后，用 ``parent_sid[]`` / ``acc[]`` **反向一遍**上卷目录大小，
  无需 kids 列表与后序栈（父 sid < 子 sid 由先分配再展开保证）；
- ``on_batch`` 流式写出，避免与 writer 双持整表。
"""

from __future__ import annotations

from array import array
from collections import defaultdict
from collections.abc import Callable, Iterable

from ..models import Entry
from ..snapshot import EntryRow
from .. import applog
from .parallel import (
    CompactMftTable,
    F_IN_USE,
    F_IS_DIR,
    F_NONE,
    F_REPARSE,
    META,
    META_SIZE,
)
from .pack import FoldNames
from .parse import ACTUAL_SRC_RANK, ACTUAL_SRC_SPARSE, FileNameAttr, ParsedRecord


def _src_rank(src: int) -> int:
    """这笔占用来自哪种读法，可信度多少。数值大的更可信。

    WOF 旁路流排在最高：一个文件同时有空壳主数据流和旁路流时，只有旁路流
    是真占用。普通主数据流与稀疏主数据流同分，它们不会同时出现在一处。
    """
    return ACTUAL_SRC_RANK.get(int(src), 0)

# NTFS：5 = 卷根目录
MFT_ROOT = 5

Progress = Callable[[int, str], None]
Cancel = Callable[[], bool]
OnBatch = Callable[[list[EntryRow]], None]
# 链接回调：on_link(相对扫描根的路径, 重解析点标签, 原始目标)
OnLink = Callable[[str, int, str], None]

# 流式写库时每批行数（与 SnapshotWriter 批大小同量级）
_ON_BATCH_SIZE = 10_000

# 8 字节有符号数组的取值上限。损坏的记录可能报出接近 2^64 的大小，
# 多条累加更是必溢，到了这里就夹住。
_SIZE_MAX = (1 << 63) - 1

# 单条记录能有多大：NTFS 卷上限在 256TB 量级，单文件上限 16TB，
# 4 EiB 是它们的上万倍。超过就只能是损坏值，按未采集处理。
_SIZE_LIMIT = 1 << 62

_size_warned = False


def _clamp_size(v: object) -> int:
    """单条记录的大小：负数与天文数字一律当 0。

    损坏记录可能报出接近 2^64 的大小，直接夹到上限写进快照，会变成一个
    看着合法的「8 EiB」，界面上比「未采集」更误导。这里宁可记成 0 并留
    一条日志，也不把损坏值当真实数字。
    """
    global _size_warned
    try:
        n = int(v or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    if n > _SIZE_LIMIT:
        if not _size_warned:
            _size_warned = True
            applog.warn(
                "MFT：记录报出的文件大小超过卷容量上限，已按未采集处理"
            )
        return 0
    return n


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
        # 大小跟数据流大的那条走。占用不看大小，看这笔数是从哪读来的：
        # 压缩文件的空壳主流比真实数据大，按大小选会选中空壳，
        # 而旁路流那个来源更可信。同可信度时才按「谁有值听谁」。
        if ext.data_size > base.data_size:
            base.data_size = ext.data_size
        if ext.actual_size is not None:
            if base.actual_size is None:
                base.actual_size = ext.actual_size
                base.actual_src = ext.actual_src
            elif (ext.actual_src == ACTUAL_SRC_SPARSE
                  and base.actual_src == ACTUAL_SRC_SPARSE):
                # 稀疏文件的分配散在各段，每段自己算一部分，相加才对
                base.actual_size += ext.actual_size
            elif _src_rank(ext.actual_src) > _src_rank(base.actual_src):
                base.actual_size = ext.actual_size
                base.actual_src = ext.actual_src
        if ext.mtime and not base.mtime:
            base.mtime = ext.mtime
        if ext.has_reparse:
            base.has_reparse = True
        if ext.reparse_tag and not base.reparse_tag:
            base.reparse_tag = ext.reparse_tag
        if ext.link_target and not base.link_target:
            base.link_target = ext.link_target
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
    children: dict[int, list[tuple[int, str, int]]] | None = None,
    reparse_tag: dict[int, int] | list[int] | None = None,
    link_target: dict[int, str] | list[str] | None = None,
    actual_size: dict[int, int | None] | list[int] | None = None,
    follow_reparse: bool = False,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    on_batch: OnBatch | None = None,
    timer: object | None = None,
    live: list[bool] | bytearray | None = None,
    on_link: OnLink | None = None,
    child_of: "Callable[[int], Iterable[tuple[int, int | str, int]]] | None" = None,
    fold: "FoldNames | None" = None,
) -> tuple[list[EntryRow], int, int, int, int, int]:
    """共用建树：栈 DFS 分配 sid + 反向上卷 + 产出行。

    ``children[parent_mft] = [(child_mft, name, fn_real_size), ...]``

    也可以用 ``child_of(parent_mft)`` 现取子项（逐段折叠后的链表路径），
    两者传一个就行，传 ``child_of`` 时不建子项字典。

    ``is_dir`` / ``data_size`` / ``mtime`` / ``reparse_tag`` / ``link_target`` /
    ``actual_size`` 可为 dict（ParsedRecord 路径）或按 mft 号下标的平铺数组
    （紧凑路径）；数组路径须同时传 ``live``。

    ``actual_size`` 的平铺数组用 -1 表示未记录（紧凑路径），dict 用 ``None``。
    目录行的实际占用由子项累加，全部子项都未记录时保持未记录，不记成 0。

    返回 ``(rows, file_count, dir_count, total_size, total_actual_size,
    unrecorded_files)``。

    ``on_link`` 为每个带重解析点标签的行调一次，路径由记录号爬父链现算。
    """
    _ = follow_reparse  # 预留
    _span_start = getattr(timer, "span_start", None)
    _span_end = getattr(timer, "span_end", None)

    use_arr = live is not None
    if use_arr:
        assert isinstance(is_dir, (list, bytearray))
        assert isinstance(data_size, (list, array))
        assert isinstance(mtime, (list, array))
        assert isinstance(reparse_tag, (list, array))
        assert isinstance(link_target, list)

        def _alive(m: int) -> bool:
            return 0 <= m < len(live) and bool(live[m])  # type: ignore[arg-type]

        def _is_dir(m: int) -> bool:
            return bool(is_dir[m])

        def _dsize(m: int) -> int:
            return int(data_size[m] or 0)

        def _mtime(m: int) -> int:
            return int(mtime[m] or 0)

        def _rtag(m: int) -> int:
            return int(reparse_tag[m] or 0)  # type: ignore[index]

        def _ltarget(m: int) -> str:
            return link_target[m]  # type: ignore[index]

        def _asize(m: int) -> int | None:
            if actual_size is None:
                return None
            v = actual_size[m]  # type: ignore[index]
            return None if v < 0 else int(v)
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

        def _rtag(m: int) -> int:
            return int((reparse_tag or {}).get(m, 0))  # type: ignore[union-attr,attr-defined]

        def _ltarget(m: int) -> str:
            return str((link_target or {}).get(m, ""))  # type: ignore[union-attr,attr-defined]

        def _asize(m: int) -> int | None:
            if actual_size is None:
                return None
            v = actual_size.get(m)  # type: ignore[union-attr]
            return None if v is None else int(v)

    if _span_start:
        _span_start("mft_tree_bfs")

    # 平铺数组：下标 = sid（1..n）；0 不用
    # 动态扩容用 list。q 用栈（pop 末尾）= DFS；父 sid < 子 sid 由
    # 「先分配 sid 再展开」保证，与遍历顺序无关。
    parent_sid_arr = array("i", [0, 0])  # [0]=pad, [1]=root parent（0 表示没有父）
    name_arr: list[str] = ["", ""]
    # 逐段折叠路径下不存名字本身，只存它在文本池里的编号，输出时现切
    name_k = array("i", [-1, -1]) if fold is not None else None

    def name_of(sid: int) -> str:
        if name_k is not None:
            k = name_k[sid] if 0 <= sid < len(name_k) else -1
            return fold.name_at(k) if k >= 0 else ""
        return name_arr[sid] if sid < len(name_arr) else ""

    is_dir_arr_sid = bytearray([0, 1])
    mtime_arr_sid = array("q", [0, int(root_mtime)])
    file_size_arr = array("q", [0, 0])
    rtag_sid_arr = array("I", [0, 0])  # 按 sid 索引（BFS 时从 mft 号查值转过来）
    ltarget_sid_arr: list[str] = ["", ""]  # 同 rtag：链接目标按 sid 索引
    # 实际占用：-1 = 未记录。用紧凑数组，百万级条目下每条只占 8 字节。
    # 根初值也是未记录：子项全都未记录时，根不能记成 0。
    actual_sid_arr = array("q", [0, -1])

    def parent_of(sid: int) -> int | None:
        # 定长数组没有 None，用 0 当「无父」，写库时要还原成空
        p = parent_sid_arr[sid]
        return p if p > 0 else None

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

        for child_mft, cname, fn_real in (
            child_of(mft) if child_of is not None else children.get(mft, [])
        ):
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
            if name_k is not None:
                name_k.append(int(cname))
            else:
                name_arr.append(cname)
            is_dir_arr_sid.append(1 if c_is_dir else 0)
            mtime_arr_sid.append(int(_mtime(child_mft) or 0))
            file_size_arr.append(_clamp_size(fsize))
            rtag_sid_arr.append(int(_rtag(child_mft) or 0))
            ltarget_sid_arr.append(_ltarget(child_mft))
            a = _asize(child_mft)
            actual_sid_arr.append(-1 if a is None else _clamp_size(a))

            if c_is_dir and child_mft not in primary_sid:
                primary_sid[child_mft] = sid
                q.append(child_mft)

    if _span_end:
        _span_end("mft_tree_bfs")

    # 反向一遍上卷：sid 递增保证子 > 父
    if _span_start:
        _span_start("mft_tree_agg")
    acc = array("q", file_size_arr)  # 文件=自身大小；目录先 0 再累加
    for sid in range(next_id - 1, 0, -1):
        if is_dir_arr_sid[sid]:
            # 目录自身 file_size 为 0；acc 仅来自子
            pass
        ps = parent_sid_arr[sid]
        if ps:
            # 损坏记录可能报出接近上限的大小，多条累加起来会撞穿 8 字节有符号数组；
            # 到了上限就夹住，至少不把整次 MFT 扫描维回目录遍历
            total = acc[ps] + acc[sid]
            acc[ps] = total if total < _SIZE_MAX else _SIZE_MAX
    total_size = acc[1] if next_id > 1 else 0
    if _span_end:
        _span_end("mft_tree_agg")

    # 实际占用单独一遍：未记录的条目不能当 0 参与累加，否则合计偏小且难辨认。
    # 目录初值也是未记录，第一个有值的子项进来才变成数字。
    if _span_start:
        _span_start("mft_tree_agg_actual")
    acc_actual = array("q", actual_sid_arr)
    for sid in range(next_id - 1, 0, -1):
        ps = parent_sid_arr[sid]
        if ps is None:
            continue
        v = acc_actual[sid]
        if v < 0:
            continue
        if acc_actual[ps] < 0:
            acc_actual[ps] = v
        else:
            total = acc_actual[ps] + v
            acc_actual[ps] = total if total < _SIZE_MAX else _SIZE_MAX
    total_actual_size = acc_actual[1] if next_id > 1 and acc_actual[1] >= 0 else 0
    if _span_end:
        _span_end("mft_tree_agg_actual")

    if _span_start:
        _span_start("mft_tree_emit")
    file_count = 0
    dir_count = 0
    unrecorded_files = 0
    keep_all = on_batch is None
    rows: list[EntryRow] = []
    batch: list[EntryRow] = []
    batch_n = _ON_BATCH_SIZE

    for sid in range(1, next_id):
        if cancel and cancel():
            raise MftTreeError("cancelled")
        row_reparse = rtag_sid_arr[sid]
        if row_reparse and on_link is not None:
            on_link(
                _rel_path_of(sid, parent_sid_arr, name_of),
                int(row_reparse),
                ltarget_sid_arr[sid] or "",
            )
        if is_dir_arr_sid[sid]:
            dir_count += 1
            row = EntryRow.directory(
                sid,
                parent_of(sid),
                name_of(sid),
                acc[sid],
                mtime_arr_sid[sid],
                reparse_tag=row_reparse,
                link_target=ltarget_sid_arr[sid],
                actual_size=(
                    acc_actual[sid] if acc_actual[sid] >= 0 else None
                ),
            )
        else:
            file_count += 1
            if actual_sid_arr[sid] < 0:
                unrecorded_files += 1
            row = EntryRow.file(
                sid,
                parent_of(sid),
                name_of(sid),
                file_size_arr[sid],
                mtime_arr_sid[sid],
                reparse_tag=row_reparse,
                link_target=ltarget_sid_arr[sid],
                actual_size=(
                    actual_sid_arr[sid] if actual_sid_arr[sid] >= 0 else None
                ),
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

    return (
        rows,
        file_count,
        dir_count,
        total_size,
        total_actual_size,
        unrecorded_files,
    )


def _rel_path_of(num: int, parents: list[int | None], name_of: Callable[[int], str]) -> str:
    """从记录号爬到根，拼出相对扫描根的路径（``a\\b``；根目录为空串）。

    只为链接调一次，不预先为每个节点存路径：整盘几百万条记录里链接只有
    几百条，存路径反而多占一份跟节点数同量级的内存。
    名字由 ``name_of`` 现取，不要求调用方先摆成一张字符串表。
    """
    parts: list[str] = []
    cur = num
    while cur > 1:
        nm = name_of(cur)
        if nm:
            parts.append(nm)
        nxt = parents[cur] if cur < len(parents) else None
        if not nxt:
            break
        cur = nxt
    parts.reverse()
    return "\\".join(parts)


def build_entry_rows_from_compact(
    table: CompactMftTable,
    *,
    follow_reparse: bool = False,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    on_batch: OnBatch | None = None,
    timer: object | None = None,
    on_link: OnLink | None = None,
) -> tuple[list[EntryRow], int, int, int, int, int]:
    """从紧凑表建树（热路径：无 ParsedRecord）。

    merge 优化：
    - 记录号致密 ``0..n``，``live/is_dir/data_size/mtime`` 用平铺数组；
    - ``meta[i*META_SIZE]`` 先读 flags，free/None 直接跳过，省一半 unpack；
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
    # 百万级记录下用定长数组：Python 列表里每个整数是一个独立对象，
    # 一条占四十字节；这些列都放得下 64 位整数，八字节就够。
    data_size_arr = array("q", [0]) * n
    mtime_arr = array("q", [0]) * n
    reparse_tag_arr = array("I", [0]) * n
    link_target_arr = [""] * n
    # 实际占用：-1 = 未记录。紧凑数组，每条 8 字节。
    actual_size_arr = array("q", [-1]) * n
    # 每笔占用是从哪读来的，见 parse.ACTUAL_SRC_*。一条记录一个字节。
    # 合并扩展记录时靠它决定听谁的：压缩文件的空壳主数据流比真实数据大，
    # 按数据流大小猜会选中空壳，拿到的就是原体积而不是占用。
    actual_src_arr = bytearray(n)
    # extension：ext → base；多数卷 extension 很少，dict 足够
    base_of: dict[int, int] = {}
    # extension 暂存 (ext, flags, mt, dsz, actual, src)，第二遍并到 base；
    # 重解析标签不在元信息里，走旁路，所以这份不带它。
    # ext 记录的链接目标经 table.link_targets 用 base_of 转回 base
    ext_attrs: list[tuple[int, int, int, int, int, int]] = []

    for i in range(n):
        # flags 在 meta 每条记录的第 0 字节（每条宽度见 pack._META_SIZE）
        flags = meta[i * META_SIZE]
        if flags & F_NONE:
            continue
        if not (flags & F_IN_USE):
            continue
        # 完整 unpack 只对 in-use 记录（重解析标签走旁路，不在这里）
        _f, src, base, mt, dsz, act = META.unpack_from(meta, i * META_SIZE)
        if base != 0 and base != i:
            base_of[i] = base
            ext_attrs.append((i, flags, mt, dsz, act, src))
            continue
        live[i] = 1
        is_dir_arr[i] = 1 if (flags & F_IS_DIR) else 0
        # 损坏的记录可能报出超过 2^63 的大小，直接写 8 字节数组会 OverflowError
        data_size_arr[i] = _clamp_size(dsz)
        mtime_arr[i] = int(mt or 0)
        if act >= 0:
            actual_size_arr[i] = act
            actual_src_arr[i] = src
        _ = flags & F_REPARSE  # 紧凑路径只看标签值，不看这位（已并入 reparse_tag）

    for ext_num, flags, mt, dsz, act, src in ext_attrs:
        base_num = base_of[ext_num]
        if base_num < 0 or base_num >= n or not live[base_num]:
            continue
        if flags & F_IS_DIR:
            is_dir_arr[base_num] = 1
        # 大小跟数据流大的那条走。占用不看大小，看这笔数是从哪读来的：
        # 压缩文件的空壳主流比真实数据大，按大小选会选中空壳，
        # 而旁路流那个来源更可信。同可信度时才按“谁有值听谁”。
        if dsz > data_size_arr[base_num]:
            data_size_arr[base_num] = _clamp_size(dsz)
        if act >= 0:
            cur = actual_size_arr[base_num]
            cur_src = actual_src_arr[base_num]
            if cur < 0:
                actual_size_arr[base_num] = act
                actual_src_arr[base_num] = src
            elif src == ACTUAL_SRC_SPARSE and cur_src == ACTUAL_SRC_SPARSE:
                # 稀疏文件的分配散在各段，每段自己算一部分，相加才对
                total = cur + act
                actual_size_arr[base_num] = total if total < _SIZE_MAX else _SIZE_MAX
            elif _src_rank(src) > _src_rank(cur_src):
                actual_size_arr[base_num] = act
                actual_src_arr[base_num] = src
        if mt and not mtime_arr[base_num]:
            mtime_arr[base_num] = int(mt)

    # 重解析标签只存在于少数记录，从旁路列表回填到按记录号的数组。
    # 分两遍：先接受记录自己带的标签，再让扩展记录的标签补空位。
    # 同一条记录挂多条扩展时取记录号最小的那条，与旧路径按记录号升序合并一致，
    # 否则清单到达顺序一变，同一个盘两次扫出的标签就会不同。
    # 标签通常有八万条左右，用按记录号下标的定长数组记来源最省；
    # 完全没有标签时不必占用这片内存。
    tag_from = array("i", [-1]) * n if table.reparse_tags else None
    if tag_from is not None:
        for own_pass in (True, False):
            for rel, tag in table.reparse_tags:
                if not 0 <= rel < n:
                    continue
                mapped = base_of.get(rel, rel)
                if (mapped == rel) != own_pass or not 0 <= mapped < n:
                    continue
                if own_pass:
                    tag_from[mapped] = rel
                    reparse_tag_arr[mapped] = int(tag)
                else:
                    prev = tag_from[mapped]
                    # prev 就是自己时说明自身带的标签已经赢了，不能被扩展盖掉；
                    # prev 是别的扩展时，取记录号更小的那条
                    if prev < 0 or (prev != mapped and rel < prev):
                        tag_from[mapped] = rel
                        reparse_tag_arr[mapped] = int(tag)

    if MFT_ROOT >= n or not live[MFT_ROOT]:
        raise MftTreeError("MFT root record #5 missing")

    # 链接目标比标签稀少得多（整盘一千条上下），用字典只给真有值的那几条记账，
    # 比按记录号开一张全长数组省得多。
    link_from: dict[int, int] = {}
    for own_pass in (True, False):
        for rel, target in table.link_targets:
            if not 0 <= rel < n:
                continue
            mapped = base_of.get(rel, rel)
            if (mapped == rel) != own_pass or not 0 <= mapped < n:
                continue
            if own_pass:
                link_from[mapped] = rel
                link_target_arr[mapped] = target
            else:
                prev = link_from.get(mapped, -1)
                if prev < 0 or (prev != mapped and rel < prev):
                    link_from[mapped] = rel
                    link_target_arr[mapped] = target

    # 名字行两种来源：分块路径在解析阶段就逐段折成了定长数组，这里只按
    # 父记录号串链表；整表路径（单进程回退与测试）照旧按记录号分组建字典。
    child_of: Callable[[int], list[tuple[int, int | str, int]]] | None = None
    children: dict[int, list[tuple[int, str, int]]] | None = None
    fold = table.fold
    if fold is not None:
        m = len(fold)
        first = array("i", [-1]) * (n + 1)
        nxt = array("i", [-1]) * m
        child_map = array("i", [-1]) * m
        for k in range(m):
            c = fold.child[k]
            if c < 0 or c >= n:
                continue
            target = base_of.get(c, c)
            if target == MFT_ROOT or target < 0 or target >= n or not live[target]:
                continue
            p = fold.parent[k]
            if p < 0 or p > n:
                continue
            child_map[k] = target
            nxt[k] = first[p]
            first[p] = k

        def child_of(p: int) -> list[tuple[int, int, int]]:
            # 链表是头插入，取出来要反转一次才和原来的插入顺序一致。
            # 第二项传名字在文本池里的编号，解码交给 emit 时按需做，
            # 这样建树期间不用为每一条记录常驻一个字符串。
            out: list[tuple[int, int, int]] = []
            k = first[p] if 0 <= p <= n else -1
            while k >= 0:
                out.append((child_map[k], k, fold.real[k]))
                k = nxt[k]
            out.reverse()
            return out

    else:
        # 名字行按记录序连续；同记录局部 has_win/seen，一遍直建 children
        children = defaultdict(list)
        names = table.names
        nn = len(names)
        i = 0
        while i < nn:
            idx0 = names.idx[i]
            j = i + 1
            while j < nn and names.idx[j] == idx0:
                j += 1
            # [i, j) 同一记录的全部 name 行
            if 0 <= idx0 < n:
                target = base_of.get(idx0, idx0)
                if target != MFT_ROOT and 0 <= target < n and live[target]:
                    has_win = False
                    for k in range(i, j):
                        if names.ns[k] in (0, 1, 3):
                            has_win = True
                            break
                    seen: set[tuple[int, str]] = set()
                    for k in range(i, j):
                        ns = names.ns[k]
                        if has_win and ns == 2:
                            continue
                        parent = names.parent[k]
                        name = names.name_at(k)
                        key = (parent, name)
                        if key in seen:
                            continue
                        seen.add(key)
                        children[parent].append((target, name, names.real[k]))
            i = j

    if _span_end:
        _span_end("mft_tree_merge")

    return _build_from_base(
        root_mtime=mtime_arr[MFT_ROOT],
        is_dir=is_dir_arr,
        data_size=data_size_arr,
        mtime=mtime_arr,
        children=children,
        reparse_tag=reparse_tag_arr,
        link_target=link_target_arr,
        actual_size=actual_size_arr,
        follow_reparse=follow_reparse,
        progress=progress,
        cancel=cancel,
        on_batch=on_batch,
        timer=timer,
        live=live,
        on_link=on_link,
        child_of=child_of,
        fold=fold,
    )


def build_entry_rows(
    parsed: list[ParsedRecord | None],
    *,
    follow_reparse: bool = False,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    on_batch: OnBatch | None = None,
    timer: object | None = None,
) -> tuple[list[EntryRow], int, int, int, int, int]:
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
    reparse_tag = {num: rec.reparse_tag for num, rec in by_num.items()}
    link_target = {num: rec.link_target for num, rec in by_num.items()}
    actual_size = {num: rec.actual_size for num, rec in by_num.items()}
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
        reparse_tag=reparse_tag,
        link_target=link_target,
        actual_size=actual_size,
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
    rows, file_count, dir_count, total_size, _total_actual, _unrec = (
        build_entry_rows(
            parsed,
            follow_reparse=follow_reparse,
            progress=progress,
            cancel=cancel,
        )
    )
    entries = [
        Entry(
            id=r.id,
            parent_id=r.parent_id,
            name=r.name,
            size=r.size,
            is_dir=bool(r.is_dir),
            mtime=r.mtime,
            reparse_tag=r.reparse_tag,
            link_target=r.link_target,
            actual_size=r.actual_size,
        )
        for r in rows
    ]
    return entries, file_count, dir_count, total_size
