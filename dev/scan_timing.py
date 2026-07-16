"""扫描分段计时（开发用，输出并入 ``core.applog``）。

启用（源码；``sys.frozen`` exe 永远关）::

    $env:WSMC_LOG_LEVEL = "DEBUG"   # 推荐：日志 + 计时同一开关
    # 或兼容旧习惯：
    $env:WSMC_SCAN_TIMING = "1"

可选 JSONL（机器读，与设置页缓冲独立）::

    $env:WSMC_SCAN_TIMING_LOG = "dict\\scan-timing.jsonl"

``build.py --exclude-module dev``，正常不打进包。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from core import applog


def is_enabled() -> bool:
    """是否启用扫描计时。

    - 正式 exe → False
    - ``WSMC_SCAN_TIMING`` 为真 → True（兼容）
    - 否则与 applog 一致：门槛允许 DEBUG 时启用
    """
    if getattr(sys, "frozen", False):
        return False
    flag = os.environ.get("WSMC_SCAN_TIMING", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    return applog.is_enabled("DEBUG")


@dataclass
class ScanTimer:
    """一次扫描的分段计时器（线程安全）。"""

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
        total = time.perf_counter() - self._t0
        with self._lock:
            meta = dict(self._meta)
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
    if not is_enabled():
        return _NULL
    return ScanTimer(
        root=root,
        workers=workers,
        compress_enabled=compress_enabled,
    )


def _format_summary(report: dict[str, Any]) -> str:
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
    if "files_per_s" in report:
        parts.append(f"files/s={report['files_per_s']}")
    if "entries_per_s" in report:
        parts.append(f"entries/s={report['entries_per_s']}")
    if "db_bytes" in meta:
        parts.append(f"db={meta['db_bytes']}B")
    if "dbz_bytes" in meta:
        parts.append(f"dbz={meta['dbz_bytes']}B")
    parts.append(f"root={report.get('root')!r}")
    return " ".join(str(p) for p in parts)


def _emit_report(report: dict[str, Any]) -> None:
    """唯一出口：applog；可选 JSONL。"""
    line = _format_summary(report)
    status = str(report.get("status") or "ok")
    if status in ("error", "compress_failed"):
        applog.warn(line)
    elif status == "cancelled":
        applog.info(line)
    elif applog.is_enabled("DEBUG"):
        applog.debug(line)
    else:
        # 仅 WSMC_SCAN_TIMING=1 且门槛仍是 INFO 时，保证摘要可见
        applog.info(line)

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
        applog.warn(f"[scan-timing] write log failed: {exc}")
