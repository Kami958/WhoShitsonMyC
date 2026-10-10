"""对比会话：对比、下钻、搜索、预热、路径删除（JS-API Mixin）。"""

from __future__ import annotations

import os
import time

from core import applog, i18n, store, fs_delete
from core.compress import (
    CompressError,
    drop_cache_for,
    ensure_db_path,
    is_session_cached,
)
from core.differ import Diff, DiffError, SearchCancelled
from core.snapshot import SnapshotError

from app.bootstrap import _delete_error_message


class CompareMixin:
    """Diff 会话生命周期与对比树操作。"""

    def compare_cache_status(self, old_path: str = "", new_path: str = "") -> dict:
        """对比前查询：两侧是否还需解压 ``.dbz``。

        供前端显示「正在解压」还是「对比中」，以进程内会话缓存为准，
        避免同快照再次对比时误提示解压。
        """
        old = str(old_path or "")
        new = str(new_path or "")
        # 同一对已打开的 Diff 会话：连连接都在，更不需要再解压
        key = (old, new) if old and new else None
        if (
            key is not None
            and self._diff is not None
            and self._diff_key == key
        ):
            return {
                "ok": True,
                "need_decompress": False,
                "old_cached": True,
                "new_cached": True,
                "session_ready": True,
            }
        old_cached = is_session_cached(old) if old else True
        new_cached = is_session_cached(new) if new else True
        return {
            "ok": True,
            "need_decompress": not (old_cached and new_cached),
            "old_cached": old_cached,
            "new_cached": new_cached,
            "session_ready": False,
        }

    def compare(self, old_path: str, new_path: str, need_marks: bool = False) -> dict:
        """对比两份快照，返回概览 + 顶层变化节点。

        Args:
            need_marks: 前端筛「新增/已删除」时传 True，后端启用「目录递归
                范围内含新增/删除」下钻标记（首启一次性全量计算，之后复用）。
                默认 False，首屏对比不为此额外扫库。

        Returns:
            成功：``{"summary": {...}, "nodes": [...]}``；
            失败：``{"error": "..."}``（如根不一致、文件损坏）。
        """
        try:
            with self._diff_lock:
                self._ensure_diff(old_path, new_path)
                assert self._diff is not None
                if need_marks:
                    self._diff.ensure_marks()
                nodes = self._diff.compare_children("")
                return {
                    "summary": self._summary(self._diff),
                    "nodes": [n.to_dict() for n in nodes],
                }
        except (DiffError, SnapshotError, CompressError) as exc:
            applog.warn(f"Compare rejected: {exc}")
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - 任何异常都要回 JSON，不能让前端悬死
            applog.exception("Compare failed", exc)
            return {"error": i18n.t(f"对比失败：{exc}", f"Comparison failed: {exc}")}

    def get_children(
        self, old_path: str, new_path: str, parent: str, need_marks: bool = False
    ) -> dict:
        """下钻：返回某父目录下的直接子节点对比结果。

        ``need_marks`` 语义同 :meth:`compare`：筛「新增/已删除」时的下钻
        请求带上，保证子节点也带下钻标记。
        """
        try:
            with self._diff_lock:
                self._ensure_diff(old_path, new_path)
                assert self._diff is not None
                if need_marks:
                    self._diff.ensure_marks()
                nodes = self._diff.compare_children(parent)
                return {"nodes": [n.to_dict() for n in nodes]}
        except (DiffError, SnapshotError, CompressError) as exc:
            applog.warn(f"get_children rejected: {exc}")
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - 同上，兜底成 error 响应
            applog.exception("get_children failed", exc)
            return {"error": i18n.t(
                f"读取子目录失败：{exc}", f"Failed to read subfolder: {exc}")}

    def _get_diff_children_for_modules(
        self, old_path: str, new_path: str, parent: str
    ) -> list:
        """给 AI packing 注入：返回子节点 dict 列表（失败返回 []，不抛）。"""
        try:
            with self._diff_lock:
                self._ensure_diff(old_path, new_path)
                assert self._diff is not None
                nodes = self._diff.compare_children(parent or "")
                out: list = []
                for n in nodes or []:
                    if hasattr(n, "to_dict"):
                        out.append(n.to_dict())
                    elif isinstance(n, dict):
                        out.append(n)
                return out
        except (DiffError, SnapshotError, CompressError) as exc:
            applog.warn(f"module get_diff_children rejected: {exc}")
            return []
        except Exception as exc:  # noqa: BLE001
            applog.exception("module get_diff_children failed", exc)
            return []

    def search_diff(
        self,
        old_path: str,
        new_path: str,
        query: str,
        limit: int = 50,
        offset: int = 0,
        sort: str = "delta-desc",
        case_sensitive: bool = False,
        exact: bool = False,
    ) -> dict:
        """在当前对比会话中按名称/路径搜索，返回匹配的对比节点（可分页）。"""
        try:
            t0 = time.perf_counter()
            with self._diff_lock:
                self._ensure_diff(old_path, new_path)
                assert self._diff is not None
                self._search_active = True
                try:
                    nodes, total = self._diff.search_by_name(
                        query,
                        limit=int(limit or 50),
                        offset=int(offset or 0),
                        sort=str(sort or "delta-desc"),
                        case_sensitive=bool(case_sensitive),
                        exact=bool(exact),
                    )
                finally:
                    self._search_active = False
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            return {
                "nodes": [n.to_dict() for n in nodes],
                "total": total,
                "query": (query or "").strip(),
                "limit": int(limit or 50),
                "offset": int(offset or 0),
                "sort": str(sort or "delta-desc"),
                "case_sensitive": bool(case_sensitive),
                "exact": bool(exact),
                "elapsed_ms": elapsed_ms,
            }
        except SearchCancelled:
            applog.info("search_diff cancelled by user")
            return {"cancelled": True}
        except (DiffError, SnapshotError, CompressError) as exc:
            applog.warn(f"search_diff rejected: {exc}")
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            applog.exception("search_diff failed", exc)
            return {"error": i18n.t(
                f"搜索失败：{exc}", f"Search failed: {exc}")}

    def cancel_search(self) -> dict:
        """强行中断正在进行的搜索，尽快释放 CPU 与对比会话锁。

        不获取 ``_diff_lock``（查询线程正持有它），直接置取消标记并用
        SQLite ``interrupt()`` 打断执行中的查询；空闲时调用是无害的空操作。
        反查走的是另一条路径，由 :meth:`cancel_link_find` 中断。
        """
        diff = self._diff
        if self._search_active and diff is not None:
            try:
                diff.cancel_search()
            except Exception:  # noqa: BLE001 - 会话恰好在关闭等边界
                pass
        return {"ok": True}

    # ---- 独立反查（迁移面板的[反查链接]）----

    def close_diff_session(self) -> dict:
        """关闭当前对比会话：释放搜索索引、SQLite 连接，并丢弃 .dbz 解压临时文件。

        前端清空对比结果时调用；空闲时是无害空操作。
        """
        try:
            with self._diff_lock:
                self._close_diff(drop_decompress_cache=True)
        except Exception:  # noqa: BLE001 - 关闭边界容错
            pass
        return {"ok": True}

    def start_search_preheat(self, old_path: str, new_path: str) -> dict:
        """打开搜索框时触发：按设置决定是否预热内存索引。

        关闭设置时直接返回 skipped；已就绪则补推 ready。
        """
        if not store.get_search_memory_index():
            return {"ok": True, "status": "skipped", "enabled": False}
        try:
            with self._diff_lock:
                self._ensure_diff(old_path, new_path)
                assert self._diff is not None
                st = self._diff.search_preheat_status()
                if st == "ready":
                    self._emit("search-preheat", {"status": "ready"})
                    return {"ok": True, "status": "ready", "enabled": True}
                if st == "started":
                    self._emit("search-preheat", {"status": "started"})
                    return {"ok": True, "status": "started", "enabled": True}
                # 尚未开始：挂回调并启动
                self._preheat_token += 1
                token = self._preheat_token

                def on_preheat_status(payload: dict) -> None:
                    if token != self._preheat_token:
                        return
                    data = dict(payload or {})
                    data.setdefault("status", "")
                    self._emit("search-preheat", data)

                self._diff.start_search_preheat(on_status=on_preheat_status)
                return {"ok": True, "status": "started", "enabled": True}
        except (DiffError, SnapshotError, CompressError) as exc:
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            applog.exception("start_search_preheat failed", exc)
            return {"error": i18n.t(
                f"搜索索引准备失败：{exc}",
                f"Failed to prepare search index: {exc}",
            )}

    def search_preheat_status(self, old_path: str = "", new_path: str = "") -> dict:
        """查询当前会话的搜索预热状态（供前端轮询兜底）。"""
        enabled = store.get_search_memory_index()
        if not enabled:
            return {"ok": True, "status": "skipped", "enabled": False}
        diff = self._diff
        key = (old_path, new_path) if old_path and new_path else None
        if diff is None or (key is not None and self._diff_key != key):
            return {"ok": True, "status": "idle", "enabled": True}
        return {
            "ok": True,
            "status": diff.search_preheat_status(),
            "enabled": True,
        }

    def _ensure_diff(self, old_path: str, new_path: str) -> None:
        """确保当前 :class:`Diff` 会话对应给定的两份快照，否则重开。

        压缩快照（``.dbz``）在这里才解压到临时文件；列表/选择阶段不解压。
        内存搜索索引不在对比时预热，而在打开搜索框时由
        :meth:`start_search_preheat` 触发。

        换一对快照或交换基准/当前时：只丢「新会话不再用到」的解压缓存，
        共用的 ``.dbz`` 临时文件继续复用，避免同快照反复解压。
        """
        key = (old_path, new_path)
        if self._diff is not None and self._diff_key == key:
            return
        prev_key = self._diff_key
        # 先关连接，暂不清解压缓存；下面只丢不再需要的路径
        self._close_diff(drop_decompress_cache=False)
        if prev_key:
            keep = {
                os.path.abspath(old_path or ""),
                os.path.abspath(new_path or ""),
            }
            for path in prev_key:
                try:
                    if path and os.path.abspath(path) not in keep:
                        drop_cache_for(path)
                except Exception:  # noqa: BLE001
                    pass
        try:
            old_db = ensure_db_path(old_path)
            new_db = ensure_db_path(new_path)
        except (CompressError, SnapshotError):
            raise
        self._diff = Diff(old_db, new_db)
        self._diff_key = key

    def _close_diff(self, *, drop_decompress_cache: bool = False) -> None:
        """关闭 Diff 会话。

        ``drop_decompress_cache=True`` 时，顺带删除本会话用过的 ``.dbz``
        解压临时文件（``core.compress`` 进程内登记）。仅在用户清空对比等
        「整段会话结束」时开启；换一对快照时由 :meth:`_ensure_diff` 按需丢弃。
        """
        # 作废旧预热推送，避免关会话后还刷「已就绪」
        self._preheat_token += 1
        key = self._diff_key
        if self._diff is not None:
            self._diff.close()
        self._diff = None
        self._diff_key = None
        if drop_decompress_cache and key:
            for path in key:
                try:
                    drop_cache_for(path)
                except Exception:  # noqa: BLE001 - 清临时文件失败不影响关会话
                    pass

    @staticmethod
    def _summary(diff: Diff) -> dict:
        """组装界面顶部概览所需的数据。"""
        return {
            "old": {
                "root": diff.old_meta.root,
                "scanned_at": diff.old_meta.scanned_at,
                "total_size": diff.old_meta.total_size,
                "skipped_count": len(diff.old_meta.skipped),
                "skipped": list(diff.old_meta.skipped or []),
            },
            "new": {
                "root": diff.new_meta.root,
                "scanned_at": diff.new_meta.scanned_at,
                "total_size": diff.new_meta.total_size,
                "skipped_count": len(diff.new_meta.skipped),
                "skipped": list(diff.new_meta.skipped or []),
            },
            "total_delta": diff.total_delta,
        }

    def check_pending_paths(self, items: list | None = None) -> dict:
        """批量评估「加入待删除」候选（不删文件、不入队）。

        供 AI 提议 / 人工确认前过滤：删除白名单、扫描根、盘符根、范围外。
        默认不因路径当前不存在而拒绝（真删时再校验）。

        每项 ``{root, rel|rel_path, name?, is_dir?}``；返回
        ``{ok, allowed: [...], rejected: [...]}``。
        """
        bl = store.get_delete_blacklist()
        raw = items if isinstance(items, list) else []
        allowed: list[dict] = []
        rejected: list[dict] = []
        # 防止一次塞爆
        cap = 200
        for i, item in enumerate(raw[:cap]):
            if not isinstance(item, dict):
                rejected.append(
                    {
                        "index": i,
                        "code": "invalid",
                        "error": _delete_error_message("invalid"),
                    }
                )
                continue
            root = str(item.get("root") or "").strip()
            rel = str(item.get("rel_path") or item.get("rel") or "").strip()
            name = str(item.get("name") or "").strip()
            is_dir = item.get("is_dir")
            ev = fs_delete.evaluate_pending_candidate(
                root, rel, bl, require_exists=False
            )
            row = {
                "index": i,
                "root": ev.get("root") or root,
                "rel": ev.get("rel") if ev.get("rel") is not None else rel,
                "path": ev.get("path") or "",
                "name": name or (ev.get("path") or rel or root),
                "is_dir": bool(is_dir) if is_dir is not None else False,
                "code": ev.get("code") or "invalid",
            }
            if item.get("reason") is not None:
                row["reason"] = str(item.get("reason") or "")
            if ev.get("ok"):
                row["ok"] = True
                if "exists" in ev:
                    row["exists"] = bool(ev.get("exists"))
                allowed.append(row)
            else:
                code = str(ev.get("code") or "invalid")
                row["ok"] = False
                row["error"] = _delete_error_message(code, root=root, rel_path=rel)
                rejected.append(row)
        return {
            "ok": True,
            "allowed": allowed,
            "rejected": rejected,
            "truncated": False if len(raw) <= cap else True,
        }

    def delete_path(
        self,
        root: str,
        rel_path: str = "",
        permanent: bool = False,
    ) -> dict:
        """删除对比树节点对应的真实路径。

        默认进回收站；``permanent=True`` 为彻底删除。
        会校验：位于对比根下、非根/盘符根、不在黑名单、路径存在。
        彻底删除途中按时间节流推 ``delete-progress`` ``{root, rel, items}``，
        供待删除列表把正在删的那一项报出已删了多少项；回收站交给系统一次调用，
        拿不到逐项数字。
        """
        bl = store.get_delete_blacklist()

        def on_progress(info: dict) -> None:
            self._emit("delete-progress", {
                "root": root,
                "rel": rel_path,
                "items": int(info.get("items") or 0),
            })

        try:
            full = fs_delete.assert_deletable(root, rel_path, bl)
            fs_delete.delete_path(full, permanent=bool(permanent), on_progress=on_progress)
        except fs_delete.DeleteError as exc:
            code = str(exc.message or exc)
            msg = _delete_error_message(code, root=root, rel_path=rel_path)
            applog.info(f"delete_path denied/failed | code={code} permanent={bool(permanent)}")
            # code 给前端分类标注（missing / blacklist / …）；文案仍走 error
            return {"error": msg, "code": code}
        except OSError as exc:
            applog.exception("delete_path OSError", exc)
            return {
                "error": i18n.t(f"删除失败：{exc}", f"Delete failed: {exc}"),
                "code": "os",
            }
        except Exception as exc:  # noqa: BLE001
            applog.exception("delete_path failed", exc)
            return {
                "error": i18n.t(f"删除失败：{exc}", f"Delete failed: {exc}"),
                "code": "fail",
            }
        applog.info(f"delete_path ok | permanent={bool(permanent)}")
        return {
            "ok": True,
            "path": full,
            "permanent": bool(permanent),
            "recycled": not bool(permanent),
        }
