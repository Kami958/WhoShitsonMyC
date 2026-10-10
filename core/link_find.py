"""反查链接：找出哪些目录联接与符号链接指向某个目录。

两个来源二选一。扫盘：把选中的盘扫一遍，扫的过程中直接判链接指向是不是
目标，不落库。用快照：拿已有的快照查，目标路径不要求在快照的扫描根下。

两条通道用同一套链接判定规则（:mod:`core.link_match`）：「相对目标按链接
自身位置展开」「卷 GUID 目标跳过」这些规则和对比树里的反查保持一致。

任务跑在后台线程：界面关掉不中断它，进度与结果随时可取。
"""

from __future__ import annotations

import os
import threading
import time

from .link_match import link_hit, link_norm

_DRIVE_FIXED = 3


def fixed_drives() -> list[str]:
    """本机固定磁盘的根路径，如 ``["C:\\\\", "F:\\\\"]``。

    只认 ``DRIVE_FIXED``（内置硬盘与固态盘）。U 盘、移动硬盘、网络盘、
    光驱都不在内：这些盘可能在扫描途中被拔掉或断开，扫到一半消失只会让
    查询失败，用户也不该在这里选到它们。非 Windows 返回空表。
    """
    if os.name != "nt":
        return []
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        mask = int(k32.GetLogicalDrives())
    except Exception:  # noqa: BLE001 - 取不到盘符列表时当作没有可选盘
        return []
    out: list[str] = []
    for i in range(26):
        if not (mask >> i) & 1:
            continue
        root = f"{chr(ord('A') + i)}:\\"
        try:
            kind = int(k32.GetDriveTypeW(ctypes.c_wchar_p(root)))
        except Exception:  # noqa: BLE001
            continue
        if kind == _DRIVE_FIXED and os.path.isdir(root):
            out.append(root)
    return out


class _ScanMatcher:
    """扫盘模式的命中收集器。

    扫描途中每遇到一个链接就问一次它是不是要查的目标，整个盘扫完，结果
    也就在手里了；不像读快照那样先把行存进库再回头查一遍。

    行从扫描的 worker 线程流过来，可能并发，所以拿锁。
    """

    def __init__(self, root: str, target: str) -> None:
        self._root = root
        self._target = link_norm(target)
        self._lock = threading.Lock()
        self.hits: list[dict] = []

    def on_link(self, rel: str, tag: int, raw: str) -> None:
        """扫描发现一条链接时回调（可能来自不同线程）。"""
        hit = link_hit(self._root, self._target, rel, tag, raw)
        if hit is not None:
            with self._lock:
                self.hits.append(hit)


class LinkFind:
    """一次反查任务。

    构造后调用 :meth:`start` 开线程，此后 :meth:`status` / :meth:`result`
    随时可读，:meth:`cancel` 请求中止。
    """

    def __init__(
        self,
        target: str,
        mode: str,
        *,
        drives: list[str] | None = None,
        snapshot: str = "",
        on_done=None,
    ) -> None:
        """``mode`` 取 ``"scan"``（扫 ``drives`` 里的每个盘）或 ``"snapshot"``。

        Args:
            on_done: 可选 ``callable(result_dict)``，任务跑完（不论成败）
                在 worker 线程里调一次，供调用方落历史。抛异常被吞掉，
                不影响任务本身。
        """
        self.mode = "snapshot" if mode == "snapshot" else "scan"
        self.target = os.path.abspath(target or "")
        self._snapshot = os.path.abspath(snapshot) if snapshot else ""
        self._roots = [r for r in (drives or []) if r]
        self._sources: list[dict] = (
            [{"kind": "snapshot", "path": self._snapshot, "label": _snapshot_label(self._snapshot)}]
            if self.mode == "snapshot"
            else [{"kind": "scan", "root": r, "label": r.rstrip("\\/")} for r in self._roots]
        )
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._diff = None  # 查询中的 Diff，用于取消时打断它
        self._state = "running"
        self._phase = "scan"
        self._source_index = 0
        self._scanned = 0
        self._current = ""
        self._error = ""
        self._started_at = time.time()
        self._t0 = time.perf_counter()
        self._elapsed = 0.0
        self._hits: list[dict] = []
        self._unresolved: list[dict] = []
        self._errors: list[dict] = []
        self._ok_sources = 0
        self._on_done = on_done

    # ---- 对外 ----

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="link-find", daemon=True
        )
        self._thread.start()

    def cancel(self) -> None:
        """请求中止：置标记，并打断正在跑的那次查询。"""
        self._cancel.set()
        diff = self._diff
        if diff is not None:
            try:
                diff.cancel_search()
            except Exception:  # noqa: BLE001 - 会话恰好关闭等边界情况
                pass

    def status(self) -> dict:
        with self._lock:
            elapsed = (
                time.perf_counter() - self._t0
                if self._state == "running"
                else self._elapsed
            )
            return {
                "state": self._state,
                "running": self._state == "running",
                "phase": self._phase,
                "target": self.target,
                "mode": self.mode,
                "sources": [s["label"] for s in self._sources],
                "source_index": self._source_index,
                "source_count": len(self._sources),
                "scanned": self._scanned,
                "current": self._current,
                "elapsed_s": round(elapsed, 1),
                "started_at": self._started_at,
                "hit_total": len(self._hits),
                "error": self._error,
            }

    def result(self) -> dict:
        with self._lock:
            return {
                "ok": True,
                "state": self._state,
                "target": self.target,
                "mode": self.mode,
                "sources": [s["label"] for s in self._sources],
                "snapshot": self._snapshot,
                "roots": list(self._roots),
                "hits": list(self._hits),
                "unresolved": list(self._unresolved),
                "total": len(self._hits),
                "unresolved_total": len(self._unresolved),
                "errors": list(self._errors),
                "error": self._error,
                "elapsed_s": round(self._elapsed, 1),
                "started_at": self._started_at,
            }

    # ---- 线程体 ----

    def _run(self) -> None:
        from .compress import ensure_db_path
        from .differ import Diff, SearchCancelled
        from .scanner import ScanCancelled, scan_to_snapshot

        state = "done"
        try:
            for idx, src in enumerate(self._sources, 1):
                with self._lock:
                    self._source_index = idx
                if self._cancel.is_set():
                    state = "cancelled"
                    break
                try:
                    if src["kind"] == "scan":
                        # 扫盘：盘扫完结果就有了，不落库。只为了答一句
                        # 「谁指向它」先写一份整盘快照再回头查一遍，白占磁盘
                        # 与内存，磁盘写满时还会直接失败。
                        self._set_phase("scan")
                        matcher = _ScanMatcher(src["root"], self.target)
                        scan_to_snapshot(
                            src["root"],
                            None,
                            progress=self._on_scan_progress,
                            cancel=self._cancel.is_set,
                            on_link=matcher.on_link,
                        )
                        if self._cancel.is_set():
                            state = "cancelled"
                            break
                        # 扫盘时链接所在目录都扫过，没有「拼不出自己在哪」的条目
                        self._collect(matcher.hits, [], src)
                        continue
                    self._set_phase("open")
                    db = ensure_db_path(src["path"])
                    if self._cancel.is_set():
                        state = "cancelled"
                        break
                    self._set_phase("query")
                    diff = Diff(db, db)
                    with self._lock:
                        self._diff = diff
                    try:
                        res = diff.links_to_abs(self.target)
                    finally:
                        with self._lock:
                            self._diff = None
                        diff.close()
                    self._collect(
                        res.get("links") or [],
                        res.get("unresolved") or [],
                        src,
                    )
                except (ScanCancelled, SearchCancelled):
                    state = "cancelled"
                    break
                except Exception as exc:  # noqa: BLE001 - 单份来源失败不该终止整次查询
                    with self._lock:
                        self._errors.append(
                            {"source": src["label"], "message": str(exc)}
                        )
        except Exception as exc:  # noqa: BLE001 - 兜底，线程不能静默死掉
            with self._lock:
                self._error = str(exc)
                state = "failed"
        finally:
            with self._lock:
                self._elapsed = time.perf_counter() - self._t0
                # 一份来源都没查成功才算失败；部分来源报了错照常给结果，
                # 错在哪几条由 result() 里的 errors 带回去
                if state == "done" and self._ok_sources == 0 and self._errors:
                    state = "failed"
                self._state = state
                self._phase = state
                self._current = ""
            self._notify_done()

    def _notify_done(self) -> None:
        """任务结束回调；回调抛异常不影响任务状态。"""
        cb = self._on_done
        if cb is None:
            return
        try:
            cb(self.result())
        except Exception:  # noqa: BLE001
            pass

    def _set_phase(self, phase: str) -> None:
        with self._lock:
            self._phase = phase
            self._current = ""

    def _on_scan_progress(self, files: int, current: str) -> None:
        with self._lock:
            self._scanned = int(files or 0)
            self._current = current or ""

    def _collect(
        self, hits: list[dict], unresolved: list[dict], src: dict
    ) -> None:
        """把一份来源的命中并进结果。两条通道给出的字段都是 ``path``/``target``/``kind``。"""
        with self._lock:
            self._ok_sources += 1
            for it in hits:
                self._hits.append(
                    {
                        "link": it.get("path") or "",
                        "target": it.get("target") or "",
                        "kind": it.get("kind") or "",
                        "source": src["label"],
                    }
                )
            for it in unresolved:
                self._unresolved.append(
                    {
                        "link": it.get("path") or "",
                        "target": it.get("target") or "",
                        "kind": it.get("kind") or "",
                        "reason": it.get("reason") or "",
                        "source": src["label"],
                    }
                )


def _snapshot_label(path: str) -> str:
    """快照在界面上的短名：文件名去掉扩展名，如 ``F_2026-10-02_1530``。"""
    if not path:
        return ""
    name = os.path.basename(path)
    dot = name.rfind(".")
    return name[:dot] if dot > 0 else name
