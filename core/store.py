"""快照的存放位置、命名与列举。

负责「快照存哪、叫什么名、有哪些」这类应用层事务，与纯遍历/对比逻辑分开。
快照固定存于用户数据目录（Windows 为
``%LOCALAPPDATA%\\WhoShitsOnMyC\\snapshots``），其它平台回落到
``~/.local/share`` 或 ``~``，不提供迁移——位置唯一、行为可预期。

应用设置默认不落盘：无 ``settings.yaml`` 时用内置默认。
用户改过设置（设置页点「完成」、或切换语言/主题等）后自动写入该文件；
「恢复默认」删除文件并把内存值重置为内置默认。
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from .compress import (
    drop_cache_for,
    is_compressed_path,
    is_snapshot_filename,
    read_meta_any,
    write_snapshot_note,
)
from .snapshot import SnapshotError

_APP_DIR_NAME = "WhoShitsOnMyC"
_SETTINGS_FILE = "settings.yaml"
# 备注最大长度（字符）；写入快照文件内的 meta，过长截断。
_NOTE_MAX_LEN = 200
# 归纳文件夹名最大长度（字符）。
_FOLDER_MAX_LEN = 64
# 文件名中不安全的字符统一替换为下划线。
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
# 归纳文件夹名：允许中文与常见字符，禁止路径分隔与控制字符。
_FOLDER_BAD_RE = re.compile(r'[\x00-\x1f\\/:*?"<>|]')

# 扫描线程数的允许范围（上限防手滑，不是性能上限）。
_WORKERS_MIN, _WORKERS_MAX = 1, 128


class StoreError(Exception):
    """快照存储管理相关错误。"""


def is_rotational_drive(drive_letter: str | None = None) -> bool | None:
    """粗测盘符是否为机械盘（Windows SeekPenalty）。

    Args:
        drive_letter: ``'C'`` / ``'C:'`` / ``None``。``None`` 时查系统盘。

    Returns:
        True: 旋转介质（HDD）
        False: 非旋转（SSD/NVMe 等）
        None: 无法判断（非 Windows / 查询失败）
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        if drive_letter:
            letter = drive_letter.strip().rstrip(":\\/").upper()
            if len(letter) != 1 or not ("A" <= letter <= "Z"):
                return None
            root = letter + ":"
        else:
            sys_dir = ctypes.create_unicode_buffer(260)
            if not kernel32.GetSystemDirectoryW(sys_dir, 260):
                return None
            root = os.path.splitdrive(sys_dir.value)[0]
            if not root:
                return None
        volume_path = "\\\\.\\" + root.rstrip("\\/")

        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_EXISTING = 3
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        handle = kernel32.CreateFileW(
            volume_path,
            0,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle == INVALID_HANDLE_VALUE or handle is None:
            handle = kernel32.CreateFileW(
                volume_path,
                GENERIC_READ,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                0,
                None,
            )
        if handle == INVALID_HANDLE_VALUE or handle is None:
            return None

        try:
            # IOCTL_STORAGE_QUERY_PROPERTY + StorageDeviceSeekPenaltyProperty
            IOCTL = 0x002D1400

            class STORAGE_PROPERTY_QUERY(ctypes.Structure):
                _fields_ = [
                    ("PropertyId", wintypes.DWORD),
                    ("QueryType", wintypes.DWORD),
                    ("AdditionalParameters", ctypes.c_byte * 1),
                ]

            class DEVICE_SEEK_PENALTY_DESCRIPTOR(ctypes.Structure):
                _fields_ = [
                    ("Version", wintypes.DWORD),
                    ("Size", wintypes.DWORD),
                    ("IncursSeekPenalty", wintypes.BOOLEAN),
                ]

            query = STORAGE_PROPERTY_QUERY()
            query.PropertyId = 7  # StorageDeviceSeekPenaltyProperty
            query.QueryType = 0
            seek = DEVICE_SEEK_PENALTY_DESCRIPTOR()
            returned = wintypes.DWORD(0)
            ok = kernel32.DeviceIoControl(
                handle,
                IOCTL,
                ctypes.byref(query),
                ctypes.sizeof(query),
                ctypes.byref(seek),
                ctypes.sizeof(seek),
                ctypes.byref(returned),
                None,
            )
            if not ok:
                return None
            return bool(seek.IncursSeekPenalty)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - 启发式失败则退回默认
        return None


def _is_rotational_system_drive() -> bool | None:
    """系统盘是否机械盘（:func:`default_scan_workers` 用）。"""
    return is_rotational_drive(None)


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
        }


# 卸载 wipe 后置位：禁止再 makedirs 把 WhoShitsOnMyC 建回来（直到进程退出）
_data_wiped = False


def _app_base_dir_path() -> str:
    """应用数据根路径（**不**创建目录）。"""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get(
            "XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share")
        )
    return os.path.join(base, _APP_DIR_NAME)


def _app_base_dir() -> str:
    """返回应用数据根目录（必要时创建）。默认快照目录就在这。

    卸载 wipe 后本会话内**不再**创建，以免空文件夹又出现。
    """
    path = _app_base_dir_path()
    if not _data_wiped:
        os.makedirs(path, exist_ok=True)
    return path


def builtin_snapshot_dir() -> str:
    """内置快照目录（应用数据根下的 ``snapshots``，必要时创建）。"""
    path = os.path.join(_app_base_dir(), "snapshots")
    if not _data_wiped:
        os.makedirs(path, exist_ok=True)
    return path


def default_snapshot_dir() -> str:
    """当前生效的快照存放目录（自定义或内置；必要时创建）。"""
    return get_snapshot_dir()


# ---- 应用设置（内存 + 可选 YAML 持久化）--------------------------------


def default_scan_workers() -> int:
    """扫描线程数默认值。

    - 探测到**机械盘**（寻道惩罚）→ ``1``（多线程易抖磁头）
    - SSD / 无法判断 → ``max(1, CPU 核数)``，不人为封顶
      （设置页仍可手动改；上限见 :func:`set_scan_workers`）
    """
    cpu = max(1, os.cpu_count() or 1)
    if _is_rotational_system_drive() is True:
        return 1
    return cpu


def settings_path() -> str:
    """``settings.yaml`` 绝对路径（位于应用数据根）。"""
    return os.path.join(_app_base_dir(), _SETTINGS_FILE)


def _clamp_workers(n: int) -> int:
    return max(_WORKERS_MIN, min(_WORKERS_MAX, int(n)))


def _parse_bool(raw: str) -> bool | None:
    s = raw.strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None


def _yaml_quote(val: str) -> str:
    """简单双引号转义，供路径/字符串写入 YAML。"""
    s = str(val).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _normalize_theme(raw: str) -> str:
    return "dark" if str(raw).strip().lower() == "dark" else "light"


def _normalize_lang(raw: str) -> str:
    return "zh" if str(raw).strip().lower() == "zh" else "en"


def _normalize_snapshot_dir(raw: str | None) -> str:
    """空串表示用内置目录；否则返回绝对路径（不强制目录已存在）。"""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    return os.path.abspath(os.path.expanduser(s))


_KNOWN_SETTING_KEYS = frozenset(
    {
        "persist",
        "scan_workers",
        "compress_snapshots",
        "use_mft",
        "lang",
        "theme",
        "snapshot_dir",
    }
)


def _parse_setting_pair(key: str, val: str, out: dict) -> None:
    """把一行 key/value 写入 ``out``（仅识别已知键）。"""
    key = (key or "").strip()
    if key not in _KNOWN_SETTING_KEYS:
        return
    val = (val or "").strip()
    if len(val) >= 2 and (
        (val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")
    ):
        inner = val[1:-1]
        val = inner.replace('\\"', '"').replace("\\\\", "\\")
    if key == "scan_workers":
        try:
            out[key] = int(val)
        except ValueError:
            pass
    elif key in ("compress_snapshots", "use_mft", "persist"):
        b = _parse_bool(val)
        if b is not None:
            out[key] = b
    elif key == "lang":
        out[key] = _normalize_lang(val)
    elif key == "theme":
        out[key] = _normalize_theme(val)
    elif key == "snapshot_dir":
        out[key] = _normalize_snapshot_dir(val)


def _load_settings_yaml(path: str) -> dict:
    """读 settings.yaml。

    支持两种形态（结果都是**扁平** dict，供 :func:`_apply_loaded` 使用）::

        # 新：按页签分节
        common:
          persist: true
          scan_workers: 4

        # 旧：顶层扁平（兼容）
        persist: true
        scan_workers: 4

    忽略注释与空行。失败返回空 dict。
    """
    out: dict = {}
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return out

    # 当前节：None = 顶层扁平；"common" = 通用设置页
    section: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        # 仅接受空格缩进（YAML 惯例）
        indent = len(raw) - len(raw.lstrip(" "))
        s = raw.strip()
        if ":" not in s:
            continue
        key, _, rest = s.partition(":")
        key = key.strip()
        val = rest.strip()
        if indent == 0 and not val and key:
            # 顶层节名，如 common:
            section = key.lower()
            continue
        if indent == 0 and val:
            # 旧扁平：顶层 key: value
            section = None
            _parse_setting_pair(key, val, out)
            continue
        if indent > 0:
            # 节内键：目前只认 common
            if section == "common" or section is None:
                _parse_setting_pair(key, val, out)
    return out


def _write_settings_yaml(path: str, data: dict) -> None:
    """写 settings.yaml（``common:`` 顶层节，对应设置页「通用」）。"""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    snap = data.get("snapshot_dir") or ""
    lines = [
        "# WhoShitsOnMyC settings — auto-written when settings change",
        "# Sections map to settings tabs (common = 通用).",
        "# Missing keys use built-in defaults on load.",
        "common:",
        f"  scan_workers: {int(data['scan_workers'])}",
        f"  compress_snapshots: {'true' if data.get('compress_snapshots') else 'false'}",
        f"  use_mft: {'true' if data.get('use_mft') else 'false'}",
        f"  lang: {_normalize_lang(data.get('lang', 'en'))}",
        f"  theme: {_normalize_theme(data.get('theme', 'light'))}",
        f"  snapshot_dir: {_yaml_quote(snap)}",
        "",
    ]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))
    os.replace(tmp, path)


def _delete_settings_yaml() -> None:
    path = settings_path()
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


# 当前会话值。import 时先落默认，再若默认路径存在 settings.yaml 则按键覆盖。
_scan_workers = default_scan_workers()
_compress_snapshots = True
_use_mft = True  # 默认开：盘符根 + NTFS 优先 MFT，失败回退 scandir
_lang = "en"  # 启动时 app 会按系统语言再设；YAML 优先覆盖
_theme = "light"
# 自定义快照目录；空串 = 使用 builtin_snapshot_dir()
_snapshot_dir = ""


def _apply_loaded(data: dict) -> None:
    """用 YAML 字典覆盖内存设置。

    - 文件不存在 / 空 dict：全部保持调用前的默认值。
    - 文件存在：只覆盖出现的键；未写的项继续用默认（或调用前已设值）。
    - 旧文件中的 ``persist`` 键忽略（已不再使用手动持久化开关）。
    """
    global _scan_workers, _compress_snapshots, _use_mft
    global _lang, _theme, _snapshot_dir
    if not data:
        return
    if "scan_workers" in data:
        _scan_workers = _clamp_workers(data["scan_workers"])
    if "compress_snapshots" in data:
        _compress_snapshots = bool(data["compress_snapshots"])
    if "use_mft" in data:
        _use_mft = bool(data["use_mft"])
    if "lang" in data:
        _lang = _normalize_lang(data["lang"])
    if "theme" in data:
        _theme = _normalize_theme(data["theme"])
    if "snapshot_dir" in data:
        _snapshot_dir = _normalize_snapshot_dir(data["snapshot_dir"])


def _settings_payload() -> dict:
    """当前内存设置（写 YAML / API 共用）。"""
    return {
        "scan_workers": _scan_workers,
        "compress_snapshots": _compress_snapshots,
        "use_mft": _use_mft,
        "lang": _lang,
        "theme": _theme,
        "snapshot_dir": _snapshot_dir,
    }


def _persist() -> None:
    """把当前内存全部设置写回 YAML（改设置后自动调用）。"""
    _write_settings_yaml(settings_path(), _settings_payload())


def reload_settings_from_disk() -> None:
    """从默认路径的 ``settings.yaml`` 重新加载。

    无文件则不改动内存；有文件则按键覆盖，缺项保留当前/默认值。
    """
    data = _load_settings_yaml(settings_path())
    _apply_loaded(data)


# 模块加载时：默认路径有 yaml 则优先读入
_apply_loaded(_load_settings_yaml(settings_path()))


def get_scan_workers() -> int:
    """返回当前生效的扫描线程数（默认 :func:`default_scan_workers`）。"""
    return _scan_workers


def set_scan_workers(n: int) -> int:
    """设置扫描线程数（越界收拢），返回生效值；值变化时写 YAML。"""
    global _scan_workers
    new = _clamp_workers(n)
    if new != _scan_workers:
        _scan_workers = new
        _persist()
    return _scan_workers


def get_compress_snapshots() -> bool:
    """返回是否在扫描完成后压缩快照（默认 True）。"""
    return _compress_snapshots


def set_compress_snapshots(enabled: bool) -> bool:
    """设置是否压缩快照；值变化时写 YAML。"""
    global _compress_snapshots
    new = bool(enabled)
    if new != _compress_snapshots:
        _compress_snapshots = new
        _persist()
    return _compress_snapshots


def get_use_mft() -> bool:
    """是否对盘符根 NTFS 尝试 MFT 快路径（默认 True）。"""
    return _use_mft


def set_use_mft(enabled: bool) -> bool:
    """设置是否尝试 MFT；值变化时写 YAML。"""
    global _use_mft
    new = bool(enabled)
    if new != _use_mft:
        _use_mft = new
        _persist()
    return _use_mft


def get_lang() -> str:
    """界面语言（``zh`` / ``en``），供 YAML 与启动恢复。"""
    return _lang


def set_lang(lang: str) -> str:
    """设置界面语言；值变化时写 YAML。"""
    global _lang
    new = _normalize_lang(lang)
    if new != _lang:
        _lang = new
        _persist()
    return _lang


def get_theme() -> str:
    """界面主题（``dark`` / ``light``）。"""
    return _theme


def set_theme(theme: str) -> str:
    """设置主题；值变化时写 YAML。"""
    global _theme
    new = _normalize_theme(theme)
    if new != _theme:
        _theme = new
        _persist()
    return _theme


def get_snapshot_dir() -> str:
    """当前生效的快照目录（自定义或内置；必要时创建）。"""
    if _snapshot_dir:
        path = _snapshot_dir
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            # 自定义路径不可用时回退内置，避免扫描/列举直接炸
            return builtin_snapshot_dir()
        return path
    return builtin_snapshot_dir()


def get_snapshot_dir_configured() -> str:
    """用户配置的自定义路径；空串表示使用内置目录。"""
    return _snapshot_dir


def set_snapshot_dir(path: str | None) -> str:
    """设置快照存放目录。

    空 / None → 恢复内置目录。非空路径会 abspath 并尝试创建。
    返回**生效**的绝对路径（内置或自定义）。值变化时写 YAML。
    """
    global _snapshot_dir
    raw = _normalize_snapshot_dir(path)
    if not raw:
        if _snapshot_dir:
            _snapshot_dir = ""
            _persist()
        return builtin_snapshot_dir()
    try:
        os.makedirs(raw, exist_ok=True)
    except OSError as exc:
        raise OSError(f"cannot create snapshot dir: {raw} ({exc})") from exc
    if not os.path.isdir(raw):
        raise OSError(f"snapshot dir is not a directory: {raw}")
    if raw != _snapshot_dir:
        _snapshot_dir = raw
        _persist()
    return _snapshot_dir


def _same_dir(a: str, b: str) -> bool:
    """两路径是否同一目录（规范化后比较）。"""
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(
            os.path.abspath(b)
        )
    except OSError:
        return False


def migrate_snapshots(
    src_dir: str,
    dst_dir: str,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """把 ``src_dir`` 下的快照文件迁到 ``dst_dir``。

    移动 ``.db`` / ``.dbz``，含快照根下一层归纳子目录中的文件
    （保留相对一层文件夹名）；目标已有同名文件则跳过（不覆盖）。
    返回 ``{moved, skipped, failed, errors, total}``。

    ``progress`` 可选，每处理一个文件回调一次，参数为::

        {done, total, name, status, moved, skipped, failed}
        status ∈ moved | skipped | failed
    """
    result: dict = {
        "moved": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
        "total": 0,
    }
    if not src_dir or not dst_dir or _same_dir(src_dir, dst_dir):
        return result
    if not os.path.isdir(src_dir):
        return result
    try:
        os.makedirs(dst_dir, exist_ok=True)
    except OSError as exc:
        result["failed"] += 1
        result["errors"].append(str(exc))
        return result

    try:
        names = os.listdir(src_dir)
    except OSError as exc:
        result["failed"] += 1
        result["errors"].append(str(exc))
        return result

    # (显示名, src, dst)；dst 含一层子目录时需先 makedirs
    jobs: list[tuple[str, str, str]] = []
    for name in names:
        src = os.path.join(src_dir, name)
        try:
            is_file = os.path.isfile(src)
            is_dir = os.path.isdir(src) and not os.path.islink(src)
        except OSError:
            continue
        if is_file and is_snapshot_filename(name):
            jobs.append((name, src, os.path.join(dst_dir, name)))
            continue
        if not is_dir:
            continue
        try:
            folder = sanitize_folder_name(name)
        except ValueError:
            continue
        if not folder or folder != name:
            continue
        try:
            children = os.listdir(src)
        except OSError:
            continue
        for child in children:
            if not is_snapshot_filename(child):
                continue
            child_src = os.path.join(src, child)
            if not os.path.isfile(child_src):
                continue
            jobs.append(
                (
                    f"{folder}/{child}",
                    child_src,
                    os.path.join(dst_dir, folder, child),
                )
            )
    total = len(jobs)
    result["total"] = total
    if progress and total:
        progress(
            {
                "done": 0,
                "total": total,
                "name": "",
                "status": "start",
                "moved": 0,
                "skipped": 0,
                "failed": 0,
            }
        )

    for i, (name, src, dst) in enumerate(jobs, start=1):
        status = "moved"
        if os.path.exists(dst):
            result["skipped"] += 1
            status = "skipped"
        else:
            try:
                parent = os.path.dirname(dst)
                if parent and not os.path.isdir(parent):
                    os.makedirs(parent, exist_ok=True)
                # 先尝试 rename（同卷快）；跨卷失败再 copy+remove
                try:
                    os.replace(src, dst)
                except OSError:
                    import shutil

                    shutil.copy2(src, dst)
                    try:
                        drop_cache_for(src)
                    except Exception:  # noqa: BLE001
                        pass
                    os.remove(src)
                else:
                    try:
                        drop_cache_for(src)
                    except Exception:  # noqa: BLE001
                        pass
                result["moved"] += 1
                status = "moved"
                # 源在一层子目录且已空 → 删空夹
                try:
                    src_parent = os.path.dirname(src)
                    if (
                        not _same_dir(src_parent, src_dir)
                        and _same_dir(os.path.dirname(src_parent), src_dir)
                        and os.path.isdir(src_parent)
                        and not os.listdir(src_parent)
                    ):
                        os.rmdir(src_parent)
                except OSError:
                    pass
            except OSError as exc:
                result["failed"] += 1
                result["errors"].append(f"{name}: {exc}")
                status = "failed"
        if progress:
            progress(
                {
                    "done": i,
                    "total": total,
                    "name": name,
                    "status": status,
                    "moved": result["moved"],
                    "skipped": result["skipped"],
                    "failed": result["failed"],
                }
            )
    return result


def apply_settings(
    payload: dict,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """一次性应用多条设置（供设置页点「完成」时统一提交）。

    先更新内存中的各项，再写 ``settings.yaml``（设置页提交即自动持久化）。
    忽略旧版 ``persist_settings`` 键。

    若 ``snapshot_dir`` 变更，会把**原目录**中的 ``.db``/``.dbz`` 迁到新目录。
    ``progress`` 会在迁移过程中被调用（见 :func:`migrate_snapshots`）。

    可识别键：``scan_workers``、``compress_snapshots``、``use_mft``、
    ``snapshot_dir``（空串=内置目录）。缺省键保持当前值。
    """
    global _scan_workers, _compress_snapshots, _use_mft
    global _snapshot_dir

    if not isinstance(payload, dict):
        payload = {}

    old_snap_dir = get_snapshot_dir()
    dir_changed = False

    if "scan_workers" in payload:
        _scan_workers = _clamp_workers(payload["scan_workers"])
    if "compress_snapshots" in payload:
        _compress_snapshots = bool(payload["compress_snapshots"])
    if "use_mft" in payload:
        _use_mft = bool(payload["use_mft"])
    if "snapshot_dir" in payload:
        raw = _normalize_snapshot_dir(payload.get("snapshot_dir"))
        if not raw:
            _snapshot_dir = ""
        else:
            try:
                os.makedirs(raw, exist_ok=True)
            except OSError as exc:
                raise OSError(f"cannot create snapshot dir: {raw} ({exc})") from exc
            if not os.path.isdir(raw):
                raise OSError(f"snapshot dir is not a directory: {raw}")
            _snapshot_dir = raw
        dir_changed = not _same_dir(old_snap_dir, get_snapshot_dir())

    migrate_info: dict = {
        "moved": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
        "total": 0,
    }
    if dir_changed:
        new_dir = get_snapshot_dir()
        migrate_info = migrate_snapshots(old_snap_dir, new_dir, progress=progress)

    _persist()
    out = settings_dict()
    out["snapshot_dir_changed"] = dir_changed
    out["migrate"] = migrate_info
    return out


def reset_settings_to_defaults(*, lang: str | None = None) -> dict:
    """恢复内置默认设置并删除 ``settings.yaml``。

    不删除快照文件，仅清空自定义快照目录配置（回到内置目录）。
    ``lang`` 由调用方传入冷启动默认语言（系统语言）；省略则 ``en``。
    """
    global _scan_workers, _compress_snapshots, _use_mft
    global _lang, _theme, _snapshot_dir

    _scan_workers = default_scan_workers()
    _compress_snapshots = True
    _use_mft = True
    _theme = "light"
    _snapshot_dir = ""
    _lang = _normalize_lang(lang) if lang is not None else "en"
    _delete_settings_yaml()
    return settings_dict()


def app_data_dir() -> str:
    """应用数据根（``%LOCALAPPDATA%\\WhoShitsOnMyC`` 等）。"""
    return _app_base_dir()


def wipe_app_data(*, delete_data: bool = True) -> dict:
    """卸载清理：仅处理应用数据目录，不碰用户自定义快照路径。

    - ``delete_data=True``（默认）：删除整个应用数据文件夹
      （``WhoShitsOnMyC`` 目录本身及其内 snapshots、settings.yaml 等）。
      用 ``_app_base_dir_path()``，**不会**先 makedirs 再删。
    - ``delete_data=False``：仅删除 ``settings.yaml``（若存在）。

    **不会**删除用户自选的外部快照目录（即使其中有 .db/.dbz）。
    **不**删除程序本体。

    返回 ``{ok, deleted_data, path, removed, errors}``。
    """
    import shutil

    global _snapshot_dir, _data_wiped

    # 切勿调用 _app_base_dir()：它会 makedirs 把刚要删的目录又建出来
    base = _app_base_dir_path()
    result: dict = {
        "ok": True,
        "deleted_data": bool(delete_data),
        "path": base,
        "removed": [],
        "errors": [],
    }
    if delete_data:
        # 整夹删掉 WhoShitsOnMyC（含子目录）；失败则逐项清再尝试 rmdir
        try:
            if os.path.isdir(base):
                try:
                    shutil.rmtree(base)
                    result["removed"].append(
                        os.path.basename(base.rstrip("\\/")) or base
                    )
                except OSError:
                    for name in os.listdir(base):
                        p = os.path.join(base, name)
                        try:
                            if os.path.isdir(p) and not os.path.islink(p):
                                shutil.rmtree(p)
                            else:
                                os.remove(p)
                            result["removed"].append(name)
                        except OSError as exc:
                            result["ok"] = False
                            result["errors"].append(f"{name}: {exc}")
                    try:
                        os.rmdir(base)
                        result["removed"].append(
                            os.path.basename(base.rstrip("\\/")) or base
                        )
                    except OSError as exc:
                        result["ok"] = False
                        result["errors"].append(f"rmdir: {exc}")
            elif os.path.exists(base):
                try:
                    os.remove(base)
                    result["removed"].append(
                        os.path.basename(base.rstrip("\\/")) or base
                    )
                except OSError as exc:
                    result["ok"] = False
                    result["errors"].append(str(exc))
        except OSError as exc:
            result["ok"] = False
            result["errors"].append(str(exc))

        # 内存设置回默认，避免继续写已删目录；自定义路径配置一并忘掉
        _snapshot_dir = ""
        # 本会话禁止再 makedirs 重建应用数据根
        _data_wiped = True
    else:
        # settings_path() 会经 _app_base_dir 创建目录——仅在文件可能存在时再碰路径
        conf = os.path.join(base, _SETTINGS_FILE)
        try:
            if os.path.isfile(conf):
                os.remove(conf)
                result["removed"].append(_SETTINGS_FILE)
        except OSError as exc:
            result["ok"] = False
            result["errors"].append(str(exc))
    return result


def settings_dict() -> dict:
    """当前设置快照（供 API / 测试）。"""
    return {
        "scan_workers": _scan_workers,
        "compress_snapshots": _compress_snapshots,
        "use_mft": _use_mft,
        "settings_path": settings_path(),
        "settings_file_exists": os.path.isfile(settings_path()),
        "app_data_dir": app_data_dir(),
        "lang": _lang,
        "theme": _theme,
        "snapshot_dir": get_snapshot_dir(),
        "snapshot_dir_configured": _snapshot_dir,
        "snapshot_dir_builtin": builtin_snapshot_dir(),
        "snapshot_dir_is_custom": bool(_snapshot_dir),
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
) -> None:
    """删除快照根下的一层归纳文件夹。

    默认仅允许空目录；``force=True`` 时连同其中快照文件一并删除
    （不递归删除更深子目录以外的内容——本产品只维护一层）。
    """
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
    if not force and names:
        raise OSError("folder is not empty")
    if force:
        for n in names:
            p = os.path.join(path, n)
            if os.path.isfile(p) and is_snapshot_filename(n):
                delete_snapshot(p)
            elif os.path.isdir(p) and not os.path.islink(p):
                # 不递归清深层；若有意外子目录则拒绝
                raise OSError(f"unexpected subfolder: {n}")
            else:
                try:
                    os.remove(p)
                except OSError as exc:
                    raise OSError(f"cannot remove {n}: {exc}") from exc
        names = os.listdir(path) if os.path.isdir(path) else []
        if names:
            raise OSError("folder is not empty")
    os.rmdir(path)


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


def delete_snapshot(path: str) -> None:
    """删除一个快照文件；若是 ``.dbz``，顺带清掉本进程解压临时文件。文件不存在时静默返回。

    若文件位于快照根下一层归纳文件夹且删后该夹已空，尝试删除空文件夹。
    """
    try:
        drop_cache_for(path)
    except Exception:  # noqa: BLE001 - 清会话缓存失败不影响删除本体
        pass
    abs_path = os.path.abspath(path) if path else ""
    parent = os.path.dirname(abs_path) if abs_path else ""
    try:
        os.remove(path)
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
