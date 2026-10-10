"""独立反查：找出哪些链接指向某目录（JS-API Mixin）。"""

from __future__ import annotations

import os
import time

from core import applog, i18n, fs_migrate
from core.link_find import LinkFind, fixed_drives


class LinkFindMixin:
    """反查任务启动/状态/结果/取消与历史落账。"""

    def list_fixed_drives(self) -> dict:
        """本机可选的固定磁盘，供反查选扫描范围。U 盘、网络盘不在内。"""
        drives = fixed_drives()
        return {
            "ok": True,
            "drives": [{"root": d, "label": d.rstrip("\\/")} for d in drives],
        }

    def start_link_find(
        self,
        target: str = "",
        mode: str = "scan",
        drives: list | None = None,
        snapshot: str = "",
    ) -> dict:
        """发起一次反查：找出哪些链接指向目录 ``target``。

        后台线执行，立即返回；进度用 :meth:`link_find_status` 取，结果用
        :meth:`link_find_result` 取。``mode="scan"`` 时现场扫 ``drives`` 里的
        每个盘（库是临时的，不留快照），``mode="snapshot"`` 时查 ``snapshot``
        这份已有快照，目标不要求在快照的扫描根下。

        跑完自动写一条 ``linkfind`` 历史。
        """
        path = os.path.abspath(str(target or "").strip().strip('"'))
        if not path:
            return {"error": i18n.t("请先填写要查的目录", "Enter a folder first")}
        if not os.path.isdir(path):
            return {"error": i18n.t(
                f"目录不存在：{path}", f"Folder does not exist: {path}")}
        prev = self._link_find
        if prev is not None and prev.status().get("running"):
            return {"error": i18n.t(
                "已有反查在跑，等它结束或取消它",
                "A lookup is already running; wait for it or cancel it")}

        if mode == "snapshot":
            snap = str(snapshot or "").strip()
            if not snap:
                return {"error": i18n.t("请先选择快照", "Pick a snapshot first")}
            if not os.path.isfile(snap):
                return {"error": i18n.t(
                    f"快照文件不存在：{snap}",
                    f"Snapshot file does not exist: {snap}")}
            task = LinkFind(path, "snapshot", snapshot=snap, on_done=self._link_find_done)
            scope = os.path.basename(snap)
        else:
            roots = [str(d) for d in (drives or []) if d]
            if not roots:
                return {"error": i18n.t(
                    "请至少勾选一个盘", "Select at least one drive")}
            task = LinkFind(path, "scan", drives=roots, on_done=self._link_find_done)
            scope = "、".join(r.rstrip("\\/") for r in roots)

        # 不记查询的路径（隐私），只记来源
        applog.info(f"Link find starting mode={task.mode} scope={scope}")
        self._link_find = task
        task.start()
        return {"started": True, "scope": scope}

    def link_find_status(self) -> dict:
        """当前反查任务的进度；没有任务时 ``state`` 为 ``idle``。"""
        task = self._link_find
        if task is None:
            return {"ok": True, "state": "idle", "running": False}
        return {"ok": True, **task.status()}

    def link_find_result(self) -> dict:
        """当前反查任务的结果（含每条命中的链接）。"""
        task = self._link_find
        if task is None:
            return {"ok": True, "state": "idle", "hits": [], "total": 0}
        return task.result()

    def cancel_link_find(self) -> dict:
        """取消正在跑的反查任务（不删已查到的部分结果）。"""
        task = self._link_find
        if task is not None:
            task.cancel()
        return {"ok": True}

    def _link_find_done(self, res: dict) -> None:
        """反查跑完的回调：把这次查询连同结果记进历史。"""
        state = str(res.get("state") or "")
        status = {
            "done": "success",
            "failed": "failed",
            "cancelled": "cancelled",
        }.get(state, "failed")
        error = str(res.get("error") or "")
        if not error and res.get("errors"):
            error = str(res["errors"][0].get("message") or "")
        try:
            fs_migrate.record_history({
                "op": "linkfind",
                "ts": float(res.get("started_at") or time.time()),
                "status": status,
                "source": str(res.get("target") or ""),
                "mode": str(res.get("mode") or ""),
                "roots": list(res.get("roots") or []),
                "snapshot": str(res.get("snapshot") or ""),
                "hits": list(res.get("hits") or []),
                "hit_total": int(res.get("total") or 0),
                "unresolved": list(res.get("unresolved") or []),
                "unresolved_total": int(res.get("unresolved_total") or 0),
                "elapsed_s": float(res.get("elapsed_s") or 0.0),
                "error": error,
            })
            self._emit("link-find-done", {"state": state, "total": int(res.get("total") or 0)})
        except Exception as exc:  # noqa: BLE001 - 记不上历史不该影响查询结果
            applog.exception("link find history record failed", exc)
