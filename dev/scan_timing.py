"""扫描分段计时（仅开发用）。

启用方式（源码运行）::

    set WSMC_SCAN_TIMING=1
    python app.py

可选把每次结果追加到 JSONL::

    set WSMC_SCAN_TIMING_LOG=dict\\scan-timing.jsonl

关闭：不设环境变量，或设为 0。

打包后的 exe：``sys.frozen`` 为真时一律禁用；``build.py`` 还会
``--exclude-module dev``，正常不会打进包。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any


def is_enabled() -> bool:
    """是否启用扫描计时。

    规则：
    - PyInstaller 冻结环境（正式 exe）→ 永远 False；
    - 否则看环境变量 ``WSMC_SCAN_TIMING`` 是否为 1/true/yes/on。
    """
    # 检查是否是打包的
    if getattr(sys, "frozen", False):
        return False
    # 检查环境变量是否开启
    flag = os.environ.get("WSMC_SCAN_TIMING", "").strip().lower()
    return flag in ("1", "true", "yes", "on")


@dataclass
class ScanTimer:
    """一次扫描的分段计时器（线程安全的 mark）。"""

    root: str = ""
    workers: int = 0
    compress_enabled: bool = False
    _t0: float = field(default_factory=time.perf_counter)
    _marks: dict[str, float] = field(default_factory=dict)
    _spans: dict[str, float] = field(default_factory=dict)
    _meta: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _active_spans: dict[str, float] = field(default_factory=dict)

    def mark(self, name: str) -> None:
        """记录一个绝对时间点（距 start 的秒数）。"""
        with self._lock:
            self._marks[name] = time.perf_counter() - self._t0

    def span_start(self, name: str) -> None:
        with self._lock:
            self._active_spans[name] = time.perf_counter()

    def span_end(self, name: str) -> None:
        with self._lock:
            t1 = self._active_spans.pop(name, None)
            if t1 is None:
                return
            self._spans[name] = self._spans.get(name, 0.0) + (
                time.perf_counter() - t1
            )

    def set_meta(self, **kwargs: Any) -> None:
        with self._lock:
            self._meta.update(kwargs)

    def finish(self, *, status: str = "ok") -> dict[str, Any]:
        """结束并汇总；打印到 stderr，可选写 JSONL。返回报表 dict。"""
        total = time.perf_counter() - self._t0
        with self._lock:
            meta = dict(self._meta)
            # MFT 等路径会在 set_meta(workers=…) 写入实际并发；优先用它
            workers_out = meta.get("workers", self.workers)
            report: dict[str, Any] = {
                "status": status,
                "root": self.root,
                "workers": workers_out,
                "compress_enabled": self.compress_enabled,
                "total_s": round(total, 4),
                "spans_s": {k: round(v, 4) for k, v in sorted(self._spans.items())},
                "marks_s": {k: round(v, 4) for k, v in sorted(self._marks.items())},
                "meta": meta,
            }
            # 吞吐：files/s、entries/s（文件+目录），total_s>0 且有计数时写入
            if total > 0:
                fc = meta.get("file_count")
                dc = meta.get("dir_count")
                if isinstance(fc, (int, float)) and fc >= 0:
                    report["files_per_s"] = round(fc / total, 1)
                if (
                    isinstance(fc, (int, float))
                    and isinstance(dc, (int, float))
                    and fc >= 0
                    and dc >= 0
                ):
                    report["entries_per_s"] = round((fc + dc) / total, 1)
        _emit_report(report)
        return report


class _NullTimer:
    """禁用时的空实现，方法全是 no-op，避免业务代码分支爆炸。"""

    root: str = ""
    workers: int = 0
    compress_enabled: bool = False

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


_NULL = _NullTimer()


def start_timer(
    *,
    root: str = "",
    workers: int = 0,
    compress_enabled: bool = False,
) -> ScanTimer | _NullTimer:
    """若启用则返回真计时器，否则返回空实现。"""
    if not is_enabled():
        return _NULL
    return ScanTimer(
        root=root,
        workers=workers,
        compress_enabled=compress_enabled,
    )


def _emit_report(report: dict[str, Any]) -> None:
    """stderr 人类可读一行摘要 + 可选 JSONL。"""
    spans = report.get("spans_s") or {}
    meta = report.get("meta") or {}
    parts = [
        f"[scan-timing] status={report.get('status')}",
        f"total={report.get('total_s')}s",
        f"workers={report.get('workers')}",
    ]
    for key in (
        "scan_to_snapshot",
        "mft_read",
        "mft_parse",
        "mft_parse_wait",
        "mft_tree",
        "mft_tree_merge",
        "mft_tree_bfs",
        "mft_tree_agg",
        "mft_tree_emit",
        "mft_write_join",
        "drain_rows",
        "finalize",
        "compress",
    ):
        if key in spans:
            parts.append(f"{key}={spans[key]}s")
    if "mft_parse_gather_s" in meta:
        parts.append(f"mft_parse_gather={meta['mft_parse_gather_s']}s")
    if "backend" in meta:
        parts.append(f"backend={meta['backend']}")
    if "file_count" in meta:
        parts.append(f"files={meta['file_count']}")
    if "dir_count" in meta:
        parts.append(f"dirs={meta['dir_count']}")
    # 吞吐：优先 files/s；有 entries/s 时一并打出
    if "files_per_s" in report:
        parts.append(f"files/s={report['files_per_s']}")
    if "entries_per_s" in report:
        parts.append(f"entries/s={report['entries_per_s']}")
    if "db_bytes" in meta:
        parts.append(f"db={meta['db_bytes']}B")
    if "dbz_bytes" in meta:
        parts.append(f"dbz={meta['dbz_bytes']}B")
    parts.append(f"root={report.get('root')!r}")
    print(" ".join(str(p) for p in parts), file=sys.stderr)

    log_path = os.environ.get("WSMC_SCAN_TIMING_LOG", "").strip()
    if not log_path:
        return
    try:
        log_path = os.path.abspath(log_path)
        parent = os.path.dirname(log_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(report, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[scan-timing] write log failed: {exc}", file=sys.stderr)
