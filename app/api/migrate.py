"""目录迁移、链接管理与迁移历史（JS-API Mixin）。"""

from __future__ import annotations

import os
import threading
import time

from core import applog, i18n, fs_delete, fs_migrate
from core.scanner import query_free_size

from app.bootstrap import _delete_error_message, _migrate_error_message


# 迁移类作业的名字与它正在跑时的提示：被哪个占着就报哪一个
_LINK_JOB_BUSY = {
    "migrate": ("已有目录迁移正在进行", "A directory migration is already running"),
    "remigrate": ("已有迁移正在进行", "A migration is already running"),
    "restore": ("已有目录还原正在进行", "A directory restore is already running"),
}


class MigrateMixin:
    """迁移校验/执行/取消、链接删除与还原、历史记录。"""

    # ---- 迁移类作业的占用 ----
    # 一次只跑一个作业，判定依据是作业有没有在动文件，不是后台线程有没有退出。
    # 线程把终态推给界面之前先放开占位：那时文件操作与落盘都已结束，
    # 界面收到结果就能接着开下一项，不会撞上「已有迁移正在进行」。

    def _link_job_busy_error(self) -> dict | None:
        """有迁移类作业在跑时返回它对应的提示，空闲返回 None。"""
        with self._link_job_lock:
            job = self._link_job
        if not job:
            return None
        return {"error": i18n.t(*(_LINK_JOB_BUSY.get(job) or _LINK_JOB_BUSY["migrate"]))}

    def _claim_link_job(self, job: str) -> tuple[dict | None, int]:
        """占住作业位，返回拦下的错误与这一次运行的编号。

        占位成功时错误为 None。编号把释放限定在自己这一次运行上：
        上一项推送完还有收尾动作，不能把下一项刚占的位清掉。
        """
        with self._link_job_lock:
            busy = self._link_job
            if busy:
                text = _LINK_JOB_BUSY.get(busy) or _LINK_JOB_BUSY["migrate"]
                return {"error": i18n.t(*text)}, 0
            self._link_seq += 1
            self._link_job = job
            self._link_job_token = self._link_seq
            return None, self._link_seq

    def _release_link_job(self, token: int) -> None:
        """放开自己占的作业位；编号对不上说明已经换人，什么都不做。"""
        with self._link_job_lock:
            if token and self._link_job_token == token:
                self._link_job = ""
                self._link_job_token = 0

    def _emit_link_job_done(self, token: int, event: str, payload: dict) -> None:
        """作业收尾的推送：先放开占位，再通知界面。"""
        self._release_link_job(token)
        self._emit(event, payload)

    def _run_link_job(self, token: int, body, args: tuple) -> None:
        """后台作业的统一外壳：正常结束与中途异常都要放开占位。"""
        try:
            body(*args)
        finally:
            self._release_link_job(token)

    def validate_directory_migration(self, source: str = "", dest: str = "") -> dict:
        """迁移前置校验（不执行任何操作）。返回 ``{ok, code, message, source, dest, aside}``。"""
        pre = fs_migrate.validate_migrate(source or "", dest or "")
        pre["message"] = (
            "" if pre.get("ok")
            else _migrate_error_message(str(pre.get("code") or ""))
        )
        # 只在输入合法时附带存放盘剩余量，供表单提示 [存放盘剩余 xx]
        if pre.get("ok"):
            pre["dest_free"] = query_free_size(str(pre.get("dest") or ""))
        return pre

    def validate_directory_remigration(self, link: str = "", dest: str = "") -> dict:
        """再次迁移的前置校验（不执行任何操作）。

        返回 ``{ok, code, message, link, dest, old_target}``；合法时附 ``dest_free``。
        """
        pre = fs_migrate.validate_remigrate(link or "", dest or "")
        pre["message"] = (
            "" if pre.get("ok")
            else _migrate_error_message(str(pre.get("code") or ""))
        )
        if pre.get("ok"):
            pre["dest_free"] = query_free_size(str(pre.get("dest") or ""))
        return pre

    def start_directory_migration(self, source: str = "", dest: str = "", backup_action: str = "keep") -> dict:
        """后台线程执行目录迁移，进度与结果经事件推送。

        ``backup_action`` 决定迁移完成后备份目录的去向：``keep`` 留在原处，
        ``recycle`` 移入回收站，``purge`` 彻底删除。

        事件：``migrate-dir-progress`` {stage, done, total, bytes_done,
        bytes_total, current} / ``migrate-dir-done`` {ok, source, dest,
        aside, files, bytes, backup_action?, backup_cleaned?,
        backup_permanent?, error?, code?}。
        返回 ``{"started": True}``；已有迁移在跑则返回 error。
        """
        busy = self._link_job_busy_error()
        if busy:
            return busy
        fs_migrate.set_backup_suffix(i18n.t("原目录备份", " original backup"))
        fs_migrate.set_old_target_suffix(i18n.t("旧迁移", " old migration"))
        pre = fs_migrate.validate_migrate(source or "", dest or "")
        if not pre.get("ok"):
            return {
                "error": _migrate_error_message(str(pre.get("code") or "invalid_source")),
                "code": pre.get("code") or "invalid_source",
            }
        # 剩余空间预检：扫一次访问路径算总字节，塞不下直接拒绝（不启动线程）。
        # 不放进 validate_migrate —— 它被前端防抖反复调用，访问路径内容多时每次全扫会卡输入。
        src = str(pre["source"])
        dst = str(pre["dest"])
        try:
            space_ok = fs_migrate.free_space_ok(src, dst)
        except fs_migrate.MigrateError as exc:
            # 扫访问路径时撞上读不到的目录：迁移不启动，把位置报出来
            return {
                "error": _migrate_error_message(exc.message),
                "code": exc.message.split(":", 1)[0],
            }
        if not space_ok:
            return {
                "error": _migrate_error_message("not_enough_space"),
                "code": "not_enough_space",
            }
        # 校验与剩余空间预检都是只读操作，做完再正式占位，占位到启动之间不再有失败出口
        busy, token = self._claim_link_job("migrate")
        if busy:
            return busy
        self._migrate_cancel.clear()
        try:
            threading.Thread(
                target=self._run_link_job,
                args=(
                    token,
                    self._run_directory_migration,
                    (src, dst, str(backup_action or "keep"), token),
                ),
                daemon=True,
            ).start()
        except BaseException:
            self._release_link_job(token)
            raise
        return {"started": True}

    def cancel_directory_migration(self) -> dict:
        """请求取消正在进行的目录迁移。"""
        self._migrate_cancel.set()
        return {"ok": True}

    def _ask_for(self, cancel: threading.Event):
        """给 core 层用的 ask 回调：把迁移的提问转给前端，等用户答复。"""

        def _ask(kind: str, payload: dict) -> str:
            return self._ask(kind, payload, cancel)

        return _ask

    def _run_directory_migration(
        self, source: str, dest: str, backup_action: str = "keep", job_token: int = 0
    ) -> None:
        """后台线程体：执行迁移并推送进度与终态事件。"""
        # 校验结论由核心流程回填，随历史条目一同写入
        verify_info: dict = {}
        def on_progress(info: dict) -> None:
            self._emit("migrate-dir-progress", dict(info or {}))

        try:
            result = fs_migrate.migrate_directory(
                source,
                dest,
                progress=on_progress,
                cancel=self._migrate_cancel.is_set,
                backup_action=str(backup_action or "keep"),
                ask=self._ask_for(self._migrate_cancel),
                verify_out=verify_info,
            )
        except fs_migrate.MigrateError as exc:
            code = str(exc.message or "fail")
            cancelled = (
                self._migrate_cancel.is_set()
                or code == "cancelled"
                or code == "aborted"
            )
            applog.info(
                f"directory migration failed | code={code} source={source}"
            )
            fs_migrate.record_history({
                "ts": time.time(),
                "verify": verify_info.get("mode") or "",
                "op": "migrate",
                "source": source,
                "target": dest,
                "backup": "",
                "files": 0,
                "bytes": 0,
                "status": "cancelled" if cancelled else "failed",
                "error": _migrate_error_message(code),
                "code": code,
                "backup_cleaned": False,
            })
            self._emit_link_job_done(
                job_token,
                "migrate-dir-done",
                {
                    "ok": False,
                    "source": source,
                    "dest": dest,
                    "error": _migrate_error_message(code),
                    "code": code,
                },
            )
            return
        except Exception as exc:  # noqa: BLE001
            applog.exception("directory migration failed", exc)
            fs_migrate.record_history({
                "ts": time.time(),
                "verify": verify_info.get("mode") or "",
                "op": "migrate",
                "source": source,
                "target": dest,
                "backup": "",
                "files": 0,
                "bytes": 0,
                "status": "failed",
                "error": str(exc),
                "code": "fail",
                "backup_cleaned": False,
            })
            self._emit_link_job_done(
                job_token,
                "migrate-dir-done",
                {
                    "ok": False,
                    "source": source,
                    "dest": dest,
                    "error": i18n.t(
                        f"目录迁移失败：{exc}",
                        f"Directory migration failed: {exc}",
                    ),
                    "code": "fail",
                },
            )
            return
        applog.info(
            f"directory migration ok | source={source} dest={dest} "
            f"files={result.get('files')} bytes={result.get('bytes')}"
        )
        fs_migrate.register_link(
            str(result.get("source") or ""),
            str(result.get("dest") or ""),
            str(result.get("aside") or ""),
            files=int(result.get("files") or 0),
            bytes_=int(result.get("bytes") or 0),
        )
        fs_migrate.record_history({
            "ts": time.time(),
            "verify": verify_info.get("mode") or "",
            "op": "migrate",
            "source": str(result.get("source") or source),
            "target": str(result.get("dest") or dest),
            "backup": str(result.get("aside") or ""),
            "files": int(result.get("files") or 0),
            "bytes": int(result.get("bytes") or 0),
            "links": int(result.get("links") or 0),
            "skipped": int(result.get("skipped") or 0),
            "status": "success",
            "error": "",
            "code": "",
            "backup_cleaned": bool(result.get("backup_cleaned") or False),
            "backup_permanent": bool(result.get("backup_permanent") or False),
        })
        self._emit_link_job_done(job_token, "migrate-dir-done", result)

    def list_directory_links(self) -> dict:
        """列出工具创建的所有目录链接及当前状态（供「目录链接」面板渲染）。"""
        return {"ok": True, "links": fs_migrate.link_records()}

    def import_link(self, link: str = "", target: str = "") -> dict:
        """把一条已经在别处建好的目录链接收进登记表。

        target 是用户填的[存放位置]。填了就要求它和链接真正指向的目录
        一致，对不上或者两个框填反了都返回错误码，由界面提示。
        """
        try:
            rec = fs_migrate.import_link(str(link or ""), str(target or ""))
        except fs_migrate.MigrateError as exc:
            code = str(exc)
            return {"error": _migrate_error_message(code), "code": code}
        except Exception as exc:  # noqa: BLE001
            applog.exception("import_link failed", exc)
            return {"error": str(exc), "code": "fail"}
        return {"ok": True, "record": rec}

    def import_directory_links(self, links: list | None = None) -> dict:
        """把一批已有的目录链接收进登记表。

        逐条导入，一条失败不影响其他条。失败的连同错误码一起返回，由前端
        按条显示原因。
        """
        imported: list[dict] = []
        failed: list[dict] = []
        for link in list(links or []):
            try:
                imported.append(fs_migrate.import_link(str(link)))
            except fs_migrate.MigrateError as exc:
                code = str(exc)
                failed.append(
                    {
                        "link": str(link),
                        "code": code,
                        "error": _migrate_error_message(code),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                applog.exception("import_directory_links failed", exc)
                failed.append({"link": str(link), "code": "fail", "error": str(exc)})
        if imported:
            applog.info(f"directory links imported | count={len(imported)}")
        return {"ok": True, "imported": imported, "failed": failed}

    def list_migration_history(self) -> dict:
        """列出迁移/还原历史（即便链接已还原、记录被移除，仍可查）。"""
        try:
            return {"ok": True, "history": fs_migrate.load_history()}
        except Exception as exc:  # noqa: BLE001
            applog.exception("list_migration_history failed", exc)
            return {"error": str(exc), "code": "fail"}

    def clear_migration_history(self) -> dict:
        """清空迁移/还原历史。"""
        try:
            n = fs_migrate.clear_history()
            return {"ok": True, "cleared": int(n)}
        except Exception as exc:  # noqa: BLE001
            applog.exception("clear_migration_history failed", exc)
            return {"error": str(exc), "code": "fail"}

    def start_directory_remigration(
        self, link: str = "", dest: str = "", old_target_action: str = "keep"
    ) -> dict:
        """后台线程把已迁移目录再搬到新目标，进度与结果经事件推送。

        ``old_target_action``：``keep`` 把旧迁移改名成 "<名称> 旧迁移" 留在原处
        （登记里记下路径，面板据此给 [删除]）| ``recycle`` 移入回收站 |
        ``purge`` 彻底删除。

        事件与普通迁移共用：``migrate-dir-progress`` / ``migrate-dir-done``
        （``source`` 是那条链接路径，``dest`` 是新目标，另附 ``old_target*``）。
        返回 ``{"started": True}``；已有迁移或再次迁移在跑则返回 error。
        """
        busy = self._link_job_busy_error()
        if busy:
            return busy
        fs_migrate.set_backup_suffix(i18n.t("原目录备份", " original backup"))
        fs_migrate.set_old_target_suffix(i18n.t("旧迁移", " old migration"))
        pre = fs_migrate.validate_remigrate(link or "", dest or "")
        if not pre.get("ok"):
            code = str(pre.get("code") or "unknown_link")
            return {"error": _migrate_error_message(code), "code": code}
        old_target = str(pre["old_target"])
        dst = str(pre["dest"])
        try:
            space_ok = fs_migrate.free_space_ok(old_target, dst)
        except fs_migrate.MigrateError as exc:
            # 扫旧迁移时撞上读不到的目录：不启动，把位置报出来
            return {
                "error": _migrate_error_message(exc.message),
                "code": exc.message.split(":", 1)[0],
            }
        if not space_ok:
            return {
                "error": _migrate_error_message("not_enough_space"),
                "code": "not_enough_space",
            }
        # 校验与剩余空间预检都是只读操作，做完再正式占位，占位到启动之间不再有失败出口
        busy, token = self._claim_link_job("remigrate")
        if busy:
            return busy
        self._remigrate_cancel.clear()
        try:
            threading.Thread(
                target=self._run_link_job,
                args=(
                    token,
                    self._run_directory_remigration,
                    (str(pre["link"]), dst, str(old_target_action or "keep"), token),
                ),
                daemon=True,
            ).start()
        except BaseException:
            self._release_link_job(token)
            raise
        return {"started": True}

    def cancel_directory_remigration(self) -> dict:
        """请求取消正在进行的再次迁移。"""
        self._remigrate_cancel.set()
        return {"ok": True}

    def _run_directory_remigration(
        self, link: str, dest: str, old_target_action: str = "keep", job_token: int = 0
    ) -> None:
        """后台线程体：执行再次迁移并推送进度与终态事件。"""
        # 校验结论由核心流程回填，随历史条目一同写入
        verify_info: dict = {}
        def on_progress(info: dict) -> None:
            self._emit("migrate-dir-progress", dict(info or {}))

        rec_snapshot = fs_migrate.get_link_record(link or "") or {}
        old_target = str(rec_snapshot.get("target") or "")
        backup = str(rec_snapshot.get("backup") or "")
        try:
            result = fs_migrate.remigrate_link(
                link,
                dest,
                progress=on_progress,
                cancel=self._remigrate_cancel.is_set,
                old_target_action=str(old_target_action or "keep"),
                ask=self._ask_for(self._remigrate_cancel),
                verify_out=verify_info,
            )
        except fs_migrate.MigrateError as exc:
            code = str(exc.message or "fail")
            cancelled = (
                self._remigrate_cancel.is_set()
                or code == "cancelled"
                or code == "aborted"
            )
            applog.info(f"directory re-migration failed | code={code} link={link}")
            fs_migrate.record_history({
                "ts": time.time(),
                "verify": verify_info.get("mode") or "",
                "op": "migrate",
                "source": link,
                "target": dest,
                "backup": backup,
                "files": 0,
                "bytes": 0,
                "status": "cancelled" if cancelled else "failed",
                "error": _migrate_error_message(code),
                "code": code,
                "backup_cleaned": False,
            })
            self._emit_link_job_done(
                job_token,
                "migrate-dir-done",
                {
                    "ok": False,
                    "source": link,
                    "dest": dest,
                    "error": _migrate_error_message(code),
                    "code": code,
                },
            )
            return
        except Exception as exc:  # noqa: BLE001
            applog.exception("directory re-migration failed", exc)
            fs_migrate.record_history({
                "ts": time.time(),
                "verify": verify_info.get("mode") or "",
                "op": "migrate",
                "source": link,
                "target": dest,
                "backup": backup,
                "files": 0,
                "bytes": 0,
                "status": "failed",
                "error": str(exc),
                "code": "fail",
                "backup_cleaned": False,
            })
            self._emit_link_job_done(
                job_token,
                "migrate-dir-done",
                {
                    "ok": False,
                    "source": link,
                    "dest": dest,
                    "error": i18n.t(f"迁移失败：{exc}", f"Migration failed: {exc}"),
                    "code": "fail",
                },
            )
            return
        applog.info(
            f"directory re-migrated | link={result.get('link')} "
            f"target={result.get('target')} old_target={result.get('old_target')} "
            f"action={result.get('old_target_action')} "
            f"left={result.get('old_target_left') or ''} "
            f"removed={bool(result.get('old_target_removed'))}"
        )
        fs_migrate.record_history({
            "ts": time.time(),
            "verify": verify_info.get("mode") or "",
            "op": "migrate",
            "source": str(result.get("link") or link),
            "target": str(result.get("target") or dest),
            "backup": backup,
            "files": int(result.get("files") or 0),
            "bytes": int(result.get("bytes") or 0),
            "links": int(result.get("links") or 0),
            "skipped": int(result.get("skipped") or 0),
            "status": "success",
            "error": "",
            "code": "",
            "backup_cleaned": False,
        })
        payload = dict(result)
        payload["source"] = str(result.get("link") or link)
        payload["dest"] = str(result.get("target") or dest)
        self._emit_link_job_done(job_token, "migrate-dir-done", payload)

    def delete_directory_link(self, link: str = "") -> dict:
        """删除一条工具创建的链接（只删 junction，数据留在目标）。"""
        try:
            rec = fs_migrate.get_link_record(link or "")
            fs_migrate.delete_junction(link or "")
        except fs_migrate.MigrateError as exc:
            code = str(exc.message or "fail")
            return {"error": _migrate_error_message(code), "code": code}
        except Exception as exc:  # noqa: BLE001
            applog.exception("delete_directory_link failed", exc)
            return {
                "error": i18n.t(
                    f"删除链接失败：{exc}", f"Failed to delete link: {exc}"
                ),
                "code": "fail",
            }
        fs_migrate.remove_link_record(link or "")
        applog.info(
            f"directory link deleted | link={link or ''} "
            f"target={(rec or {}).get('target') or ''}"
        )
        return {"ok": True, "link": link or ""}

    def restore_directory_link(self, link: str = "", permanent: bool = False) -> dict:
        """后台线程还原一条链接：存放位置复制回访问路径，删链接，备份移入回收站。

        ``permanent=True`` 时备份改为彻底删除（不进回收站）。
        事件：``migrate-dir-restore-progress`` {stage, done, total,
        bytes_done, bytes_total, current} / ``migrate-dir-restore-done``
        {ok, link, target, recycled_backup?, backup_permanent?, error?, code?}。
        返回 ``{"started": True}``；已有还原在跑则返回 error。
        """
        busy = self._link_job_busy_error()
        if busy:
            return busy
        rec = fs_migrate.get_link_record(link or "")
        if not rec:
            return {"error": _migrate_error_message("unknown_link"), "code": "unknown_link"}
        _link = str(rec.get("link") or "")
        _target = str(rec.get("target") or "")
        if not fs_migrate._is_reparse(_link):
            return {"error": _migrate_error_message("not_a_link"), "code": "not_a_link"}
        if not _target or not os.path.isdir(_target):
            return {"error": _migrate_error_message("target_missing"), "code": "target_missing"}
        # 链接与目录都在，对它们只做只读检查，随后正式占位
        busy, token = self._claim_link_job("restore")
        if busy:
            return busy
        self._restore_cancel.clear()
        try:
            threading.Thread(
                target=self._run_link_job,
                args=(
                    token,
                    self._run_directory_restore,
                    (_link, bool(permanent), token),
                ),
                daemon=True,
            ).start()
        except BaseException:
            self._release_link_job(token)
            raise
        return {"started": True}

    def cancel_directory_restore(self) -> dict:
        """请求取消正在进行的目录还原。"""
        self._restore_cancel.set()
        return {"ok": True}

    def _run_directory_restore(self, link: str, permanent: bool = False, job_token: int = 0) -> None:
        """后台线程体：执行还原并推送进度与终态事件。"""
        # 校验结论由核心流程回填，随历史条目一同写入
        verify_info: dict = {}
        def on_progress(info: dict) -> None:
            self._emit("migrate-dir-restore-progress", dict(info or {}))

        rec_snapshot = fs_migrate.get_link_record(link or "") or {}
        try:
            result = fs_migrate.restore_link(
                link,
                progress=on_progress,
                cancel=self._restore_cancel.is_set,
                permanent=bool(permanent),
                verify_out=verify_info,
            )
        except fs_migrate.MigrateError as exc:
            code = str(exc.message or "fail")
            cancelled = self._restore_cancel.is_set() or code == "cancelled"
            applog.info(f"directory link restore failed | code={code} link={link}")
            fs_migrate.record_history({
                "ts": time.time(),
                "verify": verify_info.get("mode") or "",
                "op": "restore",
                "source": str(rec_snapshot.get("link") or link),
                "target": str(rec_snapshot.get("target") or ""),
                "backup": str(rec_snapshot.get("backup") or ""),
                "files": 0,
                "bytes": 0,
                "status": "cancelled" if cancelled else "failed",
                "error": _migrate_error_message(code),
                "code": code,
                "backup_cleaned": False,
            })
            self._emit_link_job_done(
                job_token,
                "migrate-dir-restore-done",
                {"ok": False, "link": link, "error": _migrate_error_message(code), "code": code},
            )
            return
        except Exception as exc:  # noqa: BLE001
            applog.exception("directory link restore failed", exc)
            fs_migrate.record_history({
                "ts": time.time(),
                "verify": verify_info.get("mode") or "",
                "op": "restore",
                "source": str(rec_snapshot.get("link") or link),
                "target": str(rec_snapshot.get("target") or ""),
                "backup": str(rec_snapshot.get("backup") or ""),
                "files": 0,
                "bytes": 0,
                "status": "failed",
                "error": str(exc),
                "code": "fail",
                "backup_cleaned": False,
            })
            self._emit_link_job_done(
                job_token,
                "migrate-dir-restore-done",
                {
                    "ok": False,
                    "link": link,
                    "error": i18n.t(f"还原失败：{exc}", f"Restore failed: {exc}"),
                    "code": "fail",
                },
            )
            return
        applog.info(
            f"directory link restored | link={result.get('link')} "
            f"recycled_backup={result.get('recycled_backup') or ''} "
            f"backup_permanent={bool(result.get('backup_permanent'))} "
            f"recycled_target={result.get('recycled_target') or ''}"
        )
        fs_migrate.record_history({
            "ts": time.time(),
            "verify": verify_info.get("mode") or "",
            "op": "restore",
            "source": str(result.get("link") or link),
            "target": str(result.get("target") or rec_snapshot.get("target") or ""),
            "backup": str(rec_snapshot.get("backup") or ""),
            "files": int(rec_snapshot.get("files") or 0),
            "bytes": int(rec_snapshot.get("bytes") or 0),
            "links": int(result.get("links") or 0),
            "skipped": int(result.get("skipped") or 0),
            "status": "success",
            "error": "",
            "code": "",
            "backup_cleaned": bool(result.get("recycled_backup") or False),
            "backup_permanent": bool(result.get("backup_permanent") or False),
        })
        self._emit_link_job_done(job_token, "migrate-dir-restore-done", result)

    def delete_old_target(self, link: str = "", permanent: bool = False) -> dict:
        """删除再次迁移留下的旧迁移目录。默认移到回收站；``permanent=True`` 彻底删除。

        删掉后把登记里的 ``old_target`` 清空。彻底删除途中按时间节流推
        ``link-backup-progress`` ``{link, items}``，供面板显示已删了多少项。
        """
        busy = self._link_job_busy_error()
        if busy:
            return busy
        rec = fs_migrate.get_link_record(link or "")
        if not rec:
            return {
                "error": _migrate_error_message("unknown_link"),
                "code": "unknown_link",
            }
        old_target = str(rec.get("old_target") or "")
        link_path = str(rec.get("link") or link)
        if not old_target:
            return {
                "error": _migrate_error_message("no_old_target"),
                "code": "no_old_target",
            }
        if not os.path.isdir(old_target):
            # 目录已经被人删掉了，登记里还留着：清掉它。否则面板上这个入口删不掉，
            # 这条链接也会一直被旧迁移挡着、不能再次迁移。
            fs_migrate.set_link_old_target(link_path, "")
            return {"ok": True, "link": link_path, "cleared": True}

        def on_progress(info: dict) -> None:
            self._emit("link-backup-progress", {
                "link": link_path,
                "items": int(info.get("items") or 0),
            })

        try:
            fs_delete.delete_path(
                old_target, permanent=bool(permanent), on_progress=on_progress
            )
        except fs_delete.DeleteError as exc:
            code = str(exc.message or exc)
            return {"error": _delete_error_message(code), "code": code}
        except OSError as exc:
            return {
                "error": i18n.t(f"删除失败：{exc}", f"Delete failed: {exc}"),
                "code": "os",
            }
        fs_migrate.set_link_old_target(link_path, "")
        applog.info(
            f"old target deleted | link={link_path} old_target={old_target} "
            f"permanent={bool(permanent)}"
        )
        fs_migrate.record_history({
            "ts": time.time(),
            "op": "deloldtarget",
            "source": link_path,
            "target": old_target,
            "backup": "",
            "files": 0,
            "bytes": 0,
            "status": "success",
            "error": "",
            "code": "",
            # 删的是旧迁移不是备份，两个备份相关的标记位都留空，
            # 免得历史行里显示成「备份已删除」
            "backup_cleaned": False,
            "backup_permanent": False,
            "old_target_permanent": bool(permanent),
        })
        return {
            "ok": True,
            "link": link_path,
            "old_target": old_target,
            "permanent": bool(permanent),
        }

    def delete_link_backup(self, link: str = "", permanent: bool = False) -> dict:
        """删除某条链接的备份目录。默认移到回收站；``permanent=True`` 彻底删除。

        彻底删除途中按时间节流推 ``link-backup-progress`` ``{link, items}``，
        供面板把已删了多少项显示出来；回收站交给系统一次调用，拿不到逐项数字。
        """
        rec = fs_migrate.get_link_record(link or "")
        if not rec:
            return {
                "error": _migrate_error_message("unknown_link"),
                "code": "unknown_link",
            }
        backup = str(rec.get("backup") or "")
        if not backup or not os.path.isdir(backup):
            return {
                "error": _migrate_error_message("no_backup"),
                "code": "no_backup",
            }
        def on_progress(info: dict) -> None:
            self._emit("link-backup-progress", {
                "link": str(rec.get("link") or link),
                "items": int(info.get("items") or 0),
            })

        try:
            fs_delete.delete_path(backup, permanent=bool(permanent), on_progress=on_progress)
        except fs_delete.DeleteError as exc:
            code = str(exc.message or exc)
            return {"error": _delete_error_message(code), "code": code}
        except OSError as exc:
            return {
                "error": i18n.t(f"删除失败：{exc}", f"Delete failed: {exc}"),
                "code": "os",
            }
        applog.info(
            f"link backup deleted | link={link or ''} backup={backup} permanent={bool(permanent)}"
        )
        fs_migrate.record_history({
            "ts": time.time(),
            "op": "delbackup",
            "source": str(rec.get("link") or link),
            "target": str(rec.get("target") or ""),
            "backup": backup,
            "files": 0,
            "bytes": 0,
            "status": "success",
            "error": "",
            "code": "",
            "backup_cleaned": True,
            "backup_permanent": bool(permanent),
        })
        return {
            "ok": True,
            "backup": backup,
            "permanent": bool(permanent),
        }

    def remove_directory_link_record(self, link: str = "") -> dict:
        """从登记移除一条链接记录（不碰磁盘；用于链接已不存在的情况）。"""
        if not fs_migrate.get_link_record(link or ""):
            return {
                "error": _migrate_error_message("unknown_link"),
                "code": "unknown_link",
            }
        fs_migrate.remove_link_record(link or "")
        applog.info(f"directory link record removed | link={link or ''}")
        return {"ok": True, "link": link or ""}
