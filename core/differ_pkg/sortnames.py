from __future__ import annotations

from __future__ import annotations



from .classify import _SearchEntry, _child_path

def _entry_delta(entry: _SearchEntry) -> int:
    _parent, _name, o, n = entry
    return (n[1] if n else 0) - (o[1] if o else 0)


def _entry_old_size(entry: _SearchEntry) -> int:
    o = entry[2]
    return o[1] if o else 0


def _entry_new_size(entry: _SearchEntry) -> int:
    """当前侧大小；展开/同快照浏览时就是占用。"""
    n = entry[3]
    if n is not None:
        return n[1]
    o = entry[2]
    return o[1] if o else 0


def _entry_mtime(entry: _SearchEntry) -> int:
    _parent, _name, o, n = entry
    if n is not None:
        return n[3]
    if o is not None:
        return o[3]
    return 0


def _entry_pct(entry: _SearchEntry) -> float:
    """|delta| / 旧大小；旧不存在且有变化视为无穷大（与前端 deltaPct 一致）。"""
    delta = _entry_delta(entry)
    old = _entry_old_size(entry)
    if old > 0:
        return abs(delta) / old
    return float("inf") if delta != 0 else 0.0


def _sort_search_entries(
    entries: list[_SearchEntry], sort: str
) -> list[_SearchEntry]:
    """按与变化树相同的规则排序搜索候选（稳定次键用路径）。"""
    if not entries:
        return entries
    if sort == "name-asc":
        return sorted(entries, key=lambda e: (e[1].casefold(), _child_path(e[0], e[1])))
    if sort == "name-desc":
        return sorted(
            entries,
            key=lambda e: (e[1].casefold(), _child_path(e[0], e[1])),
            reverse=True,
        )
    if sort == "mtime-desc":
        return sorted(
            entries,
            key=lambda e: (-_entry_mtime(e), _child_path(e[0], e[1])),
        )
    if sort == "pct-desc":
        return sorted(
            entries,
            key=lambda e: (
                -_entry_pct(e),
                -abs(_entry_delta(e)),
                _child_path(e[0], e[1]),
            ),
        )
    if sort == "size-desc":
        return sorted(
            entries,
            key=lambda e: (
                -_entry_new_size(e),
                e[1].casefold(),
                _child_path(e[0], e[1]),
            ),
        )
    # delta-desc 默认
    return sorted(
        entries,
        key=lambda e: (-abs(_entry_delta(e)), _child_path(e[0], e[1])),
    )


