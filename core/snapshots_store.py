"""快照文件操作，路径与命名规则。

从 core/store.py 拆出，专管「快照存哪、叫什么名、有哪些、怎么删」：
文件路径生成、备注、摘要读取、归纳文件夹增删改、列举与删除。
settings.yaml 的读写仍在 core/store.py；此处只依赖其只读接口
（default_snapshot_dir 等），与设置写入不互相依赖。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from .compress import (
    drop_cache_for,
    is_compressed_path,
    is_snapshot_filename,
    read_meta_any,
    write_snapshot_note,
)
from .snapshot import SnapshotError



# 备注最大长度（字符）；写入快照文件内的 meta，过长截断。
_NOTE_MAX_LEN = 200


# 归纳文件夹名最大长度（字符）。
_FOLDER_MAX_LEN = 64


# 文件名中不安全的字符统一替换为下划线。
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


# 归纳文件夹名：允许中文与常见字符，禁止路径分隔与控制字符。
_FOLDER_BAD_RE = re.compile(r'[\x00-\x1f\\/:*?"<>|]')


def _same_dir(a: str, b: str) -> bool:
    """两路径是否同一目录（规范化后比较）。"""
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(
            os.path.abspath(b)
        )
    except OSError:
        return False


def default_snapshot_dir() -> str:
    """当前生效的快照目录（用户设置优先，未设置回落内置数据目录）。"""
    from .store import get_snapshot_dir

    return get_snapshot_dir()


def snapshot_content_key(
    root: str,
    scanned_at: float,
    total_size: int,
    file_count: int,
    skipped_count: int = 0,
) -> str:
    """同一份扫描结果的内容指纹（不读整文件、不依赖路径）。

    用 meta 关键字段区分「是否同一快照」：用户复制/移动到别处后 path 不同，
    但 root+时间+计数一致则视为重复。整文件 hash 对几十 MB 的 ``.db`` 过重。
    """
    # scanned_at 可能是 float；统一到毫秒整数，避免 1.0 vs 1.0000001
    ts_ms = int(round(float(scanned_at) * 1000))
    return (
        f"{root}|{ts_ms}|{int(total_size)}|{int(file_count)}|{int(skipped_count)}"
    )


@dataclass(slots=True)
class SnapshotInfo:
    """列举快照时返回的摘要（读取每份快照的 meta 得到）。"""

    path: str          # 快照文件绝对路径（``.db`` 或 ``.dbz``）
    root: str          # 该快照扫描的根
    scanned_at: float  # 扫描时间戳
    total_size: int    # 根聚合总大小
    file_count: int    # 文件数
    skipped_count: int # 跳过的目录数
    compressed: bool = False  # 是否为压缩包（``.dbz``）
    file_size: int = 0        # 磁盘上该文件的字节数
    note: str = ""            # 用户备注（写在快照文件内：.db meta / .dbz meta.json）
    # 相对快照根的一层归纳文件夹名；空串表示在根目录（未归入文件夹）
    folder: str = ""
    # 扫描时该盘剩余空间（字节）；旧快照无此字段，为 0
    free_size: int = 0

    @property
    def content_key(self) -> str:
        return snapshot_content_key(
            self.root,
            self.scanned_at,
            self.total_size,
            self.file_count,
            self.skipped_count,
        )

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "root": self.root,
            "scanned_at": self.scanned_at,
            "total_size": self.total_size,
            "file_count": self.file_count,
            "skipped_count": self.skipped_count,
            "compressed": self.compressed,
            "file_size": self.file_size,
            "content_key": self.content_key,
            "note": self.note or "",
            "folder": self.folder or "",
            "free_size": self.free_size,
        }


def _root_label(root: str) -> str:
    """由扫描根生成一段文件名友好的标签。

    例如 ``C:\\`` → ``C``，``D:\\Games`` → ``D_Games``。
    """
    drive, tail = os.path.splitdrive(root)
    drive = drive.rstrip(":\\/")
    tail = tail.strip("\\/")
    label = drive if not tail else f"{drive}_{tail}" if drive else tail
    label = _UNSAFE_RE.sub("_", label).strip("_")
    return label or "root"


def sanitize_folder_name(name: str | None) -> str:
    """校验并规范化一层归纳文件夹名；非法则抛 ``ValueError``。

    - 空 / 仅空白 → 空串（表示快照根，未归入文件夹）
    - 禁止 ``.`` / ``..``、路径分隔符与 Windows 非法文件名字符
    - 截断到 :data:`_FOLDER_MAX_LEN`
    """
    if name is None:
        return ""
    s = str(name).strip()
    if not s:
        return ""
    if s in (".", ".."):
        raise ValueError("invalid folder name")
    if _FOLDER_BAD_RE.search(s):
        raise ValueError("invalid folder name")
    # 末尾点/空格在 Windows 上易出问题
    s = s.rstrip(". ").strip()
    if not s or s in (".", ".."):
        raise ValueError("invalid folder name")
    if len(s) > _FOLDER_MAX_LEN:
        s = s[:_FOLDER_MAX_LEN].rstrip(". ").strip()
    if not s:
        raise ValueError("invalid folder name")
    return s


def _folder_of_path(path: str, base_dir: str) -> str:
    """相对 ``base_dir`` 的一层文件夹名；在根下或越界则 ``""``。"""
    try:
        abs_path = os.path.abspath(path)
        abs_base = os.path.abspath(base_dir)
        parent = os.path.dirname(abs_path)
        if _same_dir(parent, abs_base):
            return ""
        # 仅认「base 下恰好一层子目录」
        grand = os.path.dirname(parent)
        if not _same_dir(grand, abs_base):
            return ""
        name = os.path.basename(parent)
        try:
            return sanitize_folder_name(name)
        except ValueError:
            return ""
    except OSError:
        return ""


def _resolve_folder_dir(folder: str, base_dir: str | None = None) -> str:
    """把文件夹名解析为绝对目录；空串 = 快照根。"""
    base = os.path.abspath(base_dir or default_snapshot_dir())
    name = sanitize_folder_name(folder)
    if not name:
        return base
    return os.path.join(base, name)


def new_snapshot_path(root: str, when: float | None = None, out_dir: str | None = None) -> str:
    """为一次新扫描生成快照文件路径，形如 ``C_2026-07-10_1530.db``。

    扫描过程始终先写未压缩的 ``.db``；若开启压缩，扫完后再换成 ``.dbz``。
    新建扫描始终落在快照根目录（不自动进归纳文件夹）。

    Args:
        root: 扫描根。
        when: 时间戳，默认当前时间（仅测试会传入固定值）。
        out_dir: 存放目录，默认 :func:`default_snapshot_dir`。
    """
    out_dir = out_dir or default_snapshot_dir()
    stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(when or time.time()))
    name = f"{_root_label(root)}_{stamp}.db"
    return os.path.join(out_dir, name)


def set_note(path: str, note: str) -> str:
    """把备注写入快照文件本身（``.db`` meta 表或 ``.dbz`` meta.json）。

    Args:
        path: 快照文件绝对或相对路径。
        note: 备注文本；空串表示清除。

    Returns:
        生效文本（截断后，空=已清除）。

    Raises:
        ValueError: 路径为空或不是快照文件。
        SnapshotError / CompressError / OSError: 读写失败。
    """
    if not path or not str(path).strip():
        raise ValueError("empty path")
    abspath = os.path.abspath(str(path).strip())
    if not is_snapshot_filename(os.path.basename(abspath)):
        raise ValueError(f"not a snapshot file: {abspath}")
    if not os.path.isfile(abspath):
        raise SnapshotError(f"not a file: {abspath}")
    text = (note or "").strip()[:_NOTE_MAX_LEN]
    return write_snapshot_note(abspath, text)


def snapshot_info(path: str, *, base_dir: str | None = None) -> SnapshotInfo:
    """读取单个快照文件的摘要；失败抛 :class:`SnapshotError` 或 ``OSError``。

    ``base_dir`` 用于计算 ``folder``（相对快照根的一层子目录名）；
    默认当前生效的快照目录。导入的外部路径若不在 base 下一层，folder 为空。
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise SnapshotError(f"not a file: {path}")
    if not is_snapshot_filename(os.path.basename(path)):
        raise SnapshotError(f"not a snapshot file: {path}")
    meta = read_meta_any(path)
    try:
        file_size = os.path.getsize(path)
    except OSError:
        file_size = 0
    base = base_dir if base_dir is not None else default_snapshot_dir()
    folder = _folder_of_path(path, base) if base else ""
    return SnapshotInfo(
        path=path,
        root=meta.root,
        scanned_at=meta.scanned_at,
        total_size=meta.total_size,
        file_count=meta.file_count,
        skipped_count=len(meta.skipped),
        compressed=is_compressed_path(path),
        file_size=file_size,
        note=(meta.note or "").strip()[:_NOTE_MAX_LEN],
        folder=folder,
        free_size=meta.free_size,
    )


def list_snapshot_folders(out_dir: str | None = None) -> list[str]:
    """列举快照根下一层归纳文件夹名（已存在的子目录，按名称排序）。

    只认合法文件夹名；空目录也会列出，便于侧栏展示与移动目标。
    """
    out_dir = out_dir or default_snapshot_dir()
    if not os.path.isdir(out_dir):
        return []
    names: list[str] = []
    try:
        entries = os.listdir(out_dir)
    except OSError:
        return []
    for name in entries:
        path = os.path.join(out_dir, name)
        try:
            if not os.path.isdir(path) or os.path.islink(path):
                continue
        except OSError:
            continue
        try:
            safe = sanitize_folder_name(name)
        except ValueError:
            continue
        if safe and safe == name:
            names.append(safe)
    names.sort(key=lambda s: s.casefold())
    return names


def create_snapshot_folder(name: str, out_dir: str | None = None) -> str:
    """在快照根下创建一层归纳文件夹；已存在则直接返回名称。

    Returns:
        规范化后的文件夹名。

    Raises:
        ValueError: 名称非法。
        OSError: 创建失败。
    """
    safe = sanitize_folder_name(name)
    if not safe:
        raise ValueError("empty folder name")
    base = out_dir or default_snapshot_dir()
    path = os.path.join(base, safe)
    os.makedirs(path, exist_ok=True)
    if not os.path.isdir(path):
        raise OSError(f"not a directory: {path}")
    return safe


def move_snapshot_to_folder(
    path: str,
    folder: str | None = "",
    *,
    out_dir: str | None = None,
) -> str:
    """把快照文件移到快照根或某一层归纳文件夹。

    ``folder`` 空串 = 移回快照根。目标已有同名文件则抛 ``OSError``。
    移动成功后若源文件夹已空则尝试删除空目录。

    Returns:
        移动后的绝对路径。
    """
    if not path or not str(path).strip():
        raise ValueError("empty path")
    src = os.path.abspath(str(path).strip())
    if not is_snapshot_filename(os.path.basename(src)):
        raise ValueError(f"not a snapshot file: {src}")
    if not os.path.isfile(src):
        raise SnapshotError(f"not a file: {src}")

    base = os.path.abspath(out_dir or default_snapshot_dir())
    dest_dir = _resolve_folder_dir(folder or "", base)
    # 目标文件夹必须在 base 下（根或一层子目录）
    if not (
        _same_dir(dest_dir, base)
        or _same_dir(os.path.dirname(dest_dir), base)
    ):
        raise ValueError("folder must be under snapshot root")
    if not _same_dir(dest_dir, base):
        os.makedirs(dest_dir, exist_ok=True)

    dest = os.path.join(dest_dir, os.path.basename(src))
    if _same_dir(os.path.dirname(src), dest_dir) and os.path.normcase(
        src
    ) == os.path.normcase(dest):
        return src
    if os.path.exists(dest):
        raise OSError(f"target already exists: {dest}")

    try:
        drop_cache_for(src)
    except Exception:  # noqa: BLE001
        pass

    src_parent = os.path.dirname(src)
    try:
        os.replace(src, dest)
    except OSError:
        import shutil

        shutil.copy2(src, dest)
        try:
            drop_cache_for(src)
        except Exception:  # noqa: BLE001
            pass
        os.remove(src)

    # 源在 base 下一层子目录且已空 → 删空夹
    try:
        if (
            not _same_dir(src_parent, base)
            and _same_dir(os.path.dirname(src_parent), base)
            and os.path.isdir(src_parent)
            and not os.listdir(src_parent)
        ):
            os.rmdir(src_parent)
    except OSError:
        pass
    return dest


def rename_snapshot_folder(
    old_name: str,
    new_name: str,
    *,
    out_dir: str | None = None,
) -> str:
    """重命名快照根下的一层归纳文件夹。

    Returns:
        新文件夹名。
    """
    old = sanitize_folder_name(old_name)
    new = sanitize_folder_name(new_name)
    if not old or not new:
        raise ValueError("empty folder name")
    if old == new:
        return new
    base = os.path.abspath(out_dir or default_snapshot_dir())
    src = os.path.join(base, old)
    dst = os.path.join(base, new)
    if not os.path.isdir(src):
        raise SnapshotError(f"folder not found: {old}")
    if os.path.exists(dst):
        raise OSError(f"target already exists: {new}")
    os.rename(src, dst)
    return new


def delete_snapshot_folder(
    name: str,
    *,
    out_dir: str | None = None,
    force: bool = False,
    permanent: bool = False,
    move_out: bool = False,
) -> dict:
    """删除快照根下的一层归纳文件夹。

    - 默认仅允许空目录；``force=True`` 时直接对整个文件夹执行删除
      （``permanent=False`` 送回收站，``True`` 彻底删除）。
    - ``move_out=True`` 时把夹内快照逐个移回快照根（未归类），不删除文件；
      遇到未归类下同名快照时自动重命名（在扩展名前插入 ``_1``、``_2``...）。
    - 空目录本身始终 ``rmdir``。

    Returns:
        ``{"processed": int, "renamed": [{"from": str, "to": str}, ...]}``。
    """
    from .fs_delete import delete_path

    safe = sanitize_folder_name(name)
    if not safe:
        raise ValueError("empty folder name")
    base = os.path.abspath(out_dir or default_snapshot_dir())
    path = os.path.join(base, safe)
    if not os.path.isdir(path):
        raise SnapshotError(f"folder not found: {safe}")
    try:
        names = os.listdir(path)
    except OSError as exc:
        raise OSError(f"cannot list folder: {exc}") from exc

    snap_names = [n for n in names if is_snapshot_filename(n)]
    processed = 0
    renamed: list[dict[str, str]] = []

    if move_out:
        for n in names:
            p = os.path.join(path, n)
            if os.path.isfile(p) and is_snapshot_filename(n):
                dest_name = _unique_snapshot_name(base, n)
                if dest_name != n:
                    renamed.append({"from": n, "to": dest_name})
                dest = os.path.join(base, dest_name)
                try:
                    drop_cache_for(p)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    os.replace(p, dest)
                except OSError:
                    import shutil

                    shutil.copy2(p, dest)
                    try:
                        drop_cache_for(p)
                    except Exception:  # noqa: BLE001
                        pass
                    os.remove(p)
                processed += 1
            elif os.path.isdir(p) and not os.path.islink(p):
                raise OSError(f"unexpected subfolder: {n}")
            else:
                try:
                    os.remove(p)
                except OSError as exc:
                    raise OSError(f"cannot remove {n}: {exc}") from exc
        leftover = os.listdir(path) if os.path.isdir(path) else []
        if leftover:
            raise OSError("folder is not empty")
        os.rmdir(path)
        return {"processed": processed, "renamed": renamed}

    if not force and names:
        raise OSError("folder is not empty")

    if force:
        # 整个夹直接送回收站或彻底删除
        try:
            drop_cache_for(path)
        except Exception:  # noqa: BLE001
            pass
        delete_path(path, permanent=bool(permanent))
        return {"processed": len(snap_names), "renamed": []}

    os.rmdir(path)
    return {"processed": 0, "renamed": []}


def _unique_snapshot_name(base: str, name: str) -> str:
    """在 ``base`` 下找一个不冲突的快照文件名；同名时插入 ``_1``、``_2``..."""
    if not os.path.exists(os.path.join(base, name)):
        return name
    stem, ext = os.path.splitext(name)
    if ext.lower() == ".dbz":
        ext = ".dbz"  # 保持原扩展名大小写
    i = 1
    while True:
        cand = f"{stem}_{i}{ext}"
        if not os.path.exists(os.path.join(base, cand)):
            return cand
        i += 1


def list_snapshots(out_dir: str | None = None) -> list[SnapshotInfo]:
    """列举快照根及一层子目录内所有可读快照，按扫描时间从新到旧排序。

    同时支持 ``.db`` 与 ``.dbz``。``.dbz`` 只读 zip 内 ``meta.json``，不解压整库。
    无法读取（损坏/版本不符）的文件会被跳过，不影响其余。
    更深层级目录中的文件忽略（归纳只支持一层）。
    """
    out_dir = out_dir or default_snapshot_dir()
    if not os.path.isdir(out_dir):
        return []

    base = os.path.abspath(out_dir)
    infos: list[SnapshotInfo] = []

    def _try_add(path: str) -> None:
        try:
            infos.append(snapshot_info(path, base_dir=base))
        except (SnapshotError, OSError):
            return

    try:
        top_names = os.listdir(base)
    except OSError:
        return []

    for name in top_names:
        path = os.path.join(base, name)
        try:
            if os.path.isfile(path) and is_snapshot_filename(name):
                _try_add(path)
            elif os.path.isdir(path) and not os.path.islink(path):
                try:
                    sanitize_folder_name(name)
                except ValueError:
                    continue
                try:
                    child_names = os.listdir(path)
                except OSError:
                    continue
                for child in child_names:
                    if not is_snapshot_filename(child):
                        continue
                    child_path = os.path.join(path, child)
                    if os.path.isfile(child_path):
                        _try_add(child_path)
        except OSError:
            continue

    infos.sort(key=lambda i: i.scanned_at, reverse=True)
    return infos


def delete_snapshot(path: str, *, permanent: bool = False) -> None:
    """删除一个快照文件；若是 ``.dbz``，顺带清掉本进程解压临时文件。

    ``permanent=False``（默认）进回收站；``True`` 彻底删除。
    文件不存在时静默返回。回收站失败不降级为彻底删除。

    若文件位于快照根下一层归纳文件夹且删后该夹已空，尝试删除空文件夹。
    """
    from .fs_delete import DeleteError, delete_path, normalize_abs

    try:
        drop_cache_for(path)
    except Exception:  # noqa: BLE001 - 清会话缓存失败不影响删除本体
        pass
    abs_path = normalize_abs(path) if path else ""
    parent = os.path.dirname(abs_path) if abs_path else ""
    if abs_path:
        try:
            delete_path(abs_path, permanent=bool(permanent))
        except DeleteError as exc:
            code = str(exc.message or exc)
            # 与旧行为一致：目标已不存在时不报错
            if code == "missing":
                pass
            else:
                raise
        except FileNotFoundError:
            pass
    # 尝试清空归纳夹（仅 base 下一层）
    if not parent:
        return
    try:
        base = os.path.abspath(default_snapshot_dir())
        if (
            not _same_dir(parent, base)
            and _same_dir(os.path.dirname(parent), base)
            and os.path.isdir(parent)
            and not os.listdir(parent)
        ):
            os.rmdir(parent)
    except OSError:
        pass
