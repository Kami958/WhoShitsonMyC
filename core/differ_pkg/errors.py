from __future__ import annotations

from __future__ import annotations


from ..i18n import t

class DiffError(Exception):
    """对比相关错误。"""


class SearchCancelled(DiffError):
    """搜索被用户取消。"""

    def __init__(self) -> None:
        super().__init__(t("搜索已取消", "Search cancelled"))


class DiffRootMismatch(DiffError):
    """两份快照的扫描根不一致，无法对比。"""

    def __init__(self, old_root: str, new_root: str) -> None:
        self.old_root = old_root
        self.new_root = new_root
        super().__init__(
            t(f"两份快照扫描的根目录不同，无法对比：\n  旧：{old_root} \n  新：{new_root}",
              f"The two snapshots scanned different root folders and cannot be compared:"
              f"\n  Base: {old_root} \n  Current: {new_root}")
        )


