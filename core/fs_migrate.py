"""目录迁移 + 工具创建的目录链接登记。

迁移把一个占空间的目录整体搬到另一个位置，原路径变成一个目录联接
（``mklink /J``）继续可用。步骤固定为：

1. 把 ``source`` 复制到 ``dest``（逐文件，回报进度）。
2. 校验：两边文件数、每个文件的字节数与修改时间一致（全量 hash 会把
   读取量翻倍，这里用「快校验」，符合迁移场景）。
3. 把原目录改名让位：``source`` → ``source + 备份后缀``。
4. 在 ``source`` 处建 junction 指向 ``dest``。建失败则把让位目录改回来。
5. 把这条链接登记进 ``links.json``（app_data_dir 下），由侧边栏
   「目录链接」面板统一管理（打开目标 / 还原 / 删除链接 / 删备份）。

目录联接（junction）在 Windows 上创建不需要管理员权限；符号链接
（``mklink /D``）才需要。本模块只做 junction。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable

from . import fs_delete
from .fs_delete import is_drive_root, normalize_abs

# 迁移/还原历史记录上限。超过则按时间戳裁掉最旧的，避免长期使用后文件失控膨胀。
_MAX_HISTORY = 200

# 备份后缀：原目录让位时追加。默认中文，应用层经 set_backup_suffix 按界面语言设置。
_BACKUP_SUFFIX = " 原目录备份"
_active_backup_suffix = _BACKUP_SUFFIX


def set_backup_suffix(suffix: str) -> None:
    """设置备份目录后缀（按界面语言）。语言切换时调用。"""
    global _active_backup_suffix
    _active_backup_suffix = (suffix or "").strip() or _BACKUP_SUFFIX


class MigrateError(Exception):
    """可预期的迁移失败（message 为机器键，调用方负责 i18n）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _is_reparse(path: str) -> bool:
    """路径是否为重解析点（junction / 符号链接 / 其它）。"""
    try:
        st = os.lstat(path)
        return int(getattr(st, "st_reparse_tag", 0) or 0) != 0
    except OSError:
        return False


def _same_path(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(
            os.path.normpath(b)
        )
    except OSError:
        return False


def _unique_path(base: str) -> str:
    """生成不冲突的路径（``base`` 已存在则加序号）。"""
    candidate = base
    i = 1
    while os.path.exists(candidate):
        candidate = f"{base} ({i})"
        i += 1
    return candidate


def unique_backup_path(source: str) -> str:
    """为让位生成唯一备份路径（``source + 备份后缀``，冲突则加序号）。"""
    return _unique_path(normalize_abs(source).rstrip("\\/") + _active_backup_suffix)


def validate_migrate(source: str, dest: str) -> dict:
    """迁移前置校验，返回 ``{ok, code, source, dest, aside}``。

    ``code``：``ok`` | ``invalid_source`` | ``not_dir`` | ``source_is_link`` |
    ``drive_root`` | ``dest_exists`` | ``invalid_dest`` | ``inside_each``
    | ``same_path`` | ``not_windows``。
    """
    src = normalize_abs(source)
    dst = normalize_abs(dest)
    base = {
        "ok": False,
        "code": "invalid_source",
        "source": src,
        "dest": dst,
        "aside": "",
    }
    if not src or not os.path.lexists(src):
        base["code"] = "invalid_source"
        return base
    if not os.path.isdir(src):
        base["code"] = "not_dir"
        return base
    if _is_reparse(src):
        base["code"] = "source_is_link"
        return base
    if is_drive_root(src):
        base["code"] = "drive_root"
        return base
    if not dst:
        base["code"] = "invalid_dest"
        return base
    if _same_path(src, dst):
        base["code"] = "same_path"
        return base
    if os.path.exists(dst):
        base["code"] = "dest_exists"
        return base
    parent = os.path.dirname(dst.rstrip("\\/"))
    if not parent:
        base["code"] = "invalid_dest"
        return base
    # 互不嵌套：dest 不能建在 source 里，source 不能是 dest 的子目录
    src_key = os.path.normcase(os.path.normpath(src)).rstrip("\\")
    dst_key = os.path.normcase(os.path.normpath(dst)).rstrip("\\")
    if dst_key.startswith(src_key + "\\") or src_key.startswith(dst_key + "\\"):
        base["code"] = "inside_each"
        return base
    if os.name != "nt":
        base["code"] = "not_windows"
        return base
    base["ok"] = True
    base["code"] = "ok"
    base["aside"] = unique_backup_path(src)
    return base


def free_space_ok(source: str, dest: str) -> bool:
    """目标盘剩余空间是否够装下 source（够=真）。

    迁移前的容量预检。会扫一遍 source 统计字节数，只应在真正开始迁移前
    调用一次；不能放进受防抖反复调用的 ``validate_migrate``（大源目录每次
    全扫会卡住输入）。
    """
    from .scanner import query_free_size

    _files, total_bytes = _walk_total(source, cancel=lambda: False)
    return total_bytes <= query_free_size(dest)


def _walk_total(root: str, *, cancel: Callable[[], bool]) -> tuple[int, int]:
    """扫一遍源目录，统计文件数与总字节（用于进度百分比）。"""
    files = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        if cancel and cancel():
            raise MigrateError("cancelled")
        for name in filenames:
            p = os.path.join(dirpath, name)
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
            files += 1
        # 不跟随符号链接/重解析点子目录，避免死循环与重复计算
        dirnames[:] = [
            d for d in dirnames
            if not _is_reparse(os.path.join(dirpath, d))
        ]
    return files, total


def _copy_file(
    src: str,
    dst: str,
    *,
    cancel: Callable[[], bool],
    buf_size: int = 1024 * 1024,
) -> None:
    """逐 chunk 复制单个文件，每 chunk 检查取消；末尾用 ``copystat`` 保留元数据。

    代替 ``shutil.copy2``：``copy2`` 一次系统调用把整个文件拷完，没法在
    大文件中途打断。分块读写后取消（含空目录、大文件）都能真正中断。
    """
    try:
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            while True:
                if cancel and cancel():
                    raise MigrateError("cancelled")
                chunk = fin.read(buf_size)
                if not chunk:
                    break
                fout.write(chunk)
        shutil.copystat(src, dst)
    except OSError as exc:
        raise MigrateError(f"copy:{os.path.basename(src)}:{exc}") from exc


def _copy_tree(
    src: str,
    dst: str,
    *,
    progress: Callable[[dict], None] | None,
    cancel: Callable[[], bool],
) -> None:
    """逐文件复制，进度回调 ``{done_files, total_files, bytes_done, total_bytes, current}``。"""
    total_files, total_bytes = _walk_total(src, cancel=cancel)
    done_files = 0
    bytes_done = 0

    def _emit(current: str) -> None:
        if progress:
            progress(
                {
                    "stage": "copy",
                    "done": done_files,
                    "total": total_files,
                    "bytes_done": bytes_done,
                    "bytes_total": total_bytes,
                    "current": current,
                }
            )

    _emit(src)
    for dirpath, dirnames, filenames in os.walk(src):
        if cancel and cancel():
            raise MigrateError("cancelled")
        rel = os.path.relpath(dirpath, src)
        out_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(out_dir, exist_ok=True)
        for name in filenames:
            if cancel and cancel():
                raise MigrateError("cancelled")
            s = os.path.join(dirpath, name)
            d = os.path.join(out_dir, name)
            _copy_file(s, d, cancel=cancel)
            try:
                bytes_done += os.path.getsize(s)
            except OSError:
                pass
            done_files += 1
            _emit(os.path.join(rel, name) if rel != "." else name)
        dirnames[:] = [
            d for d in dirnames
            if not _is_reparse(os.path.join(dirpath, d))
        ]
    if progress:
        progress(
            {
                "stage": "copy",
                "done": total_files,
                "total": total_files,
                "bytes_done": total_bytes,
                "bytes_total": total_bytes,
                "current": "",
            }
        )


def _verify(
    src: str, dst: str, *, cancel: Callable[[], bool]
) -> dict:
    """快校验：两边文件数、逐文件字节数与修改时间一致。

    全量 hash 会让磁盘读取量翻倍，迁移场景用「大小 + 修改时间」即可
    抓住绝大多数复制错误。返回 ``{ok, files, bytes, mismatch}``。
    """
    src_files: dict[str, tuple[int, float]] = {}
    src_bytes = 0
    for dirpath, _dirnames, filenames in os.walk(src):
        if cancel and cancel():
            raise MigrateError("cancelled")
        rel = os.path.relpath(dirpath, src)
        for name in filenames:
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
                src_files[os.path.join(rel, name) if rel != "." else name] = (
                    st.st_size, st.st_mtime,
                )
                src_bytes += st.st_size
            except OSError:
                pass
    mismatch: list[str] = []
    dst_files: dict[str, tuple[int, float]] = {}
    dst_bytes = 0
    for dirpath, _dirnames, filenames in os.walk(dst):
        if cancel and cancel():
            raise MigrateError("cancelled")
        rel = os.path.relpath(dirpath, dst)
        for name in filenames:
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
                key = os.path.join(rel, name) if rel != "." else name
                dst_files[key] = (st.st_size, st.st_mtime)
                dst_bytes += st.st_size
            except OSError:
                pass
    if len(src_files) != len(dst_files):
        mismatch.append("count")
    for key, (size, mtime) in src_files.items():
        other = dst_files.get(key)
        if other is None:
            mismatch.append(key)
            continue
        if other[0] != size or abs(other[1] - mtime) > 1e-6:
            mismatch.append(key)
    return {
        "ok": not mismatch and len(src_files) == len(dst_files),
        "files": len(src_files),
        "bytes": src_bytes,
        "mismatch": mismatch[:20],
    }


def _create_junction(dest: str, link: str) -> None:
    """在 ``link`` 处建指向 ``dest`` 的目录联接（junction）。

    Windows 上用 ``_winapi.CreateJunction``（无管理员要求）；非 Windows
    抛错，调用方按不支持处理。
    """
    if os.name != "nt":
        raise MigrateError("not_windows")
    try:
        import _winapi

        _winapi.CreateJunction(dest, link)
    except OSError as exc:
        raise MigrateError(f"junction:{exc}") from exc


def migrate_directory(
    source: str,
    dest: str,
    *,
    progress: Callable[[dict], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    clean_backup: bool = False,
) -> dict:
    """执行一次完整迁移，返回 ``{ok, source, dest, aside, files, bytes, backup_cleaned, ...}``。

    顺序固定：复制 → 校验 → 原目录让位 → 建 junction。让位或建 junction
    失败都会尽量回滚（把让位目录改回原名），复制中途失败则清理已复制的
    ``dest``。

    ``clean_backup`` 为真时，迁移成功后把让位出来的备份目录移入回收站
    （失败不阻断，仅置 ``backup_cleaned=False``）。默认保留，作为安全网。
    """
    cancel = cancel or (lambda: False)
    pre = validate_migrate(source, dest)
    if not pre.get("ok"):
        raise MigrateError(str(pre.get("code") or "invalid_source"))
    src = str(pre["source"])
    dst = str(pre["dest"])
    aside = str(pre["aside"])

    def _emit(stage: str, **kw) -> None:
        if progress:
            progress(dict({"stage": stage}, **kw))

    try:
        _emit("copy", current="")
        _copy_tree(src, dst, progress=progress, cancel=cancel)

        _emit("verify", current="")
        ver = _verify(src, dst, cancel=cancel)
        if not ver.get("ok"):
            # 校验不过：删掉复制产物，报错，不动原目录
            shutil.rmtree(dst, ignore_errors=True)
            raise MigrateError(
                "verify" + ("".join(":" + m for m in ver.get("mismatch") or [])[:200])
            )

        _emit("setaside", current="")
        try:
            os.rename(src, aside)
        except OSError as exc:
            shutil.rmtree(dst, ignore_errors=True)
            raise MigrateError(f"rename:{exc}") from exc

        try:
            _emit("junction", current="")
            _create_junction(dst, src)
        except MigrateError:
            # 建 junction 失败：把让位目录改回原名，再清掉复制产物
            try:
                os.rename(aside, src)
            except OSError:
                pass
            shutil.rmtree(dst, ignore_errors=True)
            raise
    except MigrateError:
        # 迁移失败（含取消/校验不过）：清掉复制产物；若已让位则改回原名
        shutil.rmtree(dst, ignore_errors=True)
        try:
            if os.path.exists(aside) and not os.path.exists(src):
                os.rename(aside, src)
        except OSError:
            pass
        raise
    except Exception as exc:  # noqa: BLE001 - 任何异常都当作迁移失败
        shutil.rmtree(dst, ignore_errors=True)
        try:
            if os.path.exists(aside) and not os.path.exists(src):
                os.rename(aside, src)
        except OSError:
            pass
        raise MigrateError(f"fail:{exc}") from exc

    backup_cleaned = False
    if clean_backup and os.path.isdir(aside) and not _is_reparse(aside):
        try:
            fs_delete.delete_to_recycle(aside)
            backup_cleaned = True
        except fs_delete.DeleteError:
            backup_cleaned = False

    return {
        "ok": True,
        "source": src,
        "dest": dst,
        "aside": aside,
        "files": int(ver.get("files") or 0),
        "bytes": int(ver.get("bytes") or 0),
        "backup_cleaned": backup_cleaned,
    }


# ---- 链接登记（links.json）----
# 只有登记过的链接才允许被「目录链接」面板管理，避免接口变成任意操作入口。

_REGISTRY_LOCK = threading.Lock()


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
    """
    out: list[tuple[float, dict]] = []
    for rec in load_links():
        link = str(rec.get("link") or "")
        target = str(rec.get("target") or "")
        backup = str(rec.get("backup") or "")
        link_exists = bool(link) and os.path.lexists(link)
        is_link = link_exists and _is_reparse(link)
        target_exists = bool(target) and os.path.isdir(target)
        backup_exists = bool(backup) and os.path.isdir(backup)
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
                    "files": int(rec.get("files") or 0),
                    "bytes": int(rec.get("bytes") or 0),
                    "created_at": int(rec.get("created_at") or 0),
                    "is_link": is_link,
                    "link_exists": link_exists,
                    "target_exists": target_exists,
                    "backup_exists": backup_exists,
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


def restore_link(
    link: str,
    *,
    progress: Callable[[dict], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    permanent: bool = False,
) -> dict:
    """还原：目标内容复制回原路径 → 删链接 → 备份移入回收站。

    先把目标复制到原路径父目录下的临时目录并校验，再删 junction、同卷
    改名，失败可回滚（重新建 junction）。成功后目标数据已完整回到原路径，
    所以把备份与目标一并移出原位，不留重复副本；``permanent=True`` 时备份
    永久删除（不进回收站），目标仍进回收站。``progress`` 接收
    ``{stage, done, total, bytes_done, bytes_total, current}``。返回
    ``{ok, link, target, recycled_backup, backup_permanent, recycled_target}``；
    ``recycled_backup`` 只表示备份已从原位置移走，去向看 ``backup_permanent``。
    """
    cancel = cancel or (lambda: False)
    rec = get_link_record(link or "")
    if not rec:
        raise MigrateError("unknown_link")
    link = normalize_abs(str(rec["link"]))
    target = str(rec.get("target") or "")
    backup = str(rec.get("backup") or "")
    if not _is_reparse(link):
        raise MigrateError("not_a_link")
    if not target or not os.path.isdir(target):
        raise MigrateError("target_missing")

    def _emit(stage: str, **kw) -> None:
        if progress:
            progress(dict({"stage": stage}, **kw))

    parent = os.path.dirname(link.rstrip("\\/"))
    temp = _unique_path(
        os.path.join(parent, ".restore-" + os.path.basename(link.rstrip("\\/")))
    )
    try:
        _copy_tree(target, temp, progress=progress, cancel=cancel)
        _emit("verify", current="")
        ver = _verify(target, temp, cancel=cancel)
        if not ver.get("ok"):
            shutil.rmtree(temp, ignore_errors=True)
            raise MigrateError("verify")
        os.rmdir(link)
        _emit("restore", current="")
        try:
            os.rename(temp, link)
        except OSError:
            shutil.rmtree(temp, ignore_errors=True)
            try:
                _create_junction(target, link)
            except MigrateError:
                pass
            raise
    except MigrateError:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(temp, ignore_errors=True)
        raise MigrateError(f"restore:{exc}") from exc

    remove_link_record(link)
    recycled_backup = ""
    if backup and os.path.isdir(backup) and not _is_reparse(backup):
        try:
            fs_delete.delete_path(backup, permanent=permanent)
            recycled_backup = backup
        except fs_delete.DeleteError:
            pass
    # 目标已完整复制回原路径并校验通过，数据不再需要；也把它移入回收站，
    # 避免还原后目标残留一份完整副本（否则 2GB 级目录会永远留一份重复）。
    recycled_target = ""
    if target and os.path.isdir(target) and not _is_reparse(target):
        try:
            fs_delete.delete_to_recycle(target)
            recycled_target = target
        except fs_delete.DeleteError:
            pass
    return {
        "ok": True,
        "link": link,
        "target": target,
        "recycled_backup": recycled_backup,
        "backup_permanent": bool(permanent and recycled_backup),
        "recycled_target": recycled_target,
    }


# ---- 迁移/还原历史（migration_history.json）----
# 持久化每次 migrate / restore 的最终结果。即便链接已还原、记录被移除，
# 历史依旧可查，便于用户回顾「上次哪条没成功」。

_HISTORY_LOCK = threading.Lock()


def _history_path() -> str:
    from . import store

    return os.path.join(store.app_data_dir(), "migration_history.json")


def _normalize_history_entry(raw: dict) -> dict | None:
    """清洗一条历史记录；字段缺失或类型不对则丢弃。"""
    if not isinstance(raw, dict):
        return None
    op = str(raw.get("op") or "")
    if op not in ("migrate", "restore", "delbackup"):
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
        "backup_cleaned": bool(raw.get("backup_cleaned") or False),
        "backup_permanent": bool(raw.get("backup_permanent") or False),
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
