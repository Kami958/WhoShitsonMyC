"""搜索预热：与读盘重叠，提前把两侧索引、名字表拉热。"""

from __future__ import annotations

from .sideindex import _SideIndex
from .. import applog
from ..snapshot import open_readonly
import sqlite3
import threading
import time


class DiffPreheatMixin:

    def start_search_preheat(self, on_status=None) -> None:
        """后台把两侧条目读进内存建搜索索引；重复调用是空操作。

        就绪后子串搜索不再全表扫库（首搜也快）；建索引期间的搜索
        自动走原有 SQL 路径。

        Args:
            on_status: 可选 ``callable(dict)``。推送
                ``{"status": "started"|"ready"|"failed"|"aborted", ...}``。
        """
        # 已就绪：无需再预热
        if self._sides is not None:
            if on_status is not None:
                self._preheat_on_status = on_status
                self._report_preheat("ready")
            return
        # 线程仍在跑：只更新回调，不重复开线程
        th = self._preheat_thread
        if th is not None and th.is_alive():
            if on_status is not None:
                self._preheat_on_status = on_status
            return
        # 未就绪且无线程 / 上次失败已结束：允许重新预热
        self._preheat_stop.clear()
        self._preheat_on_status = on_status
        self._preheat_thread = threading.Thread(
            target=self._preheat_worker, name="search-preheat", daemon=True
        )
        self._preheat_thread.start()

    def _report_preheat(self, status: str, **extra) -> None:
        """向 UI 回调推送预热状态；回调异常不影响预热本身。"""
        cb = self._preheat_on_status
        if cb is None:
            return
        payload = {"status": status, **extra}
        try:
            cb(payload)
        except Exception:  # noqa: BLE001
            pass

    def _preheat_worker(self) -> None:
        conns: list[sqlite3.Connection] = []
        t0 = time.perf_counter()
        applog.debug("search preheat started")
        self._report_preheat("started")
        try:
            built: list[_SideIndex] = []
            for path in self._db_paths:
                if self._preheat_stop.is_set():
                    applog.debug("search preheat aborted (session closing)")
                    self._report_preheat("aborted")
                    return
                conn = open_readonly(path)
                conns.append(conn)
                side = _SideIndex.build(conn, self._preheat_stop)
                if side is None:
                    applog.debug("search preheat aborted (session closing)")
                    self._report_preheat("aborted")
                    return
                built.append(side)
            self._sides = (built[0], built[1])
            total_entries = len(built[0]) + len(built[1])
            total_mb = (built[0].approx_bytes() + built[1].approx_bytes()) / 1e6
            elapsed_s = time.perf_counter() - t0
            applog.info(
                f"search preheat done: {total_entries} entries "
                f"(old {len(built[0])} / new {len(built[1])}), "
                f"index ~{total_mb:.0f}MB, {elapsed_s:.1f}s"
            )
            self._report_preheat(
                "ready",
                entries=int(total_entries),
                index_mb=round(total_mb, 1),
                elapsed_s=round(elapsed_s, 2),
            )
        except MemoryError as exc:
            # 内存不足：放弃索引，搜索继续走 SQL 路径
            applog.error(
                "search preheat out of memory, falling back to SQL search",
                exc=exc,
            )
            self._report_preheat("failed", reason="oom")
        except Exception as exc:  # noqa: BLE001 - 预热失败静默回退 SQL 路径
            applog.exception("search preheat failed, falling back to SQL search", exc)
            self._report_preheat("failed", reason="error")
        finally:
            for conn in conns:
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover
                    pass

    def wait_search_preheat(self, timeout: float | None = None) -> bool:
        """等预热线程结束（测试用）；返回索引是否就绪。"""
        th = self._preheat_thread
        if th is not None:
            th.join(timeout)
        return self._sides is not None

    def search_preheat_status(self) -> str:
        """当前预热状态：ready / started / idle（供 UI 补推）。"""
        if self._sides is not None:
            return "ready"
        th = self._preheat_thread
        if th is not None and th.is_alive():
            return "started"
        return "idle"
