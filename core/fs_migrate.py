"""目录迁移 + 工具创建的目录链接登记。

迁移把一个占空间的目录整体搬到另一个位置，访问路径变成一个目录链接
（``mklink /J``）继续可用。步骤固定为：

1. 把 ``source`` 复制到 ``dest``（逐文件，回报进度）。
2. 校验：两边文件数、每个文件的字节数与修改时间一致（全量 hash 会把
   读取量翻倍，这里用「快校验」，符合迁移场景）。
3. 把原目录改名让位：``source`` → ``source + 备份后缀``。
4. 在 ``source`` 处建 junction 指向 ``dest``。建失败则把让位目录改回来。
5. 把这条链接登记进 ``links.json``（app_data_dir 下），由侧边栏
   [目录迁移] 面板统一管理（打开存放位置 / 还原 / 删除链接 / 删备份）。

目录联接（junction）在 Windows 上创建不需要管理员权限；符号链接
（``mklink /D``）才需要。迁移给访问路径建的是 junction；访问路径里已有的
链接在复制阶段按原样重建，建不出链接时按内容复制或记为跳过。
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from . import fs_delete
from .fs_delete import is_drive_root, normalize_abs

# 下面这些名字的实现已拆到 migrate_fs / migrate_links / migrate_history,
# 这里再导入一遍，旧的 core.fs_migrate.xxx 引用路径继续可用。
from .migrate_fs import (
    MigrateError,
    _LONG_AT,
    _LONG_PREFIX,
    _copy_file,
    _copy_one,
    _copy_workers,
    _drop_path,
    _fs_path,
    _is_link,
    _is_reparse,
    _link_target,
    _path_key,
    _rebuild_link,
    _rmtree,
    _rmtree_retry,
    _same_path,
    _size_of,
    _unique_path,
    _volume_fs,
    _walk_error,
    _walk_files,
    _walk_stats,
    _walk_total,
)

from .migrate_links import (
    _REGISTRY_LOCK,
    _registry_path,
    delete_junction,
    get_link_record,
    import_link,
    link_kind,
    link_records,
    link_target_abs,
    load_links,
    register_link,
    remove_link_record,
    save_links,
    scan_junctions,
    set_link_old_target,
)

from .migrate_history import (
    _HISTORY_LOCK,
    _HISTORY_OPS,
    _MAX_HISTORY,
    _history_path,
    _normalize_history_entry,
    _normalize_link_rows,
    _save_history,
    clear_history,
    load_history,
    record_history,
)

from .migrate_locks import holder_names
from .migrate_watch import ChangeWatcher

# ---- 迁移一致性 --------------------------------------------------------

# 等目录安静时的轮询间隔。
_SETTLE_POLL_S = 0.1

# 一致性相关的默认值。API 层从设置里读出后经 ``options`` 传进来，这里只兜底。
_DEFAULT_MIGRATE_OPTIONS = {
    "write_policy": "wait",
    "check_locks": True,
    "converge_rounds": 3,
    "quiet_seconds": 2.0,
    "settle_seconds": 3.0,
}

# 切换之后的收尾最多等多久：目录一直被写时不能无限等下去。
_SETTLE_MAX_WAIT_S = 30.0

# 提示里列几个文件、问占用时最多注册几个路径。
_MAX_HINT_FILES = 10
_MAX_LOCK_PATHS = 64


# 备份后缀：原目录让位时追加。默认中文，应用层经 set_backup_suffix 按界面语言设置。
_BACKUP_SUFFIX = " 原目录备份"
_active_backup_suffix = _BACKUP_SUFFIX


def set_backup_suffix(suffix: str) -> None:
    """设置备份目录后缀（按界面语言）。语言切换时调用。"""
    global _active_backup_suffix
    _active_backup_suffix = (suffix or "").strip() or _BACKUP_SUFFIX


def unique_backup_path(source: str) -> str:
    """为让位生成唯一备份路径（``source + 备份后缀``，冲突则加序号）。"""
    return _unique_path(normalize_abs(source).rstrip("\\/") + _active_backup_suffix)


# 旧迁移后缀：换了存放位置后，追加给原来那份数据。默认中文，
# 应用层经 set_old_target_suffix 按界面语言设置。
_OLD_TARGET_SUFFIX = " 旧迁移"
_active_old_target_suffix = _OLD_TARGET_SUFFIX


def set_old_target_suffix(suffix: str) -> None:
    """设置旧迁移目录后缀（按界面语言）。语言切换时调用。"""
    global _active_old_target_suffix
    _active_old_target_suffix = (suffix or "").strip() or _OLD_TARGET_SUFFIX


def unique_old_target_path(target: str) -> str:
    """为旧迁移目录生成唯一路径（``target + 旧迁移后缀``，冲突则加序号）。"""
    return _unique_path(normalize_abs(target).rstrip("\\/") + _active_old_target_suffix)


def validate_migrate(source: str, dest: str) -> dict:
    """迁移前置校验，返回 ``{ok, code, source, dest, aside}``。

    ``code``：``ok`` | ``invalid_source`` | ``not_dir`` | ``source_is_link`` |
    ``drive_root`` | ``dest_exists`` | ``invalid_dest`` | ``inside_each``
    | ``fs_mismatch`` | ``same_path`` | ``not_windows``。
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
    # 跨文件系统类型直接拒绝：FAT32/exFAT 存不下链接，时间戳精度也对不上
    src_fs = _volume_fs(src)
    dst_fs = _volume_fs(dst)
    if src_fs and dst_fs and src_fs != dst_fs:
        base["code"] = "fs_mismatch"
        return base
    if os.name != "nt":
        base["code"] = "not_windows"
        return base
    base["ok"] = True
    base["code"] = "ok"
    base["aside"] = unique_backup_path(src)
    return base


def validate_remigrate(link: str, dest: str) -> dict:
    """再次迁移的前置校验，返回 ``{ok, code, link, dest, old_target}``。

    ``code``：``ok`` | ``unknown_link`` | ``not_a_link`` | ``target_missing`` |
    ``old_target_pending`` | ``invalid_dest`` | ``same_path`` | ``dest_exists`` |
    ``drive_root`` | ``inside_each`` | ``fs_mismatch`` | ``not_windows``。
    """
    rec = get_link_record(link or "")
    base = {
        "ok": False,
        "code": "unknown_link",
        "link": "",
        "dest": "",
        "old_target": "",
    }
    if not rec:
        return base
    link_path = normalize_abs(str(rec.get("link") or ""))
    old_target = str(rec.get("target") or "")
    base["link"] = link_path
    base["old_target"] = old_target
    base["dest"] = normalize_abs(dest or "")
    if not _is_reparse(link_path):
        base["code"] = "not_a_link"
        return base
    if not old_target or not os.path.isdir(old_target):
        base["code"] = "target_missing"
        return base
    pending = str(rec.get("old_target") or "")
    if pending and os.path.isdir(pending):
        # 上一次再次迁移留下的旧目标还没删：登记里一条链接只记得住一个旧目标，
        # 再迁一次会把那个路径顶掉，数据还在盘上却没有删除入口了。先删再迁。
        base["code"] = "old_target_pending"
        return base
    dst = str(base["dest"])
    if not dst or not os.path.dirname(dst.rstrip("\\/")):
        base["code"] = "invalid_dest"
        return base
    if _same_path(old_target, dst) or _same_path(link_path, dst):
        base["code"] = "same_path"
        return base
    if is_drive_root(dst):
        base["code"] = "drive_root"
        return base
    if os.path.exists(dst):
        base["code"] = "dest_exists"
        return base
    # 互不嵌套：新目标不能建在旧目标里，也不能建在链接路径里（后者会写进旧目标）
    for other in (old_target, link_path):
        other_key = os.path.normcase(os.path.normpath(other)).rstrip("\\")
        dst_key = os.path.normcase(os.path.normpath(dst)).rstrip("\\")
        if dst_key.startswith(other_key + "\\") or other_key.startswith(dst_key + "\\"):
            base["code"] = "inside_each"
            return base
    # 跨文件系统类型直接拒绝：FAT32/exFAT 存不下链接，时间戳精度也对不上
    src_fs = _volume_fs(old_target)
    dst_fs = _volume_fs(dst)
    if src_fs and dst_fs and src_fs != dst_fs:
        base["code"] = "fs_mismatch"
        return base
    if os.name != "nt":
        base["code"] = "not_windows"
        return base
    base["ok"] = True
    base["code"] = "ok"
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


# 在途复制任务的上限：走到哪算哪，内存与文件总数无关。
_MAX_PENDING = 64

# 进度事件的最小间隔。每个文件推一次会让前端往返成为瓶颈：一次 1.74 毫秒，
# 一万五千个文件就是 26 秒。收尾那条强制发出，数字不会停在半路。
_PROGRESS_MIN_GAP_S = 0.12


def _copy_tree(
    src: str,
    dst: str,
    *,
    progress: Callable[[dict], None] | None,
    cancel: Callable[[], bool],
    refresh: bool = False,
    changed: Callable[[], bool] | None = None,
) -> dict:
    """逐文件复制，进度回调 ``{done_files, total_files, bytes_done, total_bytes, current}``。

    文件交给线程池并行复制，一份一个任务、在途数量封顶，内存与文件数
    无关。链接目录不进去复制内容，只在目标侧按原样重建链接，避免同一份
    数据在目标侧变成两份。链接文件先试重建，建不成但目标还在就按内容
    复制，只有目标也不存在（悬空链接）才跳过。返回 ``{links, copied, skipped}``，
    三项都是相对路径列表：重建为链接的、退化成按内容复制的、没复制的。

    ``refresh=True`` 时目标侧已存在、且大小与修改时间都一致的文件不再
    复制，只补上一次复制期间新增或改动过的。判定口径与 ``_verify`` 一致，
    补掉的正好是校验会报出来的那些差异。

    ``changed`` 是「作废模式」用的探子：每处理一个文件问一次，返回真就
    抛 ``blocked``，由调用方去查是谁在写、把复制产物清掉。
    """
    total_files, total_bytes = _walk_total(src, cancel=cancel)
    done_files = 0
    bytes_done = 0
    links: list[str] = []
    copied: list[str] = []
    skipped: list[str] = []
    pending: dict[Future, tuple[str, int]] = {}
    last_emit = 0.0
    # 在途文件已经写出去的字节。单个大文件要几十秒才写完，光靠文件完成时
    # 累加会让进度长时间定格，所以把在途的这份也并进上报值。复制线程累加，
    # 收任务的循环和上报都在主线程，两边用一把锁护住。
    inflight: dict[str, int] = {}
    lock = threading.Lock()

    def _emit(current: str, *, force: bool = False) -> None:
        nonlocal last_emit
        if not progress:
            return
        now = time.monotonic()
        with lock:
            if not force and now - last_emit < _PROGRESS_MIN_GAP_S:
                return
            last_emit = now
            done = done_files
            written = bytes_done + sum(inflight.values())
        progress(
            {
                "stage": "refresh" if refresh else "copy",
                "done": done,
                "total": total_files,
                "bytes_done": written,
                "bytes_total": total_bytes,
                "current": current,
            }
        )

    def _chunk_reporter(key: str) -> Callable[[int], None]:
        """造一个块级回调：把这一块并进这个任务的在途字节，再报一次进度。"""
        def _add(n: int) -> None:
            with lock:
                inflight[key] = inflight.get(key, 0) + n
            _emit(key)
        return _add

    def _count(key: str, size: int) -> None:
        nonlocal done_files, bytes_done
        with lock:
            inflight.pop(key, None)
        done_files += 1
        bytes_done += max(size, 0)
        _emit(key)

    def _reap() -> None:
        """收掉一个已完成的任务，把文件数与字节数累加进来。"""
        if not pending:
            return
        done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
        for fut in done:
            key, size = pending.pop(fut)
            try:
                fut.result()
            except BaseException as exc:  # noqa: BLE001 - 原样上抛给调用方
                if cancel and cancel():
                    raise MigrateError("cancelled") from exc
                raise
            _count(key, size)

    pool = ThreadPoolExecutor(
        max_workers=_copy_workers(dst), thread_name_prefix="migrate-copy"
    )
    try:
        _emit(src, force=True)
        for dirpath, dirnames, filenames in os.walk(src, onerror=_walk_error):
            if cancel and cancel():
                raise MigrateError("cancelled")
            if changed and changed():
                raise MigrateError("blocked")
            rel = os.path.relpath(dirpath, src)
            out_dir = dst if rel == "." else os.path.join(dst, rel)
            os.makedirs(_fs_path(out_dir), exist_ok=True)
            for name in filenames:
                if cancel and cancel():
                    raise MigrateError("cancelled")
                if changed and changed():
                    raise MigrateError("blocked")
                s = os.path.join(dirpath, name)
                d = os.path.join(out_dir, name)
                key = os.path.join(rel, name) if rel != "." else name
                if _is_link(s):
                    if refresh and os.path.lexists(_fs_path(d)):
                        # 补差异时链接已经建好，不重复建
                        links.append(key)
                    elif _rebuild_link(s, d, _link_target(s), is_dir=False):
                        links.append(key)
                    elif os.path.exists(_fs_path(s)):
                        # 建不出链接（没权限）：退回复制内容，数据照旧到得了目标侧
                        _copy_file(s, d, cancel=cancel)
                        copied.append(key)
                    else:
                        skipped.append(key)
                    _count(key, _size_of(s))
                    continue
                size = _size_of(s)
                if refresh and _up_to_date(s, d, size):
                    _count(key, size)
                    continue
                while len(pending) >= _MAX_PENDING:
                    _reap()
                with lock:
                    inflight[key] = 0
                pending[
                    pool.submit(
                        _copy_one, s, d, size, cancel=cancel, on_chunk=_chunk_reporter(key)
                    )
                ] = (key, size)
            keep: list[str] = []
            for name in dirnames:
                p = os.path.join(dirpath, name)
                if not _is_reparse(p):
                    keep.append(name)
                    continue
                key = os.path.join(rel, name) if rel != "." else name
                out = os.path.join(out_dir, name)
                if refresh and os.path.lexists(_fs_path(out)):
                    # 补差异时链接已经建好，不重复建
                    links.append(key)
                    continue
                target = _link_target(p)
                if target and _rebuild_link(p, out, target, is_dir=True):
                    links.append(key)
                else:
                    skipped.append(key)
            dirnames[:] = keep
        while pending:
            _reap()
    finally:
        # 在途的那几个不打断（每个都是系统调用一次拷完，最多百来毫秒），
        # 排队中的直接丢掉。
        pool.shutdown(wait=True, cancel_futures=True)
    if progress:
        progress(
            {
                "stage": "refresh" if refresh else "copy",
                "done": total_files,
                "total": total_files,
                "bytes_done": total_bytes,
                "bytes_total": total_bytes,
                "current": "",
            }
        )
    return {"links": links, "copied": copied, "skipped": skipped}


def _next_file(
    it: Iterator[tuple[str, int, float]],
) -> tuple[tuple[str, int, float] | None, str]:
    """取下一条比对记录，连同它的排序键（走完返回 ``(None, "")``）。"""
    item = next(it, None)
    if item is None:
        return None, ""
    return item, _path_key(item[0])


def _up_to_date(src: str, dst: str, size: int) -> bool:
    """目标侧这份文件是否已经和源一致（大小与修改时间）。

    判定口径与 ``_verify`` 一致：补差异放过的东西，校验也必须放过。
    """
    try:
        st = os.stat(_fs_path(dst))
        src_mtime = os.stat(_fs_path(src)).st_mtime
    except OSError:
        return False
    if st.st_size != size:
        return False
    return abs(st.st_mtime - src_mtime) <= 1e-6


def _verify(
    src: str,
    dst: str,
    *,
    cancel: Callable[[], bool],
    handled: dict | None = None,
) -> dict:
    """快校验：两边文件数、逐文件字节数与修改时间一致。

    全量 hash 会让磁盘读取量翻倍，迁移场景用「大小 + 修改时间」即可
    抓住绝大多数复制错误。两侧按同一顺序同时推进，边比边丢，内存与
    文件数无关。按链接处理的条目（``handled``）不参与大小与时间比对，
    只要求位置对得上：重建和退化成内容的必须在目标侧存在，没复制的
    必须不存在。返回 ``{ok, files, bytes, mismatch}``。
    """
    handled = handled or {}
    present = {str(k) for k in (handled.get("links") or [])} | {
        str(k) for k in (handled.get("copied") or [])
    }
    skipped = {str(k) for k in (handled.get("skipped") or [])}
    excluded = present | skipped
    mismatch: list[str] = []
    src_count = 0
    dst_count = 0
    src_bytes = 0
    src_it = _walk_files(src, cancel=cancel, skip=excluded)
    dst_it = _walk_files(dst, cancel=cancel, skip=excluded)
    s, s_key = _next_file(src_it)
    d, d_key = _next_file(dst_it)
    while s is not None or d is not None:
        if s is None:
            # 目标里多出来的不逐条报，文件数对不上会记一条 count
            dst_count += 1
            d, d_key = _next_file(dst_it)
        elif d is None or s_key < d_key:
            # 源里有、目标里没有：逐条报出来
            mismatch.append(s[0])
            src_count += 1
            src_bytes += s[1]
            s, s_key = _next_file(src_it)
        elif d_key < s_key:
            dst_count += 1
            d, d_key = _next_file(dst_it)
        else:
            src_count += 1
            src_bytes += s[1]
            dst_count += 1
            if d[1] != s[1] or abs(d[2] - s[2]) > 1e-6:
                mismatch.append(s[0])
            s, s_key = _next_file(src_it)
            d, d_key = _next_file(dst_it)
    if src_count != dst_count:
        mismatch.insert(0, "count")
    for key in sorted(present):
        if not os.path.lexists(_fs_path(os.path.join(dst, key))):
            mismatch.append("link:" + key)
    for key in sorted(skipped):
        if os.path.lexists(_fs_path(os.path.join(dst, key))):
            mismatch.append("extra:" + key)
    return {
        "ok": not mismatch,
        "files": src_count,
        "bytes": src_bytes,
        "mismatch": mismatch[:20],
    }


def _record_verify(out: dict | None, mode: str) -> None:
    """记录这次校验采用的比对方式，供调用方写入迁移历史。

    ``stats``：只比对目录数、文件数与总字节。
    ``full``：逐文件比对大小与修改时间。
    """
    if out is None:
        return
    out["mode"] = mode


def _clamp_int(value, low: int, high: int, default: int) -> int:
    """把设置里的数字收拢到可用范围，取不到就用默认值。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def _clamp_float(value, low: float, high: float, default: float) -> float:
    """同上，针对秒数这类小数。"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def _migrate_options(options: dict | None) -> dict:
    """合并一致性相关的运行参数，越界值收拢。

    ``write_policy`` 的 ``wait`` 是边写边补齐、最后收敛；``abort`` 是一检测到
    写入就放弃本次迁移（删掉已复制的内容，源目录一个字节不动）。
    """
    raw = dict(options or {})
    policy = str(raw.get("write_policy") or "").strip().lower()
    if policy not in ("wait", "abort"):
        policy = str(_DEFAULT_MIGRATE_OPTIONS["write_policy"])
    return {
        "write_policy": policy,
        "check_locks": bool(
            raw.get("check_locks", _DEFAULT_MIGRATE_OPTIONS["check_locks"])
        ),
        "converge_rounds": _clamp_int(raw.get("converge_rounds"), 1, 10, 3),
        "quiet_seconds": _clamp_float(raw.get("quiet_seconds"), 1.0, 10.0, 2.0),
        "settle_seconds": _clamp_float(raw.get("settle_seconds"), 1.0, 10.0, 3.0),
    }


def _under(path: str, root: str) -> bool:
    """``path`` 是否在 ``root`` 之下（含 root 自身）。"""
    p = os.path.normcase(os.path.normpath(path)).rstrip("\\/")
    r = os.path.normcase(os.path.normpath(root)).rstrip("\\/")
    return p == r or p.startswith(r + "\\")


def _prune_rels(rels) -> list[str]:
    """去掉被同一批里父目录覆盖的条目，避免同一棵子树被同步多遍。

    监控常常一次带上很多同一目录下的文件，如果其中还有那整个目录，逐个补
    就成了重复劳动。
    """
    mapping: dict[str, str] = {}
    for raw in rels or ():
        text = str(raw or "").strip().strip("\\/")
        if not text or text == ".":
            continue
        mapping.setdefault(os.path.normcase(os.path.normpath(text)), text)
    out: list[str] = []
    for key, raw in mapping.items():
        parent = os.path.dirname(key)
        covered = False
        while parent:
            if parent in mapping:
                covered = True
                break
            parent = os.path.dirname(parent)
        if not covered:
            out.append(raw)
    return sorted(out)


def _holder_names_for(paths) -> list[str]:
    """拿一批路径去问占用，返回程序名；查不到返回空表。"""
    items = [str(p) for p in (paths or ()) if str(p or "").strip()]
    if not items:
        return []
    return holder_names(items[:_MAX_LOCK_PATHS])


def _changed_files(src: str, watcher: "ChangeWatcher | None") -> tuple[list[str], int]:
    """监控记下的变化路径（前几条）与总条数。"""
    if watcher is None or not watcher.started:
        return [], 0
    rels = sorted(watcher.snapshot())
    return rels[:_MAX_HINT_FILES], len(rels)


def _changed_procs(src: str, rels) -> list[str]:
    """变化过的文件正被哪些程序占着。"""
    return _holder_names_for([os.path.join(src, rel) for rel in (rels or [])])


def _sync_changes(
    src: str,
    dst: str,
    rels,
    *,
    cancel: Callable[[], bool],
    delete_missing: bool = True,
) -> int:
    """按变更清单把源侧的变化同步到目标侧，返回处理的条目数。

    清单来自目录监控，可能不全（内核记录区满时只报「有变化」不给名字），
    所以它只是加快收敛的手段，不是正确性来源；漏掉的差异由随后的全树对比
    兜住。``delete_missing`` 为假时只补不删，切换之后的收尾用它，免得把新
    位置里刚写进去的东西当成「源侧已经没有了」删掉。
    """
    done = 0
    for rel in _prune_rels(rels):
        if cancel and cancel():
            raise MigrateError("cancelled")
        s = os.path.join(src, rel)
        d = os.path.join(dst, rel)
        if not _under(d, dst):
            continue
        if not os.path.lexists(_fs_path(s)):
            if delete_missing:
                _drop_path(d)
                done += 1
            continue
        if _is_reparse(s):
            # 链接：目标侧先弄干净，再按原样重建
            if os.path.lexists(_fs_path(d)):
                _drop_path(d)
            is_dir = os.path.isdir(_fs_path(s))
            if _rebuild_link(s, d, _link_target(s), is_dir=is_dir):
                done += 1
                continue
            if not os.path.exists(_fs_path(s)):
                # 目标已经不在了的链接，建不出来就没什么可补
                continue
            if is_dir:
                _copy_tree(s, d, progress=None, cancel=cancel, refresh=True)
            else:
                _copy_one(s, d, _size_of(s), cancel=cancel)
            done += 1
            continue
        if os.path.isdir(_fs_path(s)):
            _copy_tree(s, d, progress=None, cancel=cancel, refresh=True)
            done += 1
            continue
        size = _size_of(s)
        if not _up_to_date(s, d, size):
            _copy_one(s, d, size, cancel=cancel)
        done += 1
    return done


def _wait_quiet(
    watcher: "ChangeWatcher | None", seconds: float, cancel: Callable[[], bool]
) -> bool:
    """``seconds`` 秒内没有新的写事件就返回真；监控不可用当作真。"""
    if watcher is None or not watcher.started:
        return True
    last = watcher.event_count()
    end = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < end:
        if cancel and cancel():
            raise MigrateError("cancelled")
        time.sleep(_SETTLE_POLL_S)
        if watcher.event_count() != last:
            return False
    return True


def _settle(
    src: str,
    dst: str,
    watcher: "ChangeWatcher | None",
    *,
    quiet_s: float,
    cancel: Callable[[], bool],
    progress: Callable[[dict], None] | None = None,
    delete_missing: bool = True,
    max_wait_s: float | None = None,
) -> dict:
    """等到连续 ``quiet_s`` 秒没有新的写事件，期间按清单把变化同步过去。

    返回 ``{synced, quiet}``。``quiet`` 为假表示等到 ``max_wait_s`` 还没静下来
    （目录一直被写），这时不能当成已经收敛。
    """
    if watcher is None or not watcher.started:
        return {"synced": 0, "quiet": True}
    synced = 0
    # 进来之前就记下的变化（复制完之后、走到这里之前发生的）也要补：
    # 切换后的收尾只跑这一次，漏掉就没人再补了。
    rels = watcher.take()
    if rels:
        synced += _sync_changes(
            src, dst, rels, cancel=cancel, delete_missing=delete_missing
        )
        if progress:
            progress(dict({"stage": "settle", "synced": synced}))
    last = watcher.event_count()
    idle_from = time.monotonic()
    deadline = None if not max_wait_s else time.monotonic() + max(0.0, max_wait_s)
    while True:
        if cancel and cancel():
            raise MigrateError("cancelled")
        time.sleep(_SETTLE_POLL_S)
        now = watcher.event_count()
        if now != last:
            last = now
            idle_from = time.monotonic()
            rels = watcher.take()
            if rels:
                synced += _sync_changes(
                    src, dst, rels, cancel=cancel, delete_missing=delete_missing
                )
            if progress:
                progress(dict({"stage": "settle", "synced": synced}))
            continue
        if time.monotonic() - idle_from >= quiet_s:
            return {"synced": synced, "quiet": True}
        if deadline is not None and time.monotonic() >= deadline:
            return {"synced": synced, "quiet": False}


def _converge(
    src: str,
    dst: str,
    watcher: "ChangeWatcher | None",
    *,
    opts: dict,
    cancel: Callable[[], bool],
    progress: Callable[[dict], None] | None,
    ask: Callable[[str, dict], str] | None,
    handled: dict | None,
) -> dict:
    """收敛：等安静、全树对比、确认对比期间没人写，三件事都过了才算数。

    对比通过但对比期间监控有记录，说明结论已经过时，要重来一轮。连续
    ``converge_rounds`` 轮都不成，找用户：等目录不再被写（点[继续]重新计数），
    或者放弃这次迁移（点[取消]，已复制的内容删掉，访问路径不动）。
    """
    handled = dict(handled or {})
    limit = max(1, int(opts["converge_rounds"]))
    quiet_s = float(opts["quiet_seconds"])
    round_no = 0
    while True:
        round_no += 1
        # 单轮里等安静最多花 quiet 的数倍时间，否则目录一直被写就永远轮不到对比
        _settle(
            src,
            dst,
            watcher,
            quiet_s=quiet_s,
            cancel=cancel,
            progress=progress,
            max_wait_s=quiet_s * 5,
        )
        baseline = watcher.event_count() if (watcher and watcher.started) else 0
        if progress:
            progress(dict({"stage": "verify", "current": ""}))
        ver = _verify(src, dst, cancel=cancel, handled=handled)
        if not ver.get("ok"):
            handled = _copy_tree(
                src, dst, progress=progress, cancel=cancel, refresh=True
            )
            ver = _verify(src, dst, cancel=cancel, handled=handled)
        settled = bool(ver.get("ok")) and (
            watcher is None or not watcher.started or watcher.event_count() == baseline
        )
        if settled:
            out = dict(ver)
            out["links"] = list(handled.get("links") or [])
            out["copied"] = list(handled.get("copied") or [])
            out["skipped"] = list(handled.get("skipped") or [])
            return out
        if round_no < limit:
            continue
        if ask is None:
            # 没有交互通道（命令行、测试）：不能无限等，按失败处理
            raise MigrateError("busy")
        rels, total = _changed_files(src, watcher)
        action = ask(
            "loop",
            {
                "round": round_no,
                "rounds": limit,
                "root": src,
                "files": rels,
                "filesTotal": total,
                "procs": _changed_procs(src, rels),
            },
        )
        if action == "cancel":
            raise MigrateError("aborted")
        round_no = 0


def _confirm_switch(
    src: str,
    watcher: "ChangeWatcher | None",
    *,
    settle_s: float,
    cancel: Callable[[], bool],
    ask: Callable[[str, dict], str] | None,
    watch_failed: bool,
) -> bool:
    """切换链接之前的最后确认，返回真表示可以切。

    全程没人碰过目录就直接放行。碰过就请用户关掉那些程序；用户点[继续]
    之后再看 ``settle_s`` 秒，这期间还有写入就再问一次（这次带[强制切换]）。
    切换那一刻到底有没有人在写，工具自己看不到，这一段交给用户保证。
    """
    if ask is None:
        return True
    if (
        not watch_failed
        and watcher is not None
        and watcher.started
        and not watcher.has_events()
    ):
        return True
    attempt = 0
    while True:
        attempt += 1
        rels, total = _changed_files(src, watcher)
        action = ask(
            "switch",
            {
                "attempt": attempt,
                "root": src,
                "files": rels,
                "filesTotal": total,
                "procs": _changed_procs(src, rels),
                "watchFailed": bool(watch_failed),
            },
        )
        if action == "cancel":
            raise MigrateError("aborted")
        if action == "force":
            return True
        if _wait_quiet(watcher, settle_s, cancel):
            return True


def _refuse_no_watch(
    *,
    ask: Callable[[str, dict], str] | None,
    cancel: Callable[[], bool],
) -> None:
    """变动监测起不来时，开工前把话说完，然后中止这一趟。

    后面的校验、补齐、切换确认全靠这份监测判断目录有没有在变；起不来就
    没办法知道目录有没有被写。界面上这一问只有[取消迁移]一个按钮，用户
    看完去重启，再以管理员身份跑一次。
    """
    if ask is not None and not (cancel and cancel()):
        ask("nowatch", {})
    raise MigrateError("aborted")


def _precheck_locks(
    src: str,
    *,
    ask: Callable[[str, dict], str] | None,
    cancel: Callable[[], bool],
) -> None:
    """开始之前查一遍目录里的文件有没有被别的程序占着。

    查到就提示用户去关，用户也可以选择直接开始（后面还有监控和切换前
    确认兜着）。查不动、没查到都不打扰。
    """
    if ask is None or cancel and cancel():
        return
    try:
        with os.scandir(_fs_path(src)) as it:
            paths = [entry.path for entry in it][:_MAX_LOCK_PATHS]
    except OSError:
        return
    procs = _holder_names_for(paths)
    if not procs:
        return
    action = ask("locks", {"procs": procs})
    if action == "cancel":
        raise MigrateError("aborted")


def _blocked_code(src: str, watcher: "ChangeWatcher | None") -> str:
    """作废模式中止时带上是谁在写，交给上层拼提示。"""
    rels, _total = _changed_files(src, watcher)
    return "blocked:" + ",".join(_changed_procs(src, rels))


def _restore_link_to(target: str, link_path: str) -> bool:
    """把原路径的联接指回旧目标；只在改指新目标失败时用，成功返回真。"""
    try:
        _create_junction(target, link_path)
    except Exception:  # noqa: BLE001 - 兜底用，什么错都当作没接上
        return False
    return True


def _newest_mtime(root: str, *, cancel: Callable[[], bool]) -> float:
    """树里最新的修改时间；用来判断校验完之后有没有人往旧目标里写过东西。"""
    newest = 0.0
    for _rel, _size, mtime in _walk_files(root, cancel=cancel):
        if mtime > newest:
            newest = mtime
    return newest


def _strip_long_prefix(path: str) -> str:
    r"""去掉扩展长度前缀（``\\?\`` 与 ``\\?\UNC\``），拿回普通绝对路径。

    Windows 上读目录联接的目标（``os.readlink``）拿到的是带前缀的形式，
    跟登记里的普通路径直接比是比不上的。
    """
    if not path or not path.startswith(_LONG_PREFIX):
        return path
    rest = path[len(_LONG_PREFIX):]
    if rest.upper().startswith("UNC\\"):
        return "\\\\" + rest[4:]
    return rest


def _remove_link(path: str) -> None:
    """把链接本身删掉：文件链接用 unlink，目录链接（含交叉点）用 rmdir。

    两条路删的都是链接本身，不会动它指向的东西。
    """
    try:
        os.unlink(_fs_path(path))
    except OSError:
        os.rmdir(_fs_path(path))


def _retarget_inner_links(
    dst: str, old_target: str, rels: list[str], *, cancel: Callable[[], bool]
) -> int:
    """把新目标里指向旧目标内部的链接改指新目标里对应的位置，返回改了几条。

    只在再次迁移里用。复制是按原样重建链接的，绝对目标还停在旧目标上，
    旧目标一删这些链接就断了。指向旧目标外部的链接、以及相对目标的链接
    不动：相对目标在新位置解析仍然指对地方。

    ``rels`` 是链接在新目标里的相对路径（复制阶段收集的）。
    """
    changed = 0
    old_key = os.path.normcase(os.path.normpath(old_target)).rstrip("\\/")
    for rel in rels:
        if cancel():
            break
        link = os.path.join(dst, rel)
        target = _strip_long_prefix(_link_target(link))
        if not target or not os.path.isabs(target):
            continue
        target_key = os.path.normcase(os.path.normpath(target)).rstrip("\\/")
        if target_key != old_key and not target_key.startswith(old_key + os.sep):
            continue
        try:
            tail = os.path.relpath(target, old_target)
        except ValueError:
            continue
        if tail.startswith(".."):
            continue
        new_target = dst if tail == "." else os.path.join(dst, tail)
        is_dir = os.path.isdir(target)
        try:
            _remove_link(link)
        except OSError:
            continue
        if not _rebuild_link(link, link, new_target, is_dir=is_dir):
            # 改指失败就把链接按原样建回去，宁可它指旧目标，也别彻底消失
            _rebuild_link(link, link, target, is_dir=is_dir)
            continue
        changed += 1
    return changed


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
    backup_action: str = "keep",
    ask: Callable[[str, dict], str] | None = None,
    options: dict | None = None,
    verify_out: dict | None = None,
) -> dict:
    """执行一次完整迁移，返回 ``{ok, source, dest, aside, files, bytes, ...}``。

    ``verify_out``：传入一个字典时，把这次校验采用的比对方式写入其中
    （``stats`` 表示只比对目录数、文件数与总字节，``full`` 表示逐文件
    比对大小与修改时间），供调用方记入迁移历史；不传则不收集。

    顺序固定：查占用 → 挂监控 → 复制 → 校验 → 切换前确认 → 原目录让位 →
    建 junction → 切换后收尾。让位或建 junction 失败都会尽量回滚（把让位
    目录改回原名），复制或校验不过则清理已复制的 ``dest``。

    监控必须在复制之前挂上，否则复制期间的变化不会进记录，后面那条「零事件
    就跳过全树对比」的捷径就不成立。监控起不来、或者中途漏过事件（内核记录
    区满）时，一律退回全树对比，并且切换前一定找用户确认。

    ``ask`` 是「后端问用户」的回调：``kind`` 为 ``locks`` / ``loop`` /
    ``switch`` / ``blocked``，返回 ``continue`` / ``cancel`` / ``force``。
    缺省时不打扰用户：连补几轮不齐按失败处理，也不跳过切换前确认。

    ``backup_action`` 决定迁移成功后如何处置让位出来的备份目录：``keep``
    留在原处，``recycle`` 移入回收站，``purge`` 彻底删除。删除或移入回收站
    失败不阻断迁移，只置 ``backup_cleaned=False``；收尾没静下来或监控漏过
    事件时不删，退回 ``keep``，实际执行的动作写在返回值的 ``backup_action``
    里。
    """
    cancel = cancel or (lambda: False)
    opt = _migrate_options(options)
    pre = validate_migrate(source, dest)
    if not pre.get("ok"):
        raise MigrateError(str(pre.get("code") or "invalid_source"))
    src = str(pre["source"])
    dst = str(pre["dest"])
    aside = str(pre["aside"])

    def _emit(stage: str, **kw) -> None:
        if progress:
            progress(dict({"stage": stage}, **kw))

    watcher = ChangeWatcher()
    watch_ok = watcher.start(src)
    settle = {"synced": 0, "quiet": True}
    try:
        if not watch_ok:
            _refuse_no_watch(ask=ask, cancel=cancel)
        if opt["check_locks"]:
            _precheck_locks(src, ask=ask, cancel=cancel)

        _emit("copy", current="")
        copied = _copy_tree(
            src,
            dst,
            progress=progress,
            cancel=cancel,
            changed=(watcher.has_events if opt["write_policy"] == "abort" else None),
        )

        _emit("verify", current="")
        ver = None
        if watch_ok and not watcher.overflowed and not watcher.has_events():
            # 复制全程没有记录到写入，也没漏过监控事件：目标侧就是一份完整
            # 快照，走三项统计（目录数、文件数、总字节）就够，不必全树对比。
            skip = {
                str(k)
                for key in ("links", "copied", "skipped")
                for k in (copied.get(key) or [])
            }
            src_stats = _walk_stats(src, cancel=cancel, skip=skip)
            dst_stats = _walk_stats(dst, cancel=cancel, skip=skip)
            if src_stats == dst_stats:
                ver = {
                    "ok": True,
                    "files": src_stats[1],
                    "bytes": src_stats[2],
                    "mismatch": [],
                }
                _record_verify(verify_out, "stats")
        if ver is None:
            ver = _converge(
                src,
                dst,
                watcher,
                opts=opt,
                cancel=cancel,
                progress=progress,
                ask=ask,
                handled=copied,
            )
            _record_verify(verify_out, "full")
        if not ver.get("ok"):
            # 校验不过：删掉复制产物，报错，不动原目录
            _rmtree(dst)
            raise MigrateError(
                "verify" + ("".join(":" + m for m in ver.get("mismatch") or [])[:200])
            )

        _confirm_switch(
            src,
            watcher,
            settle_s=opt["settle_seconds"],
            cancel=cancel,
            ask=ask,
            watch_failed=not watch_ok,
        )

        _emit("setaside", current="")
        try:
            os.rename(src, aside)
        except OSError as exc:
            _rmtree(dst)
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
            _rmtree(dst)
            raise

        # 切换后收尾：切换那一瞬通过已经打开的句柄写进旧目录的内容，新位置里
        # 没有。这里只补不删，也不再受理取消：迁移已经切完了，回滚反而是错的。
        _emit("settle", current="")
        try:
            settle = _settle(
                aside,
                dst,
                watcher,
                quiet_s=opt["quiet_seconds"],
                cancel=lambda: False,
                progress=progress,
                delete_missing=False,
                max_wait_s=_SETTLE_MAX_WAIT_S,
            )
        except Exception:  # noqa: BLE001 - 收尾失败不能让已完成的切换回滚
            settle = {"synced": 0, "quiet": False}
    except MigrateError as exc:
        # 迁移失败（含取消/校验不过）：清掉复制产物；若已让位则改回原名
        _rmtree(dst)
        try:
            if os.path.exists(aside) and not os.path.exists(src):
                os.rename(aside, src)
        except OSError:
            pass
        if str(exc.message) == "blocked":
            # 作废模式：先告诉用户是谁在写，再把机器码换成带名字的那种
            if ask is not None:
                rels, total = _changed_files(src, watcher)
                ask(
                    "blocked",
                    {
                        "root": src,
                        "files": rels,
                        "filesTotal": total,
                        "procs": _changed_procs(src, rels),
                    },
                )
            raise MigrateError(_blocked_code(src, watcher)) from None
        raise
    except Exception as exc:  # noqa: BLE001 - 任何异常都当作迁移失败
        _rmtree(dst)
        try:
            if os.path.exists(aside) and not os.path.exists(src):
                os.rename(aside, src)
        except OSError:
            pass
        raise MigrateError(f"fail:{exc}") from exc
    finally:
        watcher.stop()

    action = backup_action if backup_action in ("keep", "recycle", "purge") else "keep"
    # 收尾没静下来，或监控漏过事件（缓冲区满时系统只说有变化、不说是哪些
    # 文件），备份里都可能有没补到新位置的写入，这时不能删
    can_clean = (
        bool(settle.get("quiet"))
        and not (watcher is not None and watcher.overflowed)
        and os.path.isdir(aside)
        and not _is_reparse(aside)
    )
    if action != "keep" and not can_clean:
        action = "keep"

    backup_cleaned = False
    if action != "keep":
        try:
            if action == "recycle":
                fs_delete.delete_to_recycle(aside)
            else:
                fs_delete.delete_path(aside, permanent=True)
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
        "links": len(copied.get("links") or []),
        "skipped": len(copied.get("skipped") or []),
        "settled": bool(settle.get("quiet")),
        "settled_files": int(settle.get("synced") or 0),
        "backup_action": action,
        "backup_cleaned": backup_cleaned,
        "backup_permanent": bool(backup_cleaned and action == "purge"),
    }


def remigrate_link(
    link: str,
    dest: str,
    *,
    progress: Callable[[dict], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    old_target_action: str = "keep",
    ask: Callable[[str, dict], str] | None = None,
    options: dict | None = None,
    verify_out: dict | None = None,
) -> dict:
    """再次迁移：把这链接的数据搬到新目标，链接改指新目标。

    ``verify_out``：含义与首次迁移相同，收集这次校验采用的比对方式供历史使用。

    顺序与首次迁移一样：查占用 → 挂监控 → 复制 → 校验（含跳过全树对比的
    统计检查）→ 切换前确认 → 删链接、建指向新目标的链接 → 切换后收尾。
    切换失败就把链接建回旧目标，新目标上的复制产物清掉，旧目标一个字节
    都没动。

    监控挂在旧目标上：搬运期间从原路径写进来的东西落在旧目标里，靠它记下来
    补进新目标。路径与首次迁移时不同，但被监控的是同一个目录对象，句柄有效。

    ``old_target_action``：``keep`` 把旧迁移改名成「<名称> 旧迁移」留在原处，
    路径写进登记供面板删除；``recycle`` 移入回收站；``purge`` 彻底删除。
    删除或改名失败不阻断，只把对应字段置空。收尾没静下来时也不彻底删，
    退回 ``keep``。

    返回 ``{ok, link, target, old_target, old_target_left, old_target_action,
    old_target_removed, files, bytes, links, skipped, settled}``。
    """
    cancel = cancel or (lambda: False)
    opt = _migrate_options(options)
    pre = validate_remigrate(link or "", dest or "")
    if not pre.get("ok"):
        raise MigrateError(str(pre.get("code") or "unknown_link"))
    link_path = str(pre["link"])
    old_target = str(pre["old_target"])
    dst = str(pre["dest"])
    action = (
        old_target_action
        if old_target_action in ("keep", "recycle", "purge")
        else "keep"
    )
    backup = str((get_link_record(link_path) or {}).get("backup") or "")

    def _emit(stage: str, **kw) -> None:
        if progress:
            progress(dict({"stage": stage}, **kw))

    watcher = ChangeWatcher()
    watch_ok = watcher.start(old_target)
    settle = {"synced": 0, "quiet": True}
    try:
        if not watch_ok:
            _refuse_no_watch(ask=ask, cancel=cancel)
        if opt["check_locks"]:
            _precheck_locks(old_target, ask=ask, cancel=cancel)

        _emit("copy", current="")
        copied = _copy_tree(
            old_target,
            dst,
            progress=progress,
            cancel=cancel,
            changed=(watcher.has_events if opt["write_policy"] == "abort" else None),
        )
        _emit("verify", current="")
        ver = None
        if watch_ok and not watcher.overflowed and not watcher.has_events():
            # 同上：全程没有记录到写入、也没漏过监控事件，跑三项统计就够
            skip = {
                str(k)
                for key in ("links", "copied", "skipped")
                for k in (copied.get(key) or [])
            }
            src_stats = _walk_stats(old_target, cancel=cancel, skip=skip)
            dst_stats = _walk_stats(dst, cancel=cancel, skip=skip)
            if src_stats == dst_stats:
                ver = {
                    "ok": True,
                    "files": src_stats[1],
                    "bytes": src_stats[2],
                    "mismatch": [],
                }
                _record_verify(verify_out, "stats")
        if ver is None:
            ver = _converge(
                old_target,
                dst,
                watcher,
                opts=opt,
                cancel=cancel,
                progress=progress,
                ask=ask,
                handled=copied,
            )
            _record_verify(verify_out, "full")
        if not ver.get("ok"):
            _rmtree(dst)
            raise MigrateError(
                "verify" + ("".join(":" + m for m in ver.get("mismatch") or [])[:200])
            )
        # 复制是按原样重建链接的：新目标里指向旧目标内部的链接要改指新目标里
        # 对应的位置，否则旧目标一删它们就断了。改链接不动数据，放在校验之后、
        # 切换之前做。
        retargeted = _retarget_inner_links(
            dst,
            old_target,
            list(ver.get("links") or copied.get("links") or []),
            cancel=cancel,
        )

        _confirm_switch(
            old_target,
            watcher,
            settle_s=opt["settle_seconds"],
            cancel=cancel,
            ask=ask,
            watch_failed=not watch_ok,
        )

        verify_at = time.time()
        _emit("switch", current="")
        try:
            os.rmdir(link_path)
        except OSError as exc:
            _rmtree(dst)
            raise MigrateError(f"unlink:{exc}") from exc
        try:
            _create_junction(dst, link_path)
        except Exception:  # noqa: BLE001 - 建链失败要把原路径指回旧目标
            # 旧目标里的数据还没动过，先把原路径指回去；指不回去时原路径就真的
            # 不在了，得让用户拿到 linklost，而不是一条看不出所以然的失败。
            if not _restore_link_to(old_target, link_path):
                raise MigrateError(f"linklost:{link_path}") from None
            _rmtree(dst)
            raise

        # 切换后收尾：切换那一瞬通过已经打开的句柄写进旧目标的内容，新位置里
        # 没有。只补不删，也不再受理取消：迁移已经切完了，回滚反而是错的。
        _emit("settle", current="")
        try:
            settle = _settle(
                old_target,
                dst,
                watcher,
                quiet_s=opt["quiet_seconds"],
                cancel=lambda: False,
                progress=progress,
                delete_missing=False,
                max_wait_s=_SETTLE_MAX_WAIT_S,
            )
        except Exception:  # noqa: BLE001 - 收尾失败不能让已完成的切换回滚
            settle = {"synced": 0, "quiet": False}
    except MigrateError as exc:
        _rmtree(dst)
        if str(exc.message) == "blocked":
            if ask is not None:
                rels, total = _changed_files(old_target, watcher)
                ask(
                    "blocked",
                    {
                        "root": old_target,
                        "files": rels,
                        "filesTotal": total,
                        "procs": _changed_procs(old_target, rels),
                    },
                )
            raise MigrateError(_blocked_code(old_target, watcher)) from None
        raise
    except Exception as exc:  # noqa: BLE001 - 任何异常都当作再次迁移失败
        _rmtree(dst)
        raise MigrateError(f"fail:{exc}") from exc
    finally:
        watcher.stop()

    register_link(
        link_path,
        dst,
        backup,
        files=int(ver.get("files") or 0),
        bytes_=int(ver.get("bytes") or 0),
    )

    if action != "keep" and cancel():
        # 删除可能拖很久，进破坏性步骤前再查一次取消；真开始删之后不再受理取消，
        # 免得删到一半停下，剩下那半是什么状态谁也说不清。
        action = "keep"
    if action == "purge":
        # 校验完之后又有人往原路径里写过东西（那部分落在旧目标上，新目标里没有），
        # 这时不能彻底删，改成改名保留，删除旧迁移的入口留在面板上。
        # 这趟遍历不再看取消位：取消位刚在上面查过，途中新来的取消等下次再说。
        if not settle.get("quiet"):
            # 收尾时目录还在被写：进去的东西未必都到了新目标，不能彻底删
            action = "keep"
        elif _newest_mtime(old_target, cancel=lambda: False) > verify_at:
            action = "keep"

    left = ""
    removed = False
    if action == "keep":
        left = unique_old_target_path(old_target)
        try:
            os.rename(old_target, left)
        except OSError:
            # 改不了名（占用、只读）就留在原路径，登记照记，删除入口照旧可用
            left = old_target
        set_link_old_target(link_path, left)
    else:
        try:
            if action == "recycle":
                fs_delete.delete_to_recycle(old_target)
            else:
                fs_delete.delete_path(old_target, permanent=True)
            removed = True
        except fs_delete.DeleteError:
            removed = False
        # 删掉了才清登记：删失败时路径留着，面板上的[删除]入口还在
        if removed:
            set_link_old_target(link_path, "")

    return {
        "ok": True,
        "link": link_path,
        "target": dst,
        "old_target": old_target,
        "old_target_left": left,
        "old_target_action": action,
        "old_target_removed": removed,
        "files": int(ver.get("files") or 0),
        "bytes": int(ver.get("bytes") or 0),
        "links": len(ver.get("links") or copied.get("links") or []),
        "links_retargeted": retargeted,
        "skipped": len(ver.get("skipped") or copied.get("skipped") or []),
        "settled": bool(settle.get("quiet")),
        "settled_files": int(settle.get("synced") or 0),
    }


def restore_link(
    link: str,
    *,
    progress: Callable[[dict], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    permanent: bool = False,
    verify_out: dict | None = None,
) -> dict:
    """还原：存放位置的内容复制回访问路径 → 删链接 → 备份移入回收站。

    ``verify_out``：传入一个字典时写入这次校验采用的比对方式（``full``），
    供调用方记入迁移历史；不传则不收集。

    先把存放位置的内容复制到访问路径的父目录下的临时目录并校验，再删 junction、同卷
    改名，失败可回滚（重新建 junction）。成功后数据已完整回到访问路径，
    所以把备份与存放位置上的数据一并移出原位，不留重复副本；``permanent=True`` 时备份
    彻底删除（不进回收站），目标仍进回收站。``progress`` 接收
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
        copied = _copy_tree(target, temp, progress=progress, cancel=cancel)
        _emit("verify", current="")
        ver = _verify(target, temp, cancel=cancel, handled=copied)
        if not ver.get("ok"):
            _rmtree(temp)
            raise MigrateError("verify")
        _record_verify(verify_out, "full")
        os.rmdir(link)
        _emit("restore", current="")
        try:
            os.rename(temp, link)
        except OSError:
            _rmtree(temp)
            try:
                _create_junction(target, link)
            except MigrateError:
                pass
            raise
    except MigrateError:
        _rmtree(temp)
        raise
    except Exception as exc:  # noqa: BLE001
        _rmtree(temp)
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
        "links": len(copied.get("links") or []),
        "skipped": len(copied.get("skipped") or []),
        "recycled_backup": recycled_backup,
        "backup_permanent": bool(permanent and recycled_backup),
        "recycled_target": recycled_target,
    }
