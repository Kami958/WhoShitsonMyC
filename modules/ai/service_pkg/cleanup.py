"""对比树清理：多切片推进与取消。"""

from __future__ import annotations

from core import applog
from modules.ai import config as ai_config
from modules.ai import packing as ai_packing
from typing import Any
import uuid

class CleanupMixin:
    def start_compare_cleanup(
        self,
        old_path: str = "",
        new_path: str = "",
        seed: dict | None = None,
        root: str = "",
        **kwargs: Any,
    ) -> dict:
        """从对比树 seed（默认根）开始清理 packing，返回第一批 context。

        不调用模型、不删除。前端把 ``context`` 交给 ``ask``。
        入口：对比树右键；磁盘清理另入口（本方法不负责）。
        """
        old = str(old_path or kwargs.get("old_path") or "").strip()
        new = str(new_path or kwargs.get("new_path") or "").strip()
        root_s = str(root or kwargs.get("root") or "").strip()
        raw_seed = seed if seed is not None else kwargs.get("seed")
        if not old or not new:
            return {
                "error": self._t(
                    "缺少对比快照路径",
                    "Missing compare snapshot paths",
                )
            }
        if not callable(self._get_diff_children):
            return {
                "error": self._t(
                    "对比子项接口不可用",
                    "Compare children API unavailable",
                )
            }

        seed_node: dict[str, Any]
        if isinstance(raw_seed, dict) and (
            raw_seed.get("path")
            or raw_seed.get("rel")
            or raw_seed.get("rel_path")
            or raw_seed.get("name")
            or "is_dir" in raw_seed
        ):
            seed_node = dict(raw_seed)
            # 统一 rel：对比树 node.path 即相对根
            if not seed_node.get("rel") and not seed_node.get("rel_path"):
                rel = str(seed_node.get("path") or "").strip()
                # 若 path 是绝对路径且带 root，尽量剥 rel
                if root_s and rel.lower().startswith(root_s.lower().rstrip("\\/")):
                    try:
                        import os

                        rel = os.path.relpath(rel, root_s)
                        if rel.startswith(".."):
                            rel = str(seed_node.get("path") or "")
                    except (OSError, ValueError):
                        pass
                # 前端常传 rel_path 在 path 字段（相对）
                if "\\" in rel or "/" in rel or rel:
                    # 若仍像绝对盘符，保留空 rel 表示根
                    if len(rel) >= 2 and rel[1] == ":":
                        seed_node["rel"] = ""
                        seed_node["rel_path"] = ""
                    else:
                        seed_node["rel"] = rel.replace("/", "\\")
                        seed_node["rel_path"] = seed_node["rel"]
            else:
                r = str(
                    seed_node.get("rel") or seed_node.get("rel_path") or ""
                ).replace("/", "\\")
                seed_node["rel"] = r
                seed_node["rel_path"] = r
            if "is_dir" not in seed_node:
                seed_node["is_dir"] = True
        else:
            seed_node = {
                "rel": "",
                "rel_path": "",
                "path": root_s or "",
                "name": root_s or "root",
                "is_dir": True,
                "new_size": 0,
                "old_size": 0,
                "delta": 0,
                "kind": "",
            }

        get_children = self._make_diff_get_children(old, new)
        cfg = ai_config.load(self._app_data_dir())
        max_depth = int(cfg.get("cleanup_max_depth") or ai_packing.CLEANUP_MAX_DEPTH)
        try:
            job = ai_packing.start_cleanup_job(
                seed_node, get_children, root=root_s
            )
            packed = ai_packing.pack_cleanup_slice(
                job, get_children, max_depth=max_depth
            )
        except Exception as exc:  # noqa: BLE001
            applog.exception("AI start_compare_cleanup failed", exc)
            return {
                "error": self._t(
                    f"清理上下文打包失败：{exc}",
                    f"Failed to pack cleanup context: {exc}",
                )
            }

        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            # 单任务简化：新开清理时清掉旧 job，避免堆积
            self._cleanup_jobs.clear()
            self._cleanup_jobs[job_id] = {
                "job": job,
                "old_path": old,
                "new_path": new,
                "root": root_s,
            }
        applog.info(
            f"AI compare cleanup started | job={job_id}"
            f" | slice={packed.get('slice')} | paths={packed.get('paths_in_slice')}"
            f" | has_more={packed.get('has_more')}"
        )
        return self._cleanup_slice_response(job_id, packed)

    def next_compare_cleanup(self, job_id: str = "", **kwargs: Any) -> dict:
        """同一清理任务的下一批 context；无更多则 has_more=false。"""
        jid = str(job_id or kwargs.get("job_id") or "").strip()
        if not jid:
            return {
                "error": self._t("缺少任务 id", "Missing job id"),
            }
        with self._lock:
            slot = self._cleanup_jobs.get(jid)
            if not slot:
                return {
                    "error": self._t(
                        "没有进行中的清理任务，或已结束",
                        "No active cleanup job (ended or unknown)",
                    )
                }
            job = slot["job"]
            old = str(slot.get("old_path") or "")
            new = str(slot.get("new_path") or "")

        if not getattr(job, "pending", None):
            packed = {
                "scenario": ai_packing.SCENARIO_CLEANUP,
                "slice": getattr(job, "slice_index", 0),
                "has_more": False,
                "paths_in_slice": 0,
                "items": [],
                "deferred_top": [],
                "children": [],
            }
            seed = getattr(job, "seed", None) or {}
            if isinstance(seed, dict):
                for key in (
                    "path",
                    "rel",
                    "rel_path",
                    "name",
                    "is_dir",
                    "kind",
                    "old_size",
                    "new_size",
                    "delta",
                ):
                    if key in seed:
                        packed[key] = seed[key]
            with self._lock:
                self._cleanup_jobs.pop(jid, None)
            return self._cleanup_slice_response(jid, packed)

        get_children = self._make_diff_get_children(old, new)
        cfg = ai_config.load(self._app_data_dir())
        max_depth = int(cfg.get("cleanup_max_depth") or ai_packing.CLEANUP_MAX_DEPTH)
        try:
            packed = ai_packing.pack_cleanup_slice(
                job, get_children, max_depth=max_depth
            )
        except Exception as exc:  # noqa: BLE001
            applog.exception("AI next_compare_cleanup failed", exc)
            return {
                "error": self._t(
                    f"下一批打包失败：{exc}",
                    f"Failed to pack next slice: {exc}",
                )
            }

        if not packed.get("has_more"):
            with self._lock:
                self._cleanup_jobs.pop(jid, None)
        applog.info(
            f"AI compare cleanup slice | job={jid}"
            f" | slice={packed.get('slice')} | paths={packed.get('paths_in_slice')}"
            f" | has_more={packed.get('has_more')}"
        )
        return self._cleanup_slice_response(jid, packed)

    def cancel_compare_cleanup(self, job_id: str = "", **kwargs: Any) -> dict:
        """丢弃清理任务（可选 id；空则清空全部）。"""
        jid = str(job_id or kwargs.get("job_id") or "").strip()
        with self._lock:
            if not jid:
                n = len(self._cleanup_jobs)
                self._cleanup_jobs.clear()
                return {"ok": True, "cancelled": n}
            if jid in self._cleanup_jobs:
                self._cleanup_jobs.pop(jid, None)
                return {"ok": True, "cancelled": 1}
        return {"ok": True, "cancelled": 0}
