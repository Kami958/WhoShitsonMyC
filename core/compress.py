"""快照压缩：``.db`` ↔ ``.dbz``（zip）。

``.dbz`` 是一个 zip 包，内含：

- ``meta.json``：快照元信息，列举时只读这一份，不必解压整库；
- ``data.db``：原始 SQLite 快照。

设计取舍：扫描始终先写出完整 ``.db``（保证可立刻校验），开启压缩后
再压成 ``.dbz`` 并删掉 ``.db``。对比时才把 ``.dbz`` 解到缓存目录；
列表 / 删除 / 选择路径始终指向用户可见的 ``.db`` 或 ``.dbz`` 文件。
"""

from __future__ import annotations

import hashlib
import json
import os
import zipfile

from .i18n import t
from .models import SnapshotMeta
from .snapshot import SnapshotError, read_meta

# zip 内固定成员名。
_META_MEMBER = "meta.json"
_DATA_MEMBER = "data.db"

# 压缩后的扩展名（小写比较）。
DBZ_SUFFIX = ".dbz"


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


def cache_dir() -> str:
    """返回压缩快照解压缓存目录（必要时创建）。"""
    # 与 store 的数据根同级策略：放在应用数据目录下的 cache/。
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get(
            "XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share")
        )
    path = os.path.join(base, "WhoShitsOnMyC", "cache")
    os.makedirs(path, exist_ok=True)
    return path


def _cache_key(dbz_path: str) -> str:
    """由压缩文件路径 + 体积 + mtime 生成缓存文件名（内容变了会 miss）。"""
    st = os.stat(dbz_path)
    raw = f"{os.path.abspath(dbz_path)}|{st.st_size}|{int(st.st_mtime)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest() + ".db"


def ensure_db_path(path: str) -> str:
    """保证返回可直接给 SQLite / Diff 使用的 ``.db`` 路径。

    - 普通 ``.db``：原样返回；
    - ``.dbz``：解压 ``data.db`` 到缓存（命中缓存则复用），返回缓存路径。

    Raises:
        SnapshotError / CompressError: 解压失败或成员缺失。
    """
    if not is_compressed_path(path):
        return path
    if not os.path.isfile(path):
        raise CompressError(
            t(f"压缩快照不存在：{path}", f"Compressed snapshot not found: {path}")
        )

    out = os.path.join(cache_dir(), _cache_key(path))
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out

    tmp = out + ".partial"
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
        with zipfile.ZipFile(path, "r") as zf:
            try:
                info = zf.getinfo(_DATA_MEMBER)
            except KeyError as exc:
                raise SnapshotError(
                    t(f"压缩快照缺少 data.db：{path}",
                      f"Compressed snapshot is missing data.db: {path}")
                ) from exc
            with zf.open(info, "r") as src, open(tmp, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
        if os.path.exists(out):
            os.remove(out)
        os.replace(tmp, out)
    except (OSError, zipfile.BadZipFile) as exc:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise CompressError(
            t(f"解压快照失败：{exc}", f"Failed to decompress snapshot: {exc}")
        ) from exc
    return out


def drop_cache_for(path: str) -> None:
    """删除某压缩快照对应的解压缓存（若有）。非压缩路径忽略。"""
    if not is_compressed_path(path) or not os.path.isfile(path):
        # 文件已删时仍尝试按常见缓存名清扫太难；只在文件还在时精确删。
        # 删除时文件可能仍在：调用方应在 os.remove 之前调用本函数。
        if not is_compressed_path(path):
            return
    try:
        if os.path.isfile(path):
            key = _cache_key(path)
            cache_path = os.path.join(cache_dir(), key)
            if os.path.isfile(cache_path):
                os.remove(cache_path)
    except OSError:
        pass
