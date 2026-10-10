"""历史入口：实现已拆到 :mod:`core.differ_pkg`，此模块保留导入兼容。

新代码请直接从 ``core.differ_pkg`` 导入；``tests`` monkeypatch 的
``differ_mod._hits_with_paths`` 走 :mod:`core.differ_pkg.hitspaths`。
"""

from .differ_pkg import (
    Diff,
    DiffError,
    DiffRootMismatch,
    SearchCancelled,
    compare_snapshots,
)
from .differ_pkg.links import _LINK_CANDIDATE_SQL, _LINK_REL_BATCH
from .differ_pkg.hitspaths import _hits_with_paths

__all__ = [
    "Diff",
    "DiffError",
    "DiffRootMismatch",
    "SearchCancelled",
    "compare_snapshots",
    "_LINK_CANDIDATE_SQL",
    "_LINK_REL_BATCH",
    "_hits_with_paths",
]
