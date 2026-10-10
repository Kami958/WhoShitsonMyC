"""把设置页里的日志开关同步到 applog。"""

from __future__ import annotations

from core import applog, store


def _diff_common_settings(before: dict, after: dict) -> list[str]:
    """对比通用设置，只返回变更字段的「名: 旧 -> 新」。

    不含 AI 节（AI 由 set_config 单独记 diff）。
    """
    keys = (
        "scan_workers",
        "compress_snapshots",
        "use_mft",
        "search_memory_index",
        "temp_cleanup",
        "log_sanitize",
        "log_level",
        "snapshot_dir_configured",
        "snapshot_dir",
        "delete_blacklist",
    )
    parts: list[str] = []
    for key in keys:
        if key == "delete_blacklist":
            b = before.get("delete_blacklist") or []
            a = after.get("delete_blacklist") or []
            if not isinstance(b, list):
                b = []
            if not isinstance(a, list):
                a = []
            # 规范化比较：path+mode

            def _bl_key(items: list) -> list[tuple[str, str]]:
                out: list[tuple[str, str]] = []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    out.append(
                        (
                            str(it.get("path") or "").strip().lower(),
                            str(it.get("mode") or "prefix").strip().lower(),
                        )
                    )
                out.sort()
                return out

            if _bl_key(b) == _bl_key(a):
                continue
        elif key in (
            "compress_snapshots",
            "use_mft",
            "search_memory_index",
            "temp_cleanup",
            "log_sanitize",
        ):
            if bool(before.get(key)) == bool(after.get(key)):
                continue
        elif key == "log_level":
            b = str(before.get(key) or "INFO").strip().upper()
            a = str(after.get(key) or "INFO").strip().upper()
            if b == a:
                continue
        elif key == "scan_workers":
            if int(before.get(key) or 0) == int(after.get(key) or 0):
                continue
        elif key in ("snapshot_dir", "snapshot_dir_configured"):
            b = str(before.get(key) or "").strip().rstrip("\\/").lower()
            a = str(after.get(key) or "").strip().rstrip("\\/").lower()
            if b == a:
                continue
        else:
            if before.get(key) == after.get(key):
                continue
        parts.append(
            f"{key}: {_settings_field_log_value(key, before)}"
            f" -> {_settings_field_log_value(key, after)}"
        )
    return parts


def _settings_field_log_value(key: str, data: dict) -> str:
    """通用设置字段的日志可读值。

    路径保持原文，是否脱敏由 ``applog.log_settings_changed`` 写入管线决定。
    """
    if key == "delete_blacklist":
        bl = data.get("delete_blacklist") or []
        n = len(bl) if isinstance(bl, list) else 0
        return f"{n} rules"
    if key in (
        "compress_snapshots",
        "use_mft",
        "search_memory_index",
        "temp_cleanup",
        "log_sanitize",
        "snapshot_dir_is_custom",
    ):
        return "true" if bool(data.get(key)) else "false"
    if key == "log_level":
        return str(data.get(key) or "INFO").strip().upper() or "INFO"
    if key == "scan_workers":
        return str(int(data.get(key) or 0))
    if key in ("snapshot_dir", "snapshot_dir_configured"):
        raw = str(data.get(key) or "").strip()
        if not raw:
            return "(builtin)" if key == "snapshot_dir_configured" else "-"
        return raw
    val = data.get(key)
    if val is None or val == "":
        return "-"
    return str(val)


def _sync_log_level_to_applog() -> str:
    """把日志最低等级同步到 applog。

    优先级：设置项显式值（yaml / 设置页）> 环境变量 ``WSMC_LOG_LEVEL`` / ``WSMC_DEBUG`` > 默认 INFO。
    返回生效后的等级名。
    """
    if store.is_log_level_explicit():
        level = store.get_log_level()
        return applog.set_min_level(level)
    # 未写设置：交给 applog 读环境（set_min_level(None)）
    level = applog.set_min_level(None)
    # 仅会话与 store 展示对齐，不写 yaml、不标 explicit
    store._log_level = level  # noqa: SLF001
    return level


def _sync_log_sanitize_to_applog() -> bool:
    """把路径脱敏开关同步到 applog。

    优先级：设置项显式值（yaml / 设置页）> 环境变量 ``WSMC_LOG_SANITIZE`` > 默认开。
    返回生效后的开关值。
    """
    if store.is_log_sanitize_explicit():
        enabled = store.get_log_sanitize()
    else:
        env = applog.env_log_sanitize()
        if env is None:
            enabled = store.get_log_sanitize()  # 默认 True
        else:
            enabled = env
            # 仅会话生效，不写 yaml；与 store 内存对齐便于 get_settings 展示
            store._log_sanitize = enabled  # noqa: SLF001
    return applog.set_sanitize_enabled(enabled)
