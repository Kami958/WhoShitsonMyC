"""磁盘遍历：把一个根目录扫描成快照（多线程并行版）。

核心是 :func:`scan_to_snapshot`——并行遍历目录树、自底向上聚合目录大小、
流式写入 SQLite 快照，并回报进度、支持取消。

设计要点（TreeSize 思路：目录级并行）：

- **多 worker 并行枚举**：NTFS 元数据读取可以并行，Python 的
  ``scandir/stat`` 在系统调用期间释放 GIL，多线程有实打实的提速。
  一个目录 = 一个任务，worker 从队列取目录、枚举内容、把子目录再入队。
- **自底向上聚合**：每个目录节点记录「尚未完成的子目录数」，枚举结束且
  所有子目录子树完成时视为完成——此刻聚合大小已知，产出目录行并向父级
  传播。根节点完成 = 整棵树完成。
- **单线程写库**：所有行经队列汇到调用方线程统一写 SQLite，
  连接不跨线程，天然无锁。文件行按目录批量入队，减少队列次数。
- **os.scandir + 单次 stat**：每项一次系统调用取齐属性。
- **无权限目录跳过**：记入 skipped，不中断整体。
- **默认不跟随符号链接/重解析点**：避免死循环与重复计算。
"""

from __future__ import annotations

import itertools
import os
import stat
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .i18n import t
from .models import SnapshotMeta
from .snapshot import EntryRow, SnapshotWriter

# 进度回调：progress(files_scanned: int, current_dir: str)
ProgressCallback = Callable[[int, str], None]
# 取消检查：返回 True 时中止扫描
CancelCheck = Callable[[], bool]

# 进度推送最短间隔（秒）。必须按墙钟硬节流：evaluate_js 跨桥很贵，
# 绝不能「文件数一变就推」。数字用 worker 已发现量，路径取最近目录。
_PROGRESS_MIN_INTERVAL = 0.22

# 行队列上的结束标记：根目录完成后由持锁方投放，保证是最后一个元素。
_ROWS_DONE = object()
# 任务队列上的退出标记：通知 worker 收工。
_TASK_DONE = object()


class ScanCancelled(Exception):
    """扫描被调用方主动取消。"""


@dataclass(slots=True)
class _DirNode:
    """并行遍历中的一个目录节点。

    ``acc``/``pending``/``enumerated`` 只能在持有全局树锁时读写。
    """

    id: int             # 该目录在快照中的行 id
    parent: "_DirNode | None"
    name: str           # 本段名字；根为 ""
    rel: str            # 相对扫描根的路径（进度显示与 skipped 记录用）
    abspath: str        # 绝对路径（可能带 \\?\ 长路径前缀）
    mtime: int = 0      # 目录自身修改时间
    acc: int = 0        # 已聚合的子树大小
    pending: int = 0    # 尚未完成的子目录数
    enumerated: bool = False  # 本目录自身是否已枚举完


def _long_path(path: str) -> str:
    """在 Windows 上为绝对路径加 ``\\\\?\\`` 前缀以突破 260 字符限制。

    非 Windows 平台原样返回。
    """
    if os.name != "nt":
        return path
    norm = os.path.abspath(path)
    if norm.startswith("\\\\?\\"):
        return norm
    if norm.startswith("\\\\"):  # UNC 路径 \\server\share
        return "\\\\?\\UNC\\" + norm[2:]
    return "\\\\?\\" + norm


def _is_reparse_point(stat) -> bool:
    """判断一个目录项是否为符号链接 / junction / 其它重解析点。
    这类项默认不深入，以免死循环或重复计算。尽量稳健地跨版本判断。
    """
    #Windows 定义的一个文件属性常量
    attrs = int(getattr(stat, "st_file_attributes",0))
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


def _child_rel(parent_rel: str, name: str) -> str:
    """拼出子项的相对路径。根（parent_rel == ""）的子项即其名字。"""
    return name if parent_rel == "" else parent_rel + os.sep + name


class _Scan:
    """一次并行扫描的共享状态与线程协调。"""

    def __init__(
        self,
        meta: SnapshotMeta,
        *,
        follow_symlinks: bool,
        cancel: CancelCheck | None,
        workers: int,
    ) -> None:
        self.meta = meta
        # 是否允许跟随硬链接走下去，默认是否
        self.follow = follow_symlinks
        self.cancel = cancel or (lambda: False)
        self.workers = workers

        self.ids = itertools.count(2)  # 1 号留给根
        self.tasks: queue.SimpleQueue = queue.SimpleQueue()
        self.rows: queue.SimpleQueue = queue.SimpleQueue()
        self.tree_lock = threading.Lock()
        self.stop = threading.Event()      # 取消/出错时让 worker 尽快收工
        self.current = ""                  # 最近在扫的目录（进度显示，弱一致即可）
        self.errors: list[BaseException] = []
        # worker 侧已发现的文件数（枚举完成即计，不依赖写库速度）
        self._found_lock = threading.Lock()
        self.files_found = 0

    # ---- worker 侧 ------------------------------------------------------

    def worker_loop(self) -> None:
        """worker 线程体：不断取目录任务处理，直到收到退出标记。"""
        while True:
            node = self.tasks.get()
            if node is _TASK_DONE or self.stop.is_set():
                return
            try:
                self._process_dir(node)
            except BaseException as exc:  # noqa: BLE001 - 记录后让主线程决定
                self.errors.append(exc)
                self.stop.set()
                self.rows.put(_ROWS_DONE)  # 唤醒写入线程去处理错误
                return

    def _process_dir(self, node: _DirNode) -> None:
        """枚举一个目录：文件行按目录批量入队，子目录建节点再入队。"""
        self.current = node.rel or self.meta.root

        subdirs: list[_DirNode] = []
        # 热路径用 EntryRow（NamedTuple），少一层 dataclass 分配；见 SnapshotWriter.add_rows
        file_batch: list[EntryRow] = []
        files_size = 0
        parent_id = node.id
        try:
            scan_it = os.scandir(node.abspath)
        except (PermissionError, OSError):
            # 整个目录无法读取：记为跳过。目录行仍会以 acc=0 产出。
            with self.tree_lock:
                self.meta.skipped.append(node.rel)
                self._finish_enumeration(node, subdirs, files_size)
            return

        with scan_it:
            while True:
                try:
                    entry = next(scan_it)
                except StopIteration:
                    break
                except OSError:
                    # 迭代过程中出错（如目录内容变动），停止读取该目录。
                    break

                try:
                    # 每项一次 stat：权限/类型/大小/mtime 一并拿到内存再分支。
                    st = entry.stat(follow_symlinks=False)
                    descend = stat.S_ISDIR(st.st_mode) and (
                        self.follow or not _is_reparse_point(st)
                    )
                except OSError:
                    descend = False
                    st = None  # type: ignore[assignment]

                if descend:
                    try:
                        dir_mtime = int(st.st_mtime)
                    except (OSError, AttributeError):
                        dir_mtime = 0
                    subdirs.append(
                        _DirNode(
                            id=next(self.ids),
                            parent=node,
                            name=entry.name,
                            rel=_child_rel(node.rel, entry.name),
                            abspath=entry.path,
                            mtime=dir_mtime,
                        )
                    )
                else:
                    # 文件、或不深入的符号链接/重解析点：取自身大小。
                    try:
                        size, mtime = st.st_size, int(st.st_mtime)
                    except (OSError, ValueError, AttributeError):
                        size, mtime = 0, 0
                    file_batch.append(
                        EntryRow.file(
                            next(self.ids), parent_id, entry.name, size, mtime
                        )
                    )
                    files_size += size

        # 按目录一次 put，减少队列操作次数（P4）。
        if file_batch:
            with self._found_lock:
                self.files_found += len(file_batch)
            self.rows.put(file_batch)

        with self.tree_lock:
            self._finish_enumeration(node, subdirs, files_size)
        # 枚举完再入队子目录：保证 pending 计数先于任何子目录完成事件。
        for child in subdirs:
            self.tasks.put(child)

    def _finish_enumeration(
        self, node: _DirNode, subdirs: list[_DirNode], files_size: int
    ) -> None:
        """（须持 tree_lock）标记枚举完成，若无未完成子目录则触发完成传播。"""
        node.acc += files_size
        node.pending = len(subdirs)
        node.enumerated = True
        if node.pending == 0:
            self._complete(node)

    def _complete(self, node: _DirNode) -> None:
        """（须持 tree_lock）目录完成：产出目录行并向上传播，循环代替递归。"""
        while True:
            self.rows.put(
                EntryRow.directory(
                    node.id,
                    node.parent.id if node.parent else None,
                    node.name,
                    node.acc,
                    node.mtime,
                )
            )
            self.meta.dir_count += 1
            parent = node.parent
            if parent is None:
                # 根完成 = 全部完成。_ROWS_DONE 在持锁时投放，保证是队尾。
                self.meta.total_size = node.acc
                self.rows.put(_ROWS_DONE)
                return
            parent.acc += node.acc
            parent.pending -= 1
            if not (parent.enumerated and parent.pending == 0):
                return
            node = parent

    # ---- 写入侧（调用方线程）--------------------------------------------

    def drain_rows(
        self, writer: SnapshotWriter, progress: ProgressCallback | None
    ) -> None:
        """把行队列写入快照，直到收到结束标记。兼管进度回报与取消检查。

        进度数字优先用 worker 已发现的文件数（``files_found``），与磁盘枚举
        同步；写库仍单独累计 ``meta.file_count``。按时间节流推送，避免
        evaluate_js 过频，也避免「每 N 条才跳一次」造成的前慢后跳。

        队列约定：
        - ``list[EntryRow]``：同一目录下的文件批（``EntryRow.file``，``is_dir=0``）
        - 单条 ``EntryRow``：目录完成行（``EntryRow.directory``）

        Raises:
            ScanCancelled: 调用方请求取消。
            Exception: worker 线程内抛出的异常在此重新抛出。
        """
        last_prog_t = 0.0

        def _maybe_progress(*, force: bool = False) -> None:
            nonlocal last_prog_t
            if progress is None:
                return
            now = time.perf_counter()
            # 纯时间节流：路径与计数都随这次推送刷新，避免每批写库打桥
            if not force and now - last_prog_t < _PROGRESS_MIN_INTERVAL:
                return
            with self._found_lock:
                n = self.files_found
            n = max(n, self.meta.file_count)
            last_prog_t = now
            progress(n, self.current)

        while True:
            if self.cancel():
                self._shutdown_workers()
                raise ScanCancelled()
            try:
                item = self.rows.get(timeout=0.1)
            except queue.Empty:
                # 空转也推进度：worker 可能在扫大目录尚未 put 行
                _maybe_progress()
                continue
            if item is _ROWS_DONE:
                if self.errors:
                    self._shutdown_workers()
                    raise self.errors[0]
                self._shutdown_workers()
                if progress is not None:
                    progress(max(self.meta.file_count, self.files_found), self.current)
                return
            if isinstance(item, list):
                # 文件批：条数即文件数，不必再逐条看 is_dir
                writer.add_rows(item)
                self.meta.file_count += len(item)
            else:
                writer.add_row(item)
                # 目录完成行 is_dir=1；若将来有单条文件行，is_dir=0 时仍计入
                if not item.is_dir:
                    self.meta.file_count += 1
            _maybe_progress()

    def _shutdown_workers(self) -> None:
        """通知所有 worker 退出（幂等）。"""
        self.stop.set()
        for _ in range(self.workers):
            self.tasks.put(_TASK_DONE)


def scan_to_snapshot(
    root: str,
    db_path: str,
    *,
    follow_symlinks: bool = False,
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
    now: float | None = None,
    workers: int | None = None,
    timer: object | None = None,
) -> SnapshotMeta:
    """并行扫描 ``root`` 并把结果写入 ``db_path`` 快照文件。

    这是扫描的主入口：多线程遍历 + 流式写库 + 生成 meta 一步到位。
    调用方线程充当唯一写入者，worker 线程只做目录枚举（I/O 密集，
    scandir 释放 GIL，可真并行）。

    Args:
        root: 要扫描的根目录（绝对路径）。
        db_path: 输出快照文件路径（已存在会被覆盖）。
        follow_symlinks: 是否跟随符号链接/重解析点（默认 False）。
        progress: 进度回调 ``(已扫文件数, 当前目录)``。
        cancel: 取消检查，返回 True 时中止并抛 :class:`ScanCancelled`。
        now: 覆盖「扫描时间戳」，仅用于测试；默认取当前时间。
        workers: worker 线程数；默认 ``max(1, cpu 核数)``。应用层可用盘类型启发式默认。
        timer: 可选分段计时器（开发用，见 :mod:`core.timing_probe`）；
            生产路径传 ``None`` 即可。

    Returns:
        本次扫描的 :class:`SnapshotMeta`（已写入快照）。

    Raises:
        FileNotFoundError: ``root`` 不存在。
        NotADirectoryError: ``root`` 不是目录。
        ScanCancelled: 被取消（此时快照文件不完整，应丢弃）。
    """
    # 计时接口：有则用，无则空操作（避免到处 if timer）。
    _mark = getattr(timer, "mark", None)
    _span_start = getattr(timer, "span_start", None)
    _span_end = getattr(timer, "span_end", None)
    _set_meta = getattr(timer, "set_meta", None)

    if not os.path.exists(root):
        raise FileNotFoundError(
            t(f"扫描目标不存在：{root}", f"Scan target does not exist: {root}")
        )
    if not os.path.isdir(root):
        raise NotADirectoryError(
            t(f"扫描目标不是目录：{root}", f"Scan target is not a folder: {root}")
        )

    workers = workers or max(1, os.cpu_count() or 1)

    # 盘符根 + NTFS + use_mft（默认开）：尝试 MFT，失败回退目录遍历。
    # 解析进程数由 mft.parallel 按核数/记录量自推导，不传扫描线程数。
    if not follow_symlinks:
        try:
            from .mft import MftUnavailable, is_mft_eligible, scan_mft_to_snapshot
        except ImportError:
            is_mft_eligible = None  # type: ignore[assignment]
        else:
            if is_mft_eligible(root):
                try:
                    if _mark:
                        _mark("mft_try")
                    return scan_mft_to_snapshot(
                        root,
                        db_path,
                        follow_symlinks=follow_symlinks,
                        progress=progress,
                        cancel=cancel,
                        now=now,
                        timer=timer,
                    )
                except MftUnavailable:
                    if _mark:
                        _mark("mft_fallback")
                # ScanCancelled 等向上抛
    meta = SnapshotMeta(root=os.path.abspath(root), scanned_at=now or time.time())

    root_abs = _long_path(root)
    try:
        root_mtime = int(os.stat(root_abs).st_mtime)
    except OSError:
        root_mtime = 0

    scan = _Scan(
        meta, follow_symlinks=follow_symlinks, cancel=cancel, workers=workers
    )
    scan.tasks.put(
        _DirNode(
            id=1, parent=None, name="", rel="", abspath=root_abs, mtime=root_mtime
        )
    )

    threads = [
        threading.Thread(target=scan.worker_loop, daemon=True, name=f"scan-{i}")
        for i in range(workers)
    ]
    for th in threads:
        th.start()

    if _mark:
        _mark("workers_started")
    try:
        with SnapshotWriter(db_path, root=meta.root) as writer:
            if _span_start:
                _span_start("drain_rows")
            try:
                scan.drain_rows(writer, progress)
            finally:
                if _span_end:
                    _span_end("drain_rows")
            if _mark:
                _mark("drain_done")
            if _span_start:
                _span_start("finalize")
            try:
                writer.finalize(meta)
            finally:
                if _span_end:
                    _span_end("finalize")
            if _mark:
                _mark("finalize_done")
    finally:
        # 正常结束/取消/出错都确保 worker 收到退出标记，不留孤儿线程。
        scan._shutdown_workers()

    if _set_meta:
        _set_meta(
            file_count=meta.file_count,
            dir_count=meta.dir_count,
            total_size=meta.total_size,
            skipped_count=len(meta.skipped),
            workers=workers,
        )
    return meta
