"""两份快照的对比：生成变化树。

对外主类是 :class:`~core.differ_pkg.diff.Diff`。它按「目录层级、懒加载」的
方式工作——每次只对比某个父目录下的**直接子节点**
（:meth:`~core.differ_pkg.diff.Diff.compare_children`），用户在界面上展开
哪一层才对比哪一层。这样即便快照有上百万条记录，对比也永远只处理当前
可见的一层，UI 不会卡（见设计文档第 6 节）。

v3 快照是邻接表结构（只存本段名字 + 父行 id）。对外接口仍收发
**路径字符串**（前端无感知）：会话内维护 ``{相对路径 → (旧侧 id, 新侧 id)}``
缓存，下钻时把父路径解析成两侧的 parent_id，各查一次子节点、按名字合并。

对比结果的诚实性：

- **根不同不给比**：两份快照扫的根不一样时直接报错，避免驴唇不对马嘴。
- **格式过旧不给比**：v1/v2 快照缺少邻接表结构，提示重新扫描。
- **跳过目录标记为不可比较**：某目录在一侧因无权限被跳过、另一侧扫到了，
  不谎报成「暴涨」，而是标 :attr:`~core.models.ChangeKind.INCOMPARABLE`。
"""

from .diff import Diff
from .errors import DiffError, DiffRootMismatch, SearchCancelled
from .links import compare_snapshots

__all__ = [
    "ChangeKind",
    "Diff",
    "DiffError",
    "DiffNode",
    "DiffRootMismatch",
    "SearchCancelled",
    "compare_snapshots",
]
