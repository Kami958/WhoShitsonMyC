from __future__ import annotations

from __future__ import annotations

import threading


from .classify import _SideRow
from .errors import SearchCancelled
from .sideindex import _SideIndex

def _hits_from_index(
    side: _SideIndex,
    name_key: str,
    fetch_n: int,
    *,
    cancel: threading.Event | None = None,
) -> dict[str, _SideRow]:
    """内存索引版命中收集，返回值形状与 ``_hits_with_paths`` 一致。"""
    hits: dict[str, _SideRow] = {}
    idxs = side.find(name_key.casefold(), fetch_n)
    for n, i in enumerate(idxs):
        if cancel is not None and n % 2048 == 0 and cancel.is_set():
            raise SearchCancelled()
        path = side.path_at(i)
        if path:
            hits[path] = side.row_at(i)
    return hits


