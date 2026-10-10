"""工具创建的目录链接登记表 links.json：登记、查询、删除。"""

from __future__ import annotations

import json
import os
import threading
import time
from .fs_delete import normalize_abs
from .migrate_fs import MigrateError, _fs_path, _is_reparse, _link_target, _same_path
from .models import clean_link_target, reparse_kind


def _registry_path() -> str:
    from . import store

    return os.path.join(store.app_data_dir(), "links.json")


def load_links() -> list[dict]:
    """读全部登记；文件缺失/损坏返回空表。"""
    try:
        with open(_registry_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict) and d.get("link")]
    except (OSError, ValueError):
        pass
    return []


def save_links(links: list[dict]) -> None:
    """原子写登记文件（先写临时文件再 replace）。"""
    try:
        path = _registry_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(links, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def link_kind(path: str) -> str:
    """路径的重解析点类型：``junction`` / ``symlink`` / ``reparse``；不是链接返回空串。

    只看标签，不看目标读不读得出来。导入登记时用它挡住 ``mklink /D`` 建的
    目录符号链接：那种链接的删除和迁移走的是另一套处理。
    """
    try:
        st = os.lstat(_fs_path(path))
    except OSError:
        return ""
    return reparse_kind(int(getattr(st, "st_reparse_tag", 0) or 0))


def link_target_abs(link: str) -> str:
    """目录联接指向的绝对路径（已剥掉内核前缀）；读不出来返回空串。

    目录联接的目标在磁盘上始终是绝对路径，不用再拼链接自身的位置。
    """
    return clean_link_target(_link_target(link))


def _pair_reversed(a: str, b: str) -> bool:
    """两个路径填反了：b 才是目录联接，且它指向 a。"""
    if not b or link_kind(b) != "junction":
        return False
    real = link_target_abs(b)
    return bool(real) and _same_path(real, a)


def import_link(link: str, target: str = "") -> dict:
    """把一条已经在别处建好的目录联接收进登记表。

    只收 ``mklink /J`` 建的目录联接。导入的记录不是本工具迁移留下的，没有
    备份目录，``backup`` 写空串，面板据此显示「无备份」并且不给 [还原]。

    Args:
        link: 目录链接自身的路径，也就是界面上的 [访问路径]。
        target: 目录链接指向的存放位置，界面上的 [存放位置]。留空表示由工具
            自己从联接上读；填了就必须和读出来的一致，否则拒绝导入，避免
            把不相干的两个目录登记成一对。

    Returns:
        新登记的记录，与 :func:`link_records` 的单条同形。

    Raises:
        MigrateError: 路径不存在、不是目录联接、两个路径填反了、目标读不出
            来、存放位置不是存在的目录、填的 [存放位置] 和读出来的对不上，或者
            这条已经在登记里。
    """
    link = normalize_abs(link)
    want = normalize_abs(target) if target else ""
    if not link or not os.path.lexists(_fs_path(link)):
        raise MigrateError("import_missing")
    if link_kind(link) != "junction":
        # 填反了的样子：这个框里放的其实是目标，另一个框里才是联接。
        if want and _pair_reversed(link, want):
            raise MigrateError("import_reversed")
        raise MigrateError("import_not_junction")
    real = link_target_abs(link)
    if not real or not os.path.isdir(_fs_path(real)):
        raise MigrateError("import_target_missing")
    if _is_reparse(real):
        raise MigrateError("import_target_is_link")
    if want and not _same_path(want, real):
        if _pair_reversed(link, want):
            raise MigrateError("import_reversed")
        raise MigrateError("import_pair_mismatch")
    if get_link_record(link) is not None:
        raise MigrateError("import_exists")
    register_link(link, real, "")
    return get_link_record(link) or {}


def scan_junctions(directory: str) -> list[dict]:
    """扫一个目录下第一层的目录链接，列出能导入管理的那些。

    只看第一层：工具建的联接都在原目录的父目录下，逐层递归会把整块盘的
    链接都翻出来，用户还得自己挑。

    Args:
        directory: 要扫的目录。

    Returns:
        每项含 ``link``（目录链接的路径）、``target``（指向的存放位置，读不出来
        时为空串）、``managed``（是否已在登记里）。

    Raises:
        MigrateError: 目录不存在或读不了。
    """
    directory = normalize_abs(directory)
    if not directory or not os.path.isdir(_fs_path(directory)):
        raise MigrateError("import_dir_missing")
    try:
        with os.scandir(_fs_path(directory)) as it:
            names = [e.name for e in it]
    except OSError as exc:
        raise MigrateError(f"scan:{exc}") from exc
    out: list[dict] = []
    for name in names:
        path = os.path.join(directory, name)
        if link_kind(path) != "junction":
            continue
        out.append(
            {
                "link": path,
                "target": link_target_abs(path),
                "managed": get_link_record(path) is not None,
            }
        )
    out.sort(key=lambda r: r["link"].lower())
    return out


def register_link(
    link: str, target: str, backup: str, *, files: int = 0, bytes_: int = 0
) -> None:
    """登记一条工具创建的目录链接（迁移成功时调用）。同路径重复则更新。"""
    now = time.time()
    with _REGISTRY_LOCK:
        links = load_links()
        for rec in links:
            if _same_path(str(rec.get("link") or ""), link):
                rec.update(
                    {
                        "target": target,
                        "backup": backup,
                        "files": int(files),
                        "bytes": int(bytes_),
                        "created_at": float(rec.get("created_at") or now),
                    }
                )
                save_links(links)
                return
        links.append(
            {
                "link": link,
                "target": target,
                "backup": backup,
                "files": int(files),
                "bytes": int(bytes_),
                "created_at": now,
            }
        )
        save_links(links)


def set_link_old_target(link: str, old_target: str) -> None:
    """记下或清空这条链接留下的旧目标路径。

    再次迁移选 [改名保留] 时记下改名后的路径，面板据此给 [删除]；
    旧迁移处理完就清空。传空串则移除这个字段。
    """
    with _REGISTRY_LOCK:
        links = load_links()
        for rec in links:
            if _same_path(str(rec.get("link") or ""), link):
                if old_target:
                    rec["old_target"] = old_target
                else:
                    rec.pop("old_target", None)
                save_links(links)
                return


def get_link_record(link: str) -> dict | None:
    """按链接路径取一条登记；找不到返回 None。"""
    for rec in load_links():
        if _same_path(str(rec.get("link") or ""), link):
            return dict(rec)
    return None


def remove_link_record(link: str) -> None:
    """从登记里移除一条（不碰磁盘）。"""
    with _REGISTRY_LOCK:
        links = load_links()
        kept = [
            r for r in links
            if not _same_path(str(r.get("link") or ""), link)
        ]
        if len(kept) != len(links):
            save_links(kept)


def link_records() -> list[dict]:
    """登记的全部链接，附当前文件系统状态（供面板渲染）。

    每条含 ``state``：``ok``（链接与目标都在）| ``target_missing``
    （目标丢失）| ``link_missing``（链接已不存在/不是链接）。

    ``has_backup`` 区分「本来就没有备份」与「备份被删了」：导入的联接不是
    工具迁移过来的，从没有过备份，``backup`` 是空串，面板据此显示「无备份」
    并且不给 [还原]。
    """
    out: list[tuple[float, dict]] = []
    for rec in load_links():
        link = str(rec.get("link") or "")
        target = str(rec.get("target") or "")
        backup = str(rec.get("backup") or "")
        old_target = str(rec.get("old_target") or "")
        link_exists = bool(link) and os.path.lexists(link)
        is_link = link_exists and _is_reparse(link)
        target_exists = bool(target) and os.path.isdir(target)
        backup_exists = bool(backup) and os.path.isdir(backup)
        old_target_exists = bool(old_target) and os.path.isdir(old_target)
        if not link_exists or not is_link:
            state = "link_missing"
        elif not target_exists:
            state = "target_missing"
        else:
            state = "ok"
        out.append(
            (
                float(rec.get("created_at") or 0),
                {
                    "link": link,
                    "target": target,
                    "backup": backup,
                    "old_target": old_target,
                    "files": int(rec.get("files") or 0),
                    "bytes": int(rec.get("bytes") or 0),
                    "created_at": int(rec.get("created_at") or 0),
                    "is_link": is_link,
                    "link_exists": link_exists,
                    "target_exists": target_exists,
                    "backup_exists": backup_exists,
                    "has_backup": bool(backup),
                    "old_target_exists": old_target_exists,
                    "state": state,
                },
            )
        )
    out.sort(key=lambda x: x[0], reverse=True)
    return [e for _created, e in out]


def delete_junction(link: str) -> str:
    """删除目录联接本身（数据留在目标）。仅当路径当前确为重解析点才删。"""
    link = normalize_abs(link)
    if not link or not _is_reparse(link):
        raise MigrateError("not_a_link")
    try:
        os.rmdir(link)
    except OSError as exc:
        raise MigrateError(f"unlink:{exc}") from exc
    return link


_REGISTRY_LOCK = threading.Lock()
