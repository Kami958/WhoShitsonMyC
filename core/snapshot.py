"""SQLite 快照的读写。

一份快照 = 一个 ``.db`` 文件，含两张表：

- ``entries(id, parent_id, name, size, is_dir, mtime[, reparse_tag[, link_target]])``：
  每个文件/目录一行，**邻接表**结构——只存本段名字与父行 id，不存完整
  路径（v3 起，体积约为存完整路径方案的 1/5）。根目录 id 固定为 1、
  ``parent_id`` 为 NULL。v4 起新增 ``reparse_tag`` 列，v5 起新增
  ``link_target`` 列。
- ``meta(key, value)``：键值对形式的元信息（root、时间、计数、skipped 列表、版本号）。

**版本分派**：写入侧一律写当前版本（v5）；读取侧按 meta 里的
``format_version`` 选列清单（:func:`entry_columns`），旧快照缺的列
（v3 无 ``reparse_tag``、v4 无 ``link_target``）读出为默认值，仍可读取、对比。

写入侧（:class:`SnapshotWriter`）针对「百万级记录一次性写入」做了性能调优：
关闭同步、日志走内存、分批 ``executemany``、**索引最后一次性建立**。
读取侧提供 meta 读取、版本校验，以及按 ``parent_id`` 懒加载子节点。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from typing import NamedTuple

from .i18n import t
from .models import (
    SNAPSHOT_FORMAT_VERSION,
    Entry,
    SnapshotMeta,
)

# 每积累这么多条 entry 就 flush 一次到数据库，兼顾写入速度与内存占用。
_BATCH_SIZE = 10_000

# 各版本 entries 表的 SELECT 列清单。v4 起含 reparse_tag，v5 起含 link_target。
# 解析器按快照 format_version 分派：读旧快照不带新列，读出的新字段补默认值。
_ENTRY_COLS_V3 = "id, parent_id, name, size, is_dir, mtime"
_ENTRY_COLS_V4 = "id, parent_id, name, size, is_dir, mtime, reparse_tag"
_ENTRY_COLS_V5 = (
    "id, parent_id, name, size, is_dir, mtime, reparse_tag, link_target"
)


def entry_columns(version: int) -> str:
    """按快照格式版本返回 entries 表 SELECT 列清单。

    这是读取侧的**统一解析分派**：任何读 entries 的 SQL 都用它选列。
    v5 及以上走 8 列，v4 走 7 列，低于 v4（v3）走 6 列。
    """
    v = int(version)
    if v >= 5:
        return _ENTRY_COLS_V5
    if v >= 4:
        return _ENTRY_COLS_V4
    return _ENTRY_COLS_V3


class EntryRow(NamedTuple):
    """写入缓冲的一行，字段顺序与 SQLite ``entries`` 表完全一致。

    为什么不用 :class:`Entry`（dataclass）做热路径：
    - 扫描百万级条目时，少一次对象构造与属性拆箱
    - ``NamedTuple`` 仍是 tuple，可直接交给 ``executemany``，无需再转

    字段含义与 :class:`Entry` 对齐；``is_dir`` 用整数 0/1 以匹配表列类型。
    构造请用 :meth:`file` / :meth:`directory` / :meth:`from_entry`，避免手写 0/1。
    """

    id: int
    parent_id: int | None
    name: str
    size: int
    is_dir: int  # 0=文件，1=目录（与 entries.is_dir 列一致）
    mtime: int = 0
    reparse_tag: int = 0  # Windows 重解析点标签；0=普通项，非 0=链接
    link_target: str = ""  # 链接指向的真实路径；非链接为空串

    @staticmethod
    def file(
        id: int,
        parent_id: int | None,
        name: str,
        size: int,
        mtime: int = 0,
        reparse_tag: int = 0,
        link_target: str = "",
    ) -> EntryRow:
        """构造文件行（``is_dir=0``）。"""
        return EntryRow(
            id, parent_id, name, size, 0, mtime, reparse_tag, link_target
        )

    @staticmethod
    def directory(
        id: int,
        parent_id: int | None,
        name: str,
        size: int,
        mtime: int = 0,
        reparse_tag: int = 0,
        link_target: str = "",
    ) -> EntryRow:
        """构造目录行（``is_dir=1``）。"""
        return EntryRow(
            id, parent_id, name, size, 1, mtime, reparse_tag, link_target
        )

    @staticmethod
    def from_entry(entry: Entry) -> EntryRow:
        """从领域模型 :class:`Entry` 转换（测试 / MFT 等非热路径）。"""
        return EntryRow(
            entry.id,
            entry.parent_id,
            entry.name,
            entry.size,
            1 if entry.is_dir else 0,
            entry.mtime,
            entry.reparse_tag,
            entry.link_target,
        )


class SnapshotError(Exception):
    """快照读写相关错误（文件损坏、版本不符等）。"""


class SnapshotWriter:
    """把扫描产生的行流式写入一个新的快照 ``.db`` 文件。

    热路径优先用 :meth:`add_row` / :meth:`add_rows`（:class:`EntryRow`）；
    :meth:`add` / :meth:`add_many` 仍接受 :class:`Entry`，供测试与其它路径。

    典型用法::

        with SnapshotWriter(db_path, root="C:\\\\") as writer:
            writer.add_rows(file_rows)
            writer.add_row(dir_row)
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
        self._buffer: list[EntryRow] = []
        # check_same_thread=False：MFT 路径可用独立写线程 drain 队列
        # （与 scandir「单线程写库」不同；所有公开方法仍须外部串行或单写线程）
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._configure_for_fast_write()
        self._create_schema()

    def _configure_for_fast_write(self) -> None:
        """设置加速写入的 PRAGMA。"""
        cur = self._conn.cursor()
        cur.execute("PRAGMA synchronous = OFF")
        # OFF 比 MEMORY 少一点日志开销；快照可重建，不必掉电安全
        cur.execute("PRAGMA journal_mode = OFF")
        cur.execute("PRAGMA temp_store = MEMORY")
        cur.execute("PRAGMA locking_mode = EXCLUSIVE")
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
            "  mtime INTEGER NOT NULL DEFAULT 0,"
            "  reparse_tag INTEGER NOT NULL DEFAULT 0,"
            "  link_target TEXT NOT NULL DEFAULT ''"
            ")"
        )
        cur.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        self._conn.commit()

    def add_row(self, row: EntryRow) -> None:
        """缓冲一行 :class:`EntryRow`，满批则 flush。"""
        self._buffer.append(row)
        if len(self._buffer) >= _BATCH_SIZE:
            self._flush()

    def add_rows(self, rows: list[EntryRow] | tuple[EntryRow, ...]) -> None:
        """批量缓冲多行（扫描热路径：按目录一批文件）。

        一次 ``extend`` 可能远超 ``_BATCH_SIZE``，超批时按块 ``executemany``，
        避免缓冲区无限涨。
        """
        if not rows:
            return
        buf = self._buffer
        buf.extend(rows)
        if len(buf) >= _BATCH_SIZE:
            while len(self._buffer) >= _BATCH_SIZE:
                chunk = self._buffer[:_BATCH_SIZE]
                del self._buffer[:_BATCH_SIZE]
                self._conn.executemany(
                    "INSERT INTO entries"
                    " (id, parent_id, name, size, is_dir, mtime, reparse_tag, link_target)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    chunk,
                )

    def add(self, entry: Entry) -> None:
        """缓冲一条 :class:`Entry`（测试 / MFT 等兼容路径）。"""
        self.add_row(EntryRow.from_entry(entry))

    def add_many(self, entries: list[Entry] | tuple[Entry, ...]) -> None:
        """批量缓冲多条 :class:`Entry`（内部转为 :class:`EntryRow`）。"""
        if not entries:
            return
        self.add_rows([EntryRow.from_entry(e) for e in entries])

    def _flush(self) -> None:
        """把缓冲区批量写入数据库。"""
        if not self._buffer:
            return
        self._conn.executemany(
            "INSERT INTO entries"
            " (id, parent_id, name, size, is_dir, mtime, reparse_tag, link_target)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
            "note": (meta.note or "").strip(),
            "free_size": str(meta.free_size),
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
            note=(rows.get("note") or "").strip(),
            free_size=int(rows.get("free_size", "0")),
        )
    finally:
        conn.close()


def write_meta_note(db_path: str, note: str) -> str:
    """把备注写入未压缩 ``.db`` 的 meta 表；返回生效文本（空=清除）。

    Raises:
        SnapshotError: 无法打开或写入。
    """
    text = (note or "").strip()
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        raise SnapshotError(
            t(f"无法打开快照文件：{db_path}（{exc}）",
              f"Cannot open snapshot file: {db_path} ({exc})")
        ) from exc
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("note", text),
        )
        conn.commit()
    except sqlite3.Error as exc:
        raise SnapshotError(
            t(f"写入备注失败：{db_path}（{exc}）",
              f"Failed to write note: {db_path} ({exc})")
        ) from exc
    finally:
        conn.close()
    return text


class _SnapshotConnection(sqlite3.Connection):
    """可携带快照格式版本的只读连接。

    ``sqlite3.Connection`` 原生不可挂自定义属性，故子类化并加
    ``_wmc_version``：读侧解析（:func:`entry_columns`）按它分派列清单。
    """

    _wmc_version: int = SNAPSHOT_FORMAT_VERSION


def open_readonly(db_path: str) -> sqlite3.Connection:
    """以只读方式打开一个快照连接（供 differ 做 ATTACH 对比或查询子节点）。

    调用方负责 ``close()``。

    返回的连接携带 ``_wmc_version`` 属性（快照的 format_version）：
    读侧解析按它分派列清单，旧 v3 快照自动用 v3 列（无 ``reparse_tag``）。
    读取失败/缺 meta 时按最新版本兜底（新写入的都是当前版）。

    ``check_same_thread=False``：pywebview 的每次 JS-API 调用都可能在
    不同线程上执行，对比会话（:class:`~core.differ.Diff`）会跨调用复用
    连接，故必须允许跨线程使用；调用方（app.Api）以锁保证同一时刻
    只有一个线程在用。
    """
    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro",
        uri=True,
        check_same_thread=False,
        factory=_SnapshotConnection,
    )
    version = 0
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'format_version'"
        ).fetchone()
        if row:
            version = int(row[0])
    except (sqlite3.Error, ValueError, TypeError):  # pragma: no cover - 依赖文件
        version = 0
    conn._wmc_version = version or SNAPSHOT_FORMAT_VERSION  # type: ignore[attr-defined]
    return conn


def children_of(conn: sqlite3.Connection, parent_id: int | None) -> Iterator[Entry]:
    """查询某个父目录的直接子节点（懒加载下钻用）。

    v3/v4/v5 快照均支持：按连接携带的版本号选列，旧快照缺的列读出为
    默认值（``reparse_tag`` 为 0、``link_target`` 为空串）。

    Args:
        conn: 由 :func:`open_readonly` 打开的连接。
        parent_id: 父目录的行 id；``None`` 表示查根节点本身（parent_id IS NULL）。

    Yields:
        该父目录下的每个 :class:`Entry`。
    """
    cols = entry_columns(
        getattr(conn, "_wmc_version", SNAPSHOT_FORMAT_VERSION)
    )
    if parent_id is None:
        cur = conn.execute(f"SELECT {cols} FROM entries WHERE parent_id IS NULL")
    else:
        cur = conn.execute(
            f"SELECT {cols} FROM entries WHERE parent_id = ?",
            (parent_id,),
        )
    for eid, pid, name, size, is_dir, mtime, *rest in cur:
        yield Entry(
            id=eid,
            parent_id=pid,
            name=name,
            size=size,
            is_dir=bool(is_dir),
            mtime=mtime,
            reparse_tag=int(rest[0]) if rest else 0,
            link_target=str(rest[1]) if len(rest) > 1 else "",
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
