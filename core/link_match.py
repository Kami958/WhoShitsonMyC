"""判断目录联接、符号链接是否指向某个目录：候选判定与目标比较的共用规则。

规则只此一份，两处调用：读快照库查（:meth:`core.differ.Diff.links_to_abs`）
和扫盘时边扫边判（:class:`core.link_find.LinkFind`）。两处各写一套的话，
迟早会出现「同一台机器上用快照查得到、扫盘查不到」这种对不上的结果。

判定分三步：先看这一行算不算候选（标签是目录联接或符号链接、目标读得出来、
不是卷 GUID），再把目标归一化成可比较的形式，最后和要查的目录路径比一次。

两条通道唯一的不同是「链接自己在哪」：扫盘时每个链接的相对路径当场就有，
读库时要沿 parent_id 查谱系才知道。位置只影响相对目标的展开和结果展示。
"""

from __future__ import annotations

import os

from .models import (
    REPARSE_TAG_MOUNT_POINT,
    REPARSE_TAG_SYMLINK,
    clean_link_target,
    reparse_kind,
)


def link_norm(p: str) -> str:
    """把链接目标或扫描根归一化成可比较形式。

    依次剥掉内核前缀、把正斜杠统一成反斜杠、折叠重复分隔符与 ``.``、``..``
    段、去掉尾随分隔符、按 Windows 规则小写。折叠这一步不能少：调用方拼
    路径时多写一个分隔符，归一化后就和另一侧记录的不相等，整条链接都漏掉。
    幂等：``clean_link_target`` 对已剥过的串无副作用。
    """
    s = clean_link_target(p or "").replace("/", "\\")
    if not s:
        return ""
    return os.path.normcase(os.path.normpath(s).rstrip("\\"))


def is_abs_target(t: str) -> bool:
    r"""归一化后的目标是否为绝对路径（``c:\...`` 或 ``\\server\...``）。"""
    return t.startswith("\\\\") or (
        len(t) >= 2 and t[1] == ":" and t[0].isalpha()
    )


def abs_under_root(root: str, rel: str) -> str:
    """把相对扫描根的路径拼成绝对路径。

    ``rel`` 为空串时返回扫描根本身。只做拼接，不统一大小写与分隔符：
    拼出来的串要拿来展示，比较前统一过一遍 :func:`link_norm`。盘根写法
    ``C:\\`` 去掉尾随分隔符后是 ``C:``，补一个分隔符正好拼成 ``C:\\tail``，
    不会拼成 ``C:tail``。
    """
    r = (root or "").strip()
    if not r:
        return (rel or "").strip()
    r = r.rstrip("/\\")
    tail = (rel or "").strip().strip("/\\")
    if not tail:
        return r
    return r + os.sep + tail


def link_candidate(tag: int, raw_target: str) -> tuple[str, str] | None:
    """判定一行重解析点算不算候选链接，算则返回 ``(类型, 归一化目标)``。

    只认目录联接与符号链接两种标签：MFT 那条采集路径不校验标签布局，
    OneDrive、WSL 这类标签的字节能被读成垃圾目标写进 ``link_target``。
    目标读不出来（空串）的也不在候选里，比不出结果。卷 GUID 指向的是卷
    而不是目录，与要查的目录永远不可能相等，直接排除。

    Returns:
        ``(kind, t)``：``kind`` 取 ``"junction"`` / ``"symlink"``，``t`` 是
        归一化后的目标；不是候选返回 ``None``。
    """
    tag = int(tag or 0)
    if tag not in (REPARSE_TAG_MOUNT_POINT, REPARSE_TAG_SYMLINK):
        return None
    t = link_norm(raw_target)
    if not t or t.startswith("volume{"):
        return None
    return reparse_kind(tag), t


def target_matches(link_abs: str, t: str, target: str) -> bool:
    """归一化目标 ``t`` 是否指向 ``target``。

    Args:
        link_abs: 链接自身的绝对路径；绝对目标用不上，可传空串。
        t: :func:`link_candidate` 给出的归一化目标。
        target: 归一化后的待查目录路径。

    ``mklink`` 建的相对符号链接目标是 ``..\\foo`` 这种形态，要先把链接自己
    的位置拼上再比，不展开会整片漏报。
    """
    if is_abs_target(t):
        return t == target
    if not link_abs:
        return False
    combined = os.path.normpath(os.path.join(os.path.dirname(link_abs), t))
    return link_norm(combined) == target


def link_hit(
    root: str, target: str, link_rel: str, tag: int, raw_target: str
) -> dict | None:
    """扫盘途中判一条链接是否指向待查目录，命中返回一条结果行。

    扫盘时链接自己的位置和它指向的目标都在手里，判完就能出结果，不需要
    先落一份库再回头查。

    Args:
        root: 正在扫的盘或目录（绝对路径）。
        target: 已归一化的待查目录路径（:func:`link_norm` 的结果）。
        link_rel: 链接自身相对 ``root`` 的路径。
        tag: 这一行的重解析点标签。
        raw_target: 从磁盘读到的原始目标。

    Returns:
        命中返回 ``{"path": 绝对路径, "kind": 类型, "target": 原始目标}``，
        与读库那条通道出的一样；不是候选或没命中返回 ``None``。
    """
    cand = link_candidate(tag, raw_target)
    if cand is None:
        return None
    kind, t = cand
    link_abs = abs_under_root(root, link_rel)
    if not target_matches(link_abs, t, target):
        return None
    return {
        "path": link_abs,
        "kind": kind,
        "target": raw_target,
    }
