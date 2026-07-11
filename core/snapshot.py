"""SQLite 快照的读写。

一份快照 = 一个 ``.db`` 文件，含两张表：

- ``entries(id, parent_id, name, size, is_dir, mtime)``：每个文件/目录一行，
  **邻接表**结构——只存本段名字与父行 id，不存完整路径（v3 起，体积约为
  存完整路径方案的 1/5）。根目录 id 固定为 1、``parent_id`` 为 NULL。
- ``meta(key, value)``：键值对形式的元信息（root、时间、计数、skipped 列表、版本号）。

写入侧（:class:`SnapshotWriter`）针对「百万级记录一次性写入」做了性能调优：
关闭同步、日志走内存、分批 ``executemany``、**索引最后一次性建立**。
读取侧提供 meta 读取、版本校验，以及按 ``parent_id`` 懒加载子节点。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator

from .i18n import t
from .models import (
    SNAPSHOT_FORMAT_VERSION,
    Entry,
    SnapshotMeta,
)

# 每积累这么多条 entry 就 flush 一次到数据库，兼顾写入速度与内存占用。
_BATCH_SIZE = 10_000


class SnapshotError(Exception):
    """快照读写相关错误（文件损坏、版本不符等）。"""


class SnapshotWriter:
    """把扫描产生的 :class:`Entry` 流式写入一个新的快照 ``.db`` 文件。

    典型用法::

        with SnapshotWriter(db_path, root="C:\\\\") as writer:
            for entry in scan(...):
                writer.add(entry)
            writer.finalize(meta)

    写入期间应用了激进的 PRAGMA（``synchronous=OFF`` 等）以提速——
    因为快照是「一次写成、可重建」的临时数据，写到一半崩溃直接重扫即可，
    无需保证掉电安全。
    """

    def __init__(self, db_path: str, root: str) -> None:
        """打开（覆盖）目标文件并建表。

        Args:
            db_path: 目标快照文件路径。若已存在会被覆盖。
            root: 扫描根的绝对路径，仅用于记录，实际写入在 finalize。
        """
        self.db_path = db_path
        self.root = root
        self._buffer: list[tuple[str, int, int, str | None, float]] = []
        self._conn = sqlite3.connect(db_path)
        self._configure_for_fast_write()
        self._create_schema()

    def _configure_for_fast_write(self) -> None:
        """设置加速写入的 PRAGMA。"""
        cur = self._conn.cursor()
        cur.execute("PRAGMA synchronous = OFF")
        cur.execute("PRAGMA journal_mode = MEMORY")
        cur.execute("PRAGMA temp_store = MEMORY")
        self._conn.commit()

    def _create_schema(self) -> None:
        """建表（此时**不建索引**，索引留到 finalize 一次性建立）。"""
        cur = self._conn.cursor()
        cur.execute("DROP TABLE IF EXISTS entries")
        cur.execute("DROP TABLE IF EXISTS meta")
        cur.execute(
            "CREATE TABLE entries ("
            "  id INTEGER PRIMARY KEY,"
            "  parent_id INTEGER,"
            "  name TEXT NOT NULL,"
            "  size INTEGER NOT NULL,"
            "  is_dir INTEGER NOT NULL,"
            "  mtime INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        cur.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        self._conn.commit()

    def add(self, entry: Entry) -> None:
        """缓冲一条记录，必要时 flush。"""
        self._buffer.append(
            (
                entry.id,
                entry.parent_id,
                entry.name,
                entry.size,
                1 if entry.is_dir else 0,
                entry.mtime,
            )
        )
        if len(self._buffer) >= _BATCH_SIZE:
            self._flush()

    def _flush(self) -> None:
        """把缓冲区批量写入数据库。"""
        if not self._buffer:
            return
        self._conn.executemany(
            "INSERT INTO entries (id, parent_id, name, size, is_dir, mtime)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            self._buffer,
        )
        self._buffer.clear()

    def finalize(self, meta: SnapshotMeta) -> None:
        """写完剩余缓冲、写入 meta、建立索引并提交。

        Args:
            meta: 本次扫描的元信息。其 ``root`` 会以本 writer 的 root 为准。
        """
        self._flush()
        meta.root = self.root
        self._write_meta(meta)
        # 数据写完后一次性建索引，避免逐条插入时维护索引的开销。
        # 只需 parent_id 一个索引（按父查子是唯一的查询模式）。
        cur = self._conn.cursor()
        cur.execute("CREATE INDEX idx_entries_parent ON entries(parent_id)")
        self._conn.commit()

    def _write_meta(self, meta: SnapshotMeta) -> None:
        """把 meta 各字段写入 key-value 表。"""
        rows = {
            "root": meta.root,
            "scanned_at": repr(meta.scanned_at),
            "total_size": str(meta.total_size),
            "file_count": str(meta.file_count),
            "dir_count": str(meta.dir_count),
            "skipped": json.dumps(meta.skipped, ensure_ascii=False),
            "format_version": str(meta.format_version),
        }
        self._conn.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            list(rows.items()),
        )

    def close(self) -> None:
        """关闭连接（不提交未 finalize 的数据）。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None  # type: ignore[assignment]

    def __enter__(self) -> "SnapshotWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def read_meta(db_path: str) -> SnapshotMeta:
    """读取快照的元信息并校验格式版本。

    Args:
        db_path: 快照文件路径。

    Returns:
        解析后的 :class:`SnapshotMeta`。

    Raises:
        SnapshotError: 文件无法打开、缺少 meta，或格式版本不受支持。
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - 依赖文件系统
        raise SnapshotError(
            t(f"无法打开快照文件：{db_path}（{exc}）",
              f"Cannot open snapshot file: {db_path} ({exc})")
        ) from exc
    try:
        try:
            rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        except sqlite3.Error as exc:
            raise SnapshotError(
                t(f"快照文件损坏或格式不对：{db_path}（{exc}）",
                  f"Snapshot file is corrupt or has an unexpected format: {db_path} ({exc})")
            ) from exc

        if not rows:
            raise SnapshotError(
                t(f"快照缺少元信息：{db_path}",
                  f"Snapshot is missing metadata: {db_path}")
            )

        version = int(rows.get("format_version", "0"))
        if version > SNAPSHOT_FORMAT_VERSION:
            raise SnapshotError(
                t(f"快照格式版本 {version} 高于本程序支持的 "
                  f"{SNAPSHOT_FORMAT_VERSION}，请升级程序后再打开。",
                  f"Snapshot format version {version} is newer than this app supports "
                  f"({SNAPSHOT_FORMAT_VERSION}); please update the app.")
            )
        return SnapshotMeta(
            root=rows["root"],
            scanned_at=float(rows["scanned_at"]),
            total_size=int(rows.get("total_size", "0")),
            file_count=int(rows.get("file_count", "0")),
            dir_count=int(rows.get("dir_count", "0")),
            skipped=json.loads(rows.get("skipped", "[]")),
            format_version=version,
        )
    finally:
        conn.close()


def open_readonly(db_path: str) -> sqlite3.Connection:
    """以只读方式打开一个快照连接（供 differ 做 ATTACH 对比或查询子节点）。

    调用方负责 ``close()``。

    ``check_same_thread=False``：pywebview 的每次 JS-API 调用都可能在
    不同线程上执行，对比会话（:class:`~core.differ.Diff`）会跨调用复用
    连接，故必须允许跨线程使用；调用方（app.Api）以锁保证同一时刻
    只有一个线程在用。
    """
    return sqlite3.connect(
        f"file:{db_path}?mode=ro", uri=True, check_same_thread=False
    )


def children_of(conn: sqlite3.Connection, parent_id: int | None) -> Iterator[Entry]:
    """查询某个父目录的直接子节点（懒加载下钻用，仅支持 v3 快照）。

    Args:
        conn: 由 :func:`open_readonly` 打开的连接。
        parent_id: 父目录的行 id；``None`` 表示查根节点本身（parent_id IS NULL）。

    Yields:
        该父目录下的每个 :class:`Entry`。
    """
    if parent_id is None:
        cur = conn.execute(
            "SELECT id, parent_id, name, size, is_dir, mtime"
            " FROM entries WHERE parent_id IS NULL"
        )
    else:
        cur = conn.execute(
            "SELECT id, parent_id, name, size, is_dir, mtime"
            " FROM entries WHERE parent_id = ?",
            (parent_id,),
        )
    for eid, pid, name, size, is_dir, mtime in cur:
        yield Entry(
            id=eid,
            parent_id=pid,
            name=name,
            size=size,
            is_dir=bool(is_dir),
            mtime=mtime,
        )


def write_snapshot(
    db_path: str, root: str, entries: Iterable[Entry], meta: SnapshotMeta
) -> None:
    """便捷函数：把一批 entries 一次性写成快照文件。

    主要用于测试与脚本；正式扫描走 :class:`SnapshotWriter` 的流式接口。
    """
    with SnapshotWriter(db_path, root) as writer:
        for entry in entries:
            writer.add(entry)
        writer.finalize(meta)
