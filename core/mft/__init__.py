"""NTFS MFT 快路径：仅用于「盘符根」全卷扫描。

与 :mod:`core.scanner` 的目录遍历解耦：

- **何时用**：Windows + 本地 NTFS + 扫描根是盘符根（如 ``C:\\``）
- **失败**：抛 :class:`MftUnavailable`，调用方回退常规扫描
- **语义**：默认不展开 reparse 目标卷；硬链接按多条 ``FILE_NAME`` 计
  （与「逻辑大小可能大于此电脑」一致）
- **解析**：记录多时用多进程 + SharedMemory 紧凑表直通建树（见
  :mod:`core.mft.pipeline` / :mod:`core.mft.parallel`）；进程数按核数与
  记录量自动推导，与设置页扫描线程数无关；开发可用 ``WSMC_MFT_WORKERS`` /
  ``WSMC_MFT_PROCS`` 覆盖

公开入口：:func:`is_mft_eligible`、:func:`scan_mft_to_snapshot`。
"""

from __future__ import annotations

from .scan import MftUnavailable, is_mft_eligible, scan_mft_to_snapshot

__all__ = [
    "MftUnavailable",
    "is_mft_eligible",
    "scan_mft_to_snapshot",
]
