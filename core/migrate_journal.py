"""迁移切换日志。

改名让位和建立链接之间没有原子性。写入一条待完成记录后，如果进程在这个
窗口内结束，启动时就能按原路径和备份路径把目录改回来，而不是让用户自己
去磁盘上找带后缀的备份。

日志只记切换窗口，迁移历史仍由现有模块负责。读写失败只写日志，不阻断迁移。
"""

from __future__ import annotations

import json
import os
import time
import uuid

from . import applog

_FILE_NAME = "migration_journal.json"


def _path() -> str:
    from . import store

    return os.path.join(store.app_base_dir_path(), _FILE_NAME)


def _load() -> list[dict]:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _save(entries: list[dict]) -> None:
    path = _path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False)
    os.replace(tmp, path)


def begin_switch(src: str, aside: str, dst: str) -> str:
    """在改名让位之前记一条；返回记录编号。

    写不进去只记日志，不阻断迁移：日志是恢复辅助，不是迁移的前置条件。
    """
    entry_id = uuid.uuid4().hex
    entries = _load()
    entries.append(
        {
            "id": entry_id,
            "src": src,
            "aside": aside,
            "dst": dst,
            "ts": time.time(),
        }
    )
    try:
        _save(entries)
    except OSError as exc:
        applog.warn(
            f"migration journal write failed | reason={type(exc).__name__}"
        )
    return entry_id


def finish_switch(entry_id: str) -> None:
    """切换完成或回滚成功：删掉这一条。"""
    entries = [e for e in _load() if str(e.get("id") or "") != entry_id]
    try:
        _save(entries)
    except OSError as exc:
        applog.warn(
            f"migration journal cleanup failed | reason={type(exc).__name__}"
        )


def pending_entries() -> list[dict]:
    """还没收尾的切换记录，供启动时提示。

    原路径已经存在的记录说明链接已经建成或迁移还在改名之前，不提示恢复。
    这里只过滤、不删记录：迁移进行中刷新页面时，记录必须留着，
    否则进程在改名之后被杀就再也找不到备份了。
    """
    entries = _load()
    alive: list[dict] = []
    for entry in entries:
        src = str(entry.get("src") or "")
        if src and os.path.lexists(src):
            continue
        alive.append(entry)
    return alive


def recover(entry_id: str, *, action: str) -> dict:
    """处理一条未完成记录。

    ``restore`` 把备份目录改回原路径；``keep`` 只清记录，备份留在磁盘上
    由用户自行处理。目标位置已经存在时不覆盖，保留记录让用户判断。
    """
    entries = _load()
    target = next(
        (e for e in entries if str(e.get("id") or "") == entry_id), None
    )
    if target is None:
        return {"ok": False, "code": "missing"}
    if action not in ("restore", "keep"):
        return {"ok": False, "code": "invalid_action"}

    src = str(target.get("src") or "")
    aside = str(target.get("aside") or "")
    if action == "restore":
        if not src or not aside:
            return {"ok": False, "code": "invalid"}
        if os.path.lexists(src):
            return {"ok": False, "code": "src_exists"}
        if not os.path.isdir(aside):
            return {"ok": False, "code": "backup_missing"}
        try:
            os.rename(aside, src)
        except OSError as exc:
            applog.warn(
                "migration journal restore failed"
                f" | reason={type(exc).__name__}"
            )
            return {"ok": False, "code": "os"}

    finish_switch(entry_id)
    return {"ok": True, "code": "ok", "src": src, "aside": aside}
