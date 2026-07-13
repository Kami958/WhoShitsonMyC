"""可选的扫描计时探针（生产环境永远是空操作）。

正式逻辑通过本模块拿 timer，避免业务代码直接依赖 ``dev`` 包：

- 打包后的 exe（``sys.frozen``）→ 直接返回空实现；
- 源码运行且未安装/未启用 ``dev.scan_timing`` → 空实现；
- 源码 + ``WSMC_SCAN_TIMING=1`` → 使用 ``dev.scan_timing`` 真计时。

``build.py`` 另有 ``--exclude-module dev``，正常不会把开发包打进 exe。
"""

from __future__ import annotations

import sys
from typing import Any, Protocol


class ScanTimerProto(Protocol):
    def mark(self, name: str) -> None: ...
    def span_start(self, name: str) -> None: ...
    def span_end(self, name: str) -> None: ...
    def set_meta(self, **kwargs: Any) -> None: ...
    def finish(self, *, status: str = "ok") -> dict[str, Any] | None: ...


class NullScanTimer:
    """禁用时的空实现。"""

    def mark(self, name: str) -> None:
        return None

    def span_start(self, name: str) -> None:
        return None

    def span_end(self, name: str) -> None:
        return None

    def set_meta(self, **kwargs: Any) -> None:
        return None

    def finish(self, *, status: str = "ok") -> dict[str, Any] | None:
        return None


_NULL: ScanTimerProto = NullScanTimer()


def start_scan_timer(
    *,
    root: str = "",
    workers: int = 0,
    compress_enabled: bool = False,
) -> ScanTimerProto:
    """返回计时器；任何不可用情况都退回 :class:`NullScanTimer`。"""
    if getattr(sys, "frozen", False):
        return _NULL
    try:
        from dev.scan_timing import start_timer  # type: ignore[import-not-found]
    # 打包不会 dev 包因此必定报错，不会进入计时
    except ImportError:
        return _NULL
    return start_timer(
        root=root,
        workers=workers,
        compress_enabled=compress_enabled,
    )
