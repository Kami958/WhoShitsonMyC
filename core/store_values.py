"""设置项的取值归一：把 YAML / 前端传来的任意值收敛成可用值，并规定各项在 YAML 里怎么写。"""

from __future__ import annotations

import os
import re
from . import wingeom


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
    s = (
        str(val)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
    )
    return f'"{s}"'


def _yaml_unquote(val: str) -> str:
    """去掉一层引号并还原常见转义。"""
    s = (val or "").strip()
    if len(s) >= 2 and (
        (s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")
    ):
        s = s[1:-1]
    return (
        s.replace("\\n", "\n")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )


def _normalize_model_options(raw) -> list[str]:
    """模型 id 列表：接受 list，或逗号/换行分隔字符串。"""
    items: list = []
    if isinstance(raw, list):
        items = raw
    elif raw is None:
        items = []
    else:
        text = str(raw).replace("\r\n", "\n").replace("\r", "\n")
        if "," in text and "\n" not in text.strip():
            items = text.split(",")
        else:
            items = re.split(r"[\n,]+", text)
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        s = str(item or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= 500:
            break
    return out


def _model_options_to_yaml(opts: list[str]) -> str:
    return ",".join(_normalize_model_options(opts))


def _default_enabled_tools() -> list[str]:
    """默认启用的 tool 名（与 modules.ai.tools 目录一致）。"""
    try:
        from modules.ai.tools import default_enabled_tools

        return list(default_enabled_tools())
    except Exception:
        return ["propose_pending_delete"]


def _normalize_enabled_tools(raw, *, legacy_tools_enabled=None) -> list[str]:
    """收成合法已启用 tool 列表。

    - ``raw`` 为 list/逗号串时按目录过滤
    - ``raw is None`` 且 ``legacy_tools_enabled is False`` → 空列表
    - ``raw is None`` 否则 → 默认全部目录 tool
    """
    try:
        from modules.ai.tools import normalize_enabled_tools, default_enabled_tools
    except Exception:
        # 极早期导入失败时的兜底
        if raw is None:
            if legacy_tools_enabled is False:
                return []
            return ["propose_pending_delete"]
        if isinstance(raw, str):
            return [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
        if isinstance(raw, (list, tuple)):
            return [str(x).strip() for x in raw if str(x or "").strip()]
        return []

    if raw is None:
        if legacy_tools_enabled is False:
            return []
        return list(default_enabled_tools())
    return list(normalize_enabled_tools(raw))


def _enabled_tools_to_yaml(names: list[str]) -> str:
    return ",".join(_normalize_enabled_tools(names if names is not None else []))


def _normalize_delete_blacklist(raw) -> list[dict[str, str]]:
    """删除黑名单：委托 core.fs_delete，避免 store 膨胀正则细节。"""
    from core.fs_delete import normalize_delete_blacklist

    return normalize_delete_blacklist(raw)


def _delete_blacklist_to_yaml(entries: list) -> str:
    """序列化为 JSON 字符串，供 mini-YAML 标量写入。"""
    import json

    data = _normalize_delete_blacklist(entries)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _normalize_pinned_snapshots(raw) -> list[str]:
    """置顶快照列表：保留置顶顺序，去空白去重（Windows 路径不区分大小写）。"""
    import json

    if isinstance(raw, str):
        # 从 YAML 读回来的 JSON 字符串
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = []
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        p = str(item or "").strip()
        if not p:
            continue
        key = os.path.normcase(os.path.abspath(p))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _pinned_snapshots_to_yaml(paths: list) -> str:
    """同上：置顶列表序列化为 JSON 字符串。"""
    import json

    data = _normalize_pinned_snapshots(paths)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _normalize_theme(raw: str) -> str:
    """主题偏好：light|dark|auto；非法值回落 light。"""
    v = str(raw or "").strip().lower()
    if v in ("dark", "auto"):
        return v
    return "light"


def resolve_theme(preference: str) -> str:
    """把主题偏好解析成实际主题：auto 读系统深浅色，失败默认 light。"""
    pref = _normalize_theme(preference)
    if pref != "auto":
        return pref
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if int(val) else "dark"
    except Exception:
        return "light"


def _normalize_ui_hover_group_min(raw) -> int:
    """悬停分组框最少行数阈值：0-50 收拢，非法回落 2。"""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 2
    return max(0, min(50, n))


def _normalize_ui_tool_panel_width(raw) -> int:
    """侧边栏宽度（像素）：夹到 240-640，非法回落 340。

    区间与默认值的权威定义在 ``core.wingeom``，前端 pending.js 另有同名常量。
    """
    return wingeom.clamp_panel_width(raw)


def _normalize_log_level(raw) -> str:
    """日志等级：DEBUG|INFO|WARN|ERROR；WARNING 视作 WARN；非法回落 INFO。"""
    s = str(raw or "").strip().upper()
    if s == "WARNING":
        s = "WARN"
    if s in ("DEBUG", "INFO", "WARN", "ERROR"):
        return s
    return "INFO"


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


def _normalize_cleanup_max_depth(val, default: int | None = None) -> int:
    """收成合法清理深度；失败用 default 或内置默认。"""
    fb = (
        int(_AI_DEFAULTS["cleanup_max_depth"])
        if default is None
        else int(default)
    )
    n = _as_int(val, None)
    if n is None:
        return fb
    if n < _AI_CLEANUP_DEPTH_MIN:
        return _AI_CLEANUP_DEPTH_MIN
    if n > _AI_CLEANUP_DEPTH_MAX:
        return _AI_CLEANUP_DEPTH_MAX
    return n


def _as_bool(val, default: bool | None = None) -> bool | None:
    """把配置值收成 bool；无法识别时返回 default。

    注意：不能用内置 ``bool("false")``（非空字符串恒为 True）。
    """
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return bool(val)
    parsed = _parse_bool(str(val))
    return default if parsed is None else parsed


def _as_int(val, default: int | None = None) -> int | None:
    """把配置值收成 int；失败返回 default。"""
    if isinstance(val, bool):
        return default
    if isinstance(val, int):
        return val
    if val is None:
        return default
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return default


def _as_str(val, default: str = "") -> str:
    if val is None:
        return default
    return str(val)


def _put_raw_pair(out: dict, key: str, val: str) -> None:
    """解析层：任意 key 都收下，值统一去引号后的字符串。"""
    k = (key or "").strip()
    if not k:
        return
    out[k] = _yaml_unquote((val or "").strip())


def _normalize_window_size(width, height) -> tuple[int, int]:
    """窗口宽高收到合理范围（与 create_window min_size 对齐）。"""
    try:
        w = int(width)
    except (TypeError, ValueError):
        w = 1100
    try:
        h = int(height)
    except (TypeError, ValueError):
        h = 720
    w = max(820, min(w, 10000))
    h = max(560, min(h, 10000))
    return w, h


_WORKERS_MIN, _WORKERS_MAX = 1, 128


_WORKERS_MIN, _WORKERS_MAX = 1, 128


# 清理深度允许范围（防手滑）
_AI_CLEANUP_DEPTH_MIN, _AI_CLEANUP_DEPTH_MAX = 1, 8


# 清理深度允许范围（防手滑）
_AI_CLEANUP_DEPTH_MIN, _AI_CLEANUP_DEPTH_MAX = 1, 8


_AI_DEFAULTS: dict = {
    "enabled": False,
    "base_url": "https://api.openai.com/v1",
    "model": "",
    "api_key": "",
    "extra_prompt": "",
    "consented": False,
    "model_options": [],
    # AI 清理：从起点往下展开的最大层数（seed 为 0）
    "cleanup_max_depth": 3,
    # 用户启用的 tool 名列表（逗号 YAML）；空列表 = 不注入任何 tool
    "enabled_tools": None,  # None = 默认启用目录内全部 tool
}
