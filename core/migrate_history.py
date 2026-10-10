"""迁移与还原历史记录：追加、读取、裁剪、清空。"""

from __future__ import annotations

import json
import os
import threading
import uuid


def _history_path() -> str:
    from . import store

    return os.path.join(store.app_data_dir(), "migration_history.json")


def _normalize_link_rows(raw: object) -> list[dict]:
    """清洗反查命中的链接行；不是字典的条目直接丢掉，字段缺失算空串。"""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        out.append({
            "link": str(it.get("link") or ""),
            "target": str(it.get("target") or ""),
            "kind": str(it.get("kind") or ""),
            "source": str(it.get("source") or ""),
        })
    return out


def _normalize_verify(raw: object) -> str:
    """清洗校验结论；只认可用的两种比对方式，其余一律视为未校验。"""
    mode = str(raw or "")
    return mode if mode in ("stats", "full") else ""


def _normalize_history_entry(raw: dict) -> dict | None:
    """清洗一条历史记录；字段缺失或类型不对则丢弃。"""
    if not isinstance(raw, dict):
        return None
    op = str(raw.get("op") or "")
    if op not in _HISTORY_OPS:
        return None
    status = str(raw.get("status") or "")
    if status not in ("success", "failed", "cancelled"):
        return None
    out = {
        "id": str(raw.get("id") or uuid.uuid4().hex),
        "ts": float(raw.get("ts") or 0.0),
        "op": op,
        "source": str(raw.get("source") or ""),
        "target": str(raw.get("target") or ""),
        "backup": str(raw.get("backup") or ""),
        "files": int(raw.get("files") or 0),
        "bytes": int(raw.get("bytes") or 0),
        "status": status,
        "error": str(raw.get("error") or ""),
        "code": str(raw.get("code") or ""),
        "links": int(raw.get("links") or 0),
        "skipped": int(raw.get("skipped") or 0),
        # 校验结论：记录这次校验采用的比对方式，stats 表示只比对了目录数、
        # 文件数与总字节，full 表示逐文件比对大小与修改时间；空串表示本次
        # 未执行校验，或校验未通过
        "verify": _normalize_verify(raw.get("verify")),
        "backup_cleaned": bool(raw.get("backup_cleaned") or False),
        "backup_permanent": bool(raw.get("backup_permanent") or False),
        # 删除旧目标（op="deloldtarget"）：permanent 表示没进回收站，历史行要写出来
        "old_target_permanent": bool(raw.get("old_target_permanent") or False),
        # 反查链接（op="linkfind"）：mode 是来源类型，roots/snapshot 是查了
        # 哪些盘或哪份快照，hits 与 unresolved 是当次结果的全部链接行。
        "mode": str(raw.get("mode") or ""),
        "roots": [str(r) for r in (raw.get("roots") or []) if r],
        "snapshot": str(raw.get("snapshot") or ""),
        "hits": _normalize_link_rows(raw.get("hits")),
        "hit_total": int(raw.get("hit_total") or 0),
        "unresolved": _normalize_link_rows(raw.get("unresolved")),
        "unresolved_total": int(raw.get("unresolved_total") or 0),
        "elapsed_s": float(raw.get("elapsed_s") or 0.0),
    }
    return out


def load_history() -> list[dict]:
    """读历史记录；文件缺失/损坏返回空表。"""
    try:
        with open(_history_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            out: list[dict] = []
            for d in data:
                norm = _normalize_history_entry(d)
                if norm is not None:
                    out.append(norm)
            return out
    except (OSError, ValueError):
        pass
    return []


def _save_history(items: list[dict]) -> None:
    """原子写历史（先临时文件再 replace）。"""
    try:
        path = _history_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def record_history(entry: dict) -> dict | None:
    """追加一条历史；裁剪到 ``_MAX_HISTORY`` 条；返回规范化后的记录。

    调用方传入原始字段（缺失或类型不对会被清洗层修正）。原子写失败时
    静默返回 None，不影响主流程。
    """
    norm = _normalize_history_entry(entry)
    if norm is None:
        return None
    with _HISTORY_LOCK:
        items = load_history()
        items.append(norm)
        # 按时间戳倒序保留最新的 N 条
        items.sort(key=lambda d: float(d.get("ts") or 0), reverse=True)
        if len(items) > _MAX_HISTORY:
            items = items[:_MAX_HISTORY]
        _save_history(items)
    return norm


def clear_history() -> int:
    """清空历史，返回清掉的条数（读不到文件返回 0）。"""
    with _HISTORY_LOCK:
        items = load_history()
        _save_history([])
    return len(items)


# 迁移/还原历史记录上限。超过则按时间戳裁掉最旧的，避免长期使用后文件失控膨胀。
_MAX_HISTORY = 200


# 历史里允许出现的操作类型。linkfind 是反查链接：它改不了磁盘上的任何
# 东西，但用户要能回头看到自己查过什么、查到了什么，所以同表存。
# deloldtarget 是再次迁移后删除旧目标上的数据，同表存才看得到删过什么。
_HISTORY_OPS = ("migrate", "restore", "delbackup", "linkfind", "deloldtarget")


_HISTORY_LOCK = threading.Lock()
