"""快照压缩：``.db`` ↔ ``.dbz``（zip）。

``.dbz`` 是一个 zip 包，内含：

- ``meta.json``：快照元信息（含备注），列举时只读这一份，不必解压整库；
- ``data.db``：原始 SQLite 快照。

设计取舍：扫描始终先写出完整 ``.db``（保证可立刻校验），开启压缩后
再压成 ``.dbz`` 并删掉 ``.db``。对比时把 ``.dbz`` 解到**进程内临时文件**
（仅内存侧登记，不落应用数据目录的 cache/）；进程退出即丢弃。
列表 / 删除 / 选择路径始终指向用户可见的 ``.db`` 或 ``.dbz`` 文件。
"""

from __future__ import annotations

import atexit
import json
import os
import tempfile
import threading
import zipfile

from .i18n import t
from .models import SnapshotMeta
from .snapshot import SnapshotError, read_meta, write_meta_note

# zip 内固定成员名。
_META_MEMBER = "meta.json"
_DATA_MEMBER = "data.db"

# 压缩后的扩展名（小写比较）。
DBZ_SUFFIX = ".dbz"

# 进程内解压登记：abspath(dbz) → (temp_db_path, mtime, size)
# 不写 %LOCALAPPDATA%\...\cache；软件关闭后系统清临时文件。
_session_lock = threading.Lock()
_session_db: dict[str, tuple[str, float, int]] = {}
_atexit_registered = False


class CompressError(Exception):
    """压缩 / 解压相关错误。"""


def is_compressed_path(path: str) -> bool:
    """路径是否指向压缩快照（``.dbz``）。"""
    return path.lower().endswith(DBZ_SUFFIX)


def is_snapshot_filename(name: str) -> bool:
    """文件名是否像一份快照（``.db`` 或 ``.dbz``）。"""
    lower = name.lower()
    return lower.endswith(".db") or lower.endswith(DBZ_SUFFIX)


def _meta_to_json(meta: SnapshotMeta) -> str:
    """把 :class:`SnapshotMeta` 序列成 zip 内的 ``meta.json`` 文本。"""
    return json.dumps(
        {
            "root": meta.root,
            "scanned_at": meta.scanned_at,
            "total_size": meta.total_size,
            "file_count": meta.file_count,
            "dir_count": meta.dir_count,
            "skipped": meta.skipped,
            "format_version": meta.format_version,
            "note": (meta.note or "").strip(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _meta_from_json(text: str) -> SnapshotMeta:
    """从 ``meta.json`` 文本还原 :class:`SnapshotMeta`。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SnapshotError(
            t("压缩快照的 meta.json 损坏", "Compressed snapshot meta.json is corrupt")
        ) from exc
    try:
        return SnapshotMeta(
            root=data["root"],
            scanned_at=float(data["scanned_at"]),
            total_size=int(data.get("total_size", 0)),
            file_count=int(data.get("file_count", 0)),
            dir_count=int(data.get("dir_count", 0)),
            skipped=list(data.get("skipped") or []),
            format_version=int(data.get("format_version", 0)),
            note=str(data.get("note") or "").strip(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotError(
            t("压缩快照的 meta.json 字段不完整",
              "Compressed snapshot meta.json is incomplete")
        ) from exc


def compress_db(db_path: str, meta: SnapshotMeta | None = None) -> str:
    """把一份 ``.db`` 压成同目录下的 ``.dbz``，成功后删除 ``.db``。

    Args:
        db_path: 未压缩快照路径（必须以 ``.db`` 结尾）。
        meta: 可选；不传则从 ``db_path`` 读取。写入 zip 的 ``meta.json``。

    Returns:
        生成的 ``.dbz`` 绝对路径。

    Raises:
        CompressError: 源文件不存在或压缩失败（失败时尽量保留原 ``.db``）。
    """
    if not os.path.isfile(db_path):
        raise CompressError(
            t(f"无法压缩：文件不存在 {db_path}",
              f"Cannot compress: file not found {db_path}")
        )
    if is_compressed_path(db_path):
        return db_path

    if meta is None:
        try:
            meta = read_meta(db_path)
        except SnapshotError as exc:
            raise CompressError(str(exc)) from exc

    root, ext = os.path.splitext(db_path)
    dbz_path = (root + DBZ_SUFFIX) if ext.lower() == ".db" else (db_path + DBZ_SUFFIX)

    tmp_path = dbz_path + ".partial"
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        with zipfile.ZipFile(
            tmp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as zf:
            zf.writestr(_META_MEMBER, _meta_to_json(meta))
            zf.write(db_path, arcname=_DATA_MEMBER)
        # 原子替换：先写 partial，再替换目标。
        if os.path.exists(dbz_path):
            os.remove(dbz_path)
        os.replace(tmp_path, dbz_path)
    except OSError as exc:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise CompressError(
            t(f"压缩快照失败：{exc}", f"Failed to compress snapshot: {exc}")
        ) from exc

    try:
        os.remove(db_path)
    except OSError:
        # 压缩已成功；删不掉原文件只是多占一份盘，不视为失败。
        pass
    return dbz_path


def read_meta_any(path: str) -> SnapshotMeta:
    """读取快照 meta：``.db`` 走 SQLite，``.dbz`` 只读 zip 内 ``meta.json``。

    Raises:
        SnapshotError: 文件不可读或格式不对。
    """
    if is_compressed_path(path):
        try:
            with zipfile.ZipFile(path, "r") as zf:
                try:
                    raw = zf.read(_META_MEMBER)
                except KeyError as exc:
                    raise SnapshotError(
                        t(f"压缩快照缺少 meta.json：{path}",
                          f"Compressed snapshot is missing meta.json: {path}")
                    ) from exc
        except zipfile.BadZipFile as exc:
            raise SnapshotError(
                t(f"压缩快照损坏：{path}", f"Compressed snapshot is corrupt: {path}")
            ) from exc
        except OSError as exc:
            raise SnapshotError(
                t(f"无法打开压缩快照：{path}（{exc}）",
                  f"Cannot open compressed snapshot: {path} ({exc})")
            ) from exc
        return _meta_from_json(raw.decode("utf-8"))
    return read_meta(path)


def _register_atexit_once() -> None:
    global _atexit_registered
    if _atexit_registered:
        return
    atexit.register(clear_session_cache)
    _atexit_registered = True


def clear_session_cache() -> None:
    """删除本进程解压出的全部临时 ``.db``（退出时自动调用）。"""
    with _session_lock:
        items = list(_session_db.values())
        _session_db.clear()
    for temp_path, _, _ in items:
        try:
            if os.path.isfile(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def is_session_cached(path: str) -> bool:
    """``.dbz`` 是否已有本进程有效临时 ``.db``（可直接复用，无需再解压）。

    非压缩路径视为已就绪（无需解压）。路径不存在 / 缓存失效返回 False。
    """
    if not path:
        return False
    if not is_compressed_path(path):
        return True
    if not os.path.isfile(path):
        return False
    abspath = os.path.abspath(path)
    try:
        st = os.stat(abspath)
        mtime = float(st.st_mtime)
        size = int(st.st_size)
    except OSError:
        return False
    with _session_lock:
        hit = _session_db.get(abspath)
        if hit is None:
            return False
        temp_path, hit_mtime, hit_size = hit
        return (
            hit_mtime == mtime
            and hit_size == size
            and os.path.isfile(temp_path)
            and os.path.getsize(temp_path) > 0
        )


def ensure_db_path(path: str) -> str:
    """保证返回可直接给 SQLite / Diff 使用的 ``.db`` 路径。

    - 普通 ``.db``：原样返回；
    - ``.dbz``：解压 ``data.db`` 到**系统临时文件**，仅本进程登记复用；
      **不**写入应用数据目录下的 cache/。软件关闭后临时文件丢弃。

    Raises:
        SnapshotError / CompressError: 解压失败或成员缺失。
    """
    if not is_compressed_path(path):
        return path
    if not os.path.isfile(path):
        raise CompressError(
            t(f"压缩快照不存在：{path}", f"Compressed snapshot not found: {path}")
        )

    abspath = os.path.abspath(path)
    try:
        st = os.stat(abspath)
        mtime = float(st.st_mtime)
        size = int(st.st_size)
    except OSError as exc:
        raise CompressError(
            t(f"无法读取压缩快照：{path}（{exc}）",
              f"Cannot stat compressed snapshot: {path} ({exc})")
        ) from exc

    with _session_lock:
        hit = _session_db.get(abspath)
        if hit is not None:
            temp_path, hit_mtime, hit_size = hit
            if (
                hit_mtime == mtime
                and hit_size == size
                and os.path.isfile(temp_path)
                and os.path.getsize(temp_path) > 0
            ):
                return temp_path
            # 失效：删旧临时文件
            try:
                if os.path.isfile(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            _session_db.pop(abspath, None)

    # 解到系统临时目录（不进 WhoShitsOnMyC/cache）
    fd, temp_path = tempfile.mkstemp(prefix="wsmc_", suffix=".db")
    os.close(fd)
    try:
        with zipfile.ZipFile(abspath, "r") as zf:
            try:
                info = zf.getinfo(_DATA_MEMBER)
            except KeyError as exc:
                raise SnapshotError(
                    t(f"压缩快照缺少 data.db：{path}",
                      f"Compressed snapshot is missing data.db: {path}")
                ) from exc
            with zf.open(info, "r") as src, open(temp_path, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
        if os.path.getsize(temp_path) <= 0:
            raise CompressError(
                t(f"解压快照结果为空：{path}",
                  f"Decompressed snapshot is empty: {path}")
            )
    except (OSError, zipfile.BadZipFile, SnapshotError, CompressError) as exc:
        try:
            if os.path.isfile(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        if isinstance(exc, (SnapshotError, CompressError)):
            raise
        raise CompressError(
            t(f"解压快照失败：{exc}", f"Failed to decompress snapshot: {exc}")
        ) from exc

    with _session_lock:
        # 并发时可能已有别的线程解好了：保留先到者，删自己的副本
        existing = _session_db.get(abspath)
        if existing is not None:
            other, om, osz = existing
            if (
                om == mtime
                and osz == size
                and os.path.isfile(other)
                and os.path.getsize(other) > 0
            ):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                return other
        _session_db[abspath] = (temp_path, mtime, size)
        _register_atexit_once()
    return temp_path


def drop_cache_for(path: str) -> None:
    """丢弃某压缩快照在本进程内的临时解压文件（若有）。非压缩路径忽略。"""
    if not is_compressed_path(path):
        return
    abspath = os.path.abspath(path)
    with _session_lock:
        hit = _session_db.pop(abspath, None)
    if not hit:
        return
    temp_path = hit[0]
    try:
        if os.path.isfile(temp_path):
            os.remove(temp_path)
    except OSError:
        pass


def update_dbz_note(dbz_path: str, note: str) -> str:
    """更新 ``.dbz`` 内 ``meta.json`` 的 note 字段；返回生效文本。

    只重写 zip 内 meta 与 data（从原包复制 data.db），不改扫描内容。
    """
    text = (note or "").strip()
    if not os.path.isfile(dbz_path):
        raise CompressError(
            t(f"压缩快照不存在：{dbz_path}",
              f"Compressed snapshot not found: {dbz_path}")
        )
    try:
        with zipfile.ZipFile(dbz_path, "r") as zf:
            try:
                meta_raw = zf.read(_META_MEMBER)
            except KeyError as exc:
                raise SnapshotError(
                    t(f"压缩快照缺少 meta.json：{dbz_path}",
                      f"Compressed snapshot is missing meta.json: {dbz_path}")
                ) from exc
            try:
                data_raw = zf.read(_DATA_MEMBER)
            except KeyError as exc:
                raise SnapshotError(
                    t(f"压缩快照缺少 data.db：{dbz_path}",
                      f"Compressed snapshot is missing data.db: {dbz_path}")
                ) from exc
        meta = _meta_from_json(meta_raw.decode("utf-8"))
        meta.note = text
        tmp = dbz_path + ".note-partial"
        if os.path.exists(tmp):
            os.remove(tmp)
        with zipfile.ZipFile(
            tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as zf:
            zf.writestr(_META_MEMBER, _meta_to_json(meta))
            zf.writestr(_DATA_MEMBER, data_raw)
        os.replace(tmp, dbz_path)
    except (OSError, zipfile.BadZipFile) as exc:
        try:
            tmp = dbz_path + ".note-partial"
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise CompressError(
            t(f"写入备注失败：{exc}", f"Failed to write note: {exc}")
        ) from exc
    # 备注变了不改变 data.db 内容，但 mtime 变了 → 清会话解压登记
    drop_cache_for(dbz_path)
    return text


def write_snapshot_note(path: str, note: str) -> str:
    """把备注写入快照文件本身（``.db`` meta 表或 ``.dbz`` meta.json）。"""
    path = os.path.abspath(path)
    if is_compressed_path(path):
        return update_dbz_note(path, note)
    return write_meta_note(path, note)
