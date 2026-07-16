"""应用版本号 —— 唯一来源。
设置接口、检查更新等都从这里读，避免多处不一致。
"""

from __future__ import annotations

import re

__version__ = "1.1.0"

# GitHub 仓库（检查更新 / 发布页）
GITHUB_OWNER = "Kami958"
GITHUB_REPO = "WhoShitsonMyC"
GITHUB_RELEASES_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases"
GITHUB_LATEST_API = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
)


def normalize_version(raw: str | None) -> str:
    """去掉前后空白与可选前缀 v/V，便于比较。"""
    if raw is None:
        return ""
    s = str(raw).strip()
    if s[:1] in ("v", "V"):
        s = s[1:].strip()
    return s


def parse_version(raw: str | None) -> tuple[int, ...]:
    """把 ``1.2.3`` / ``v1.2.3-beta`` 解析为可比较的数字元组。

    非数字段忽略；无法解析时返回空元组。
    """
    s = normalize_version(raw)
    if not s:
        return ()
    # 取到第一个预发布分隔符之前（- / +）
    core = re.split(r"[-+]", s, maxsplit=1)[0]
    parts: list[int] = []
    for piece in core.split("."):
        m = re.match(r"^(\d+)", piece)
        if not m:
            break
        parts.append(int(m.group(1)))
    return tuple(parts)


def _pad_versions(
    a: tuple[int, ...], b: tuple[int, ...]
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """长度不同时右侧补 0：1.2 vs 1.2.0 视为相等。"""
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))


def is_remote_newer(remote: str | None, local: str | None) -> bool:
    """远端版本是否严格高于本机版本。"""
    return compare_versions(remote, local) > 0


def compare_versions(remote: str | None, local: str | None) -> int:
    """比较远端与本机版本。

    返回：
      ``1``  远端更新（有可升级版本）
      ``0``  相同
      ``-1`` 本机更高（开发版 / 超前发布）
    无法解析远端时视为 ``0``；仅本机无法解析时视为远端更新。
    """
    a = parse_version(remote)
    b = parse_version(local)
    if not a and not b:
        return 0
    if not a:
        return 0
    if not b:
        return 1
    a_pad, b_pad = _pad_versions(a, b)
    if a_pad > b_pad:
        return 1
    if a_pad < b_pad:
        return -1
    return 0
