"""AI 上下文 Packing：右键固定一层 vs 清理有限递归 + 多切片。

场景策略分离，**禁止**共用「每层固定 N」：

- ``right_click``：深度 1，子项最多 ``RIGHT_CLICK_MAX_CHILDREN``
- ``cleanup``：绝对大小 + 占上级比例剪枝，深度/单批路径安全阀；
  装不下则 ``has_more`` + ``deferred_top``，由 ``CleanupJob`` 跨批继续

本模块不调盘、不调 delete；``get_children(parent_rel) -> list[dict]`` 由调用方注入
（对比树可为 ``Diff.compare_children`` 的 dict 列表）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 场景名
# ---------------------------------------------------------------------------
SCENARIO_RIGHT_CLICK = "right_click"
SCENARIO_CLEANUP = "cleanup"

# 右键：仅一层 + 固定条数（不是全局 meta 上限）
RIGHT_CLICK_MAX_CHILDREN = 10
RIGHT_CLICK_MAX_DEPTH = 1

# 清理：可调常量（产品可后移设置页）
CLEANUP_MIN_ABS_SIZE = 200 * 1024 * 1024  # ~200MB
CLEANUP_MIN_SHARE_OF_PARENT = 0.12
CLEANUP_MAX_DEPTH = 3  # seed depth=0
CLEANUP_MAX_PATHS_PER_SLICE = 48
CLEANUP_DEFERRED_TOP_N = 10

GetChildrenFn = Callable[[str], list[dict[str, Any]]]


def node_metric(node: dict[str, Any] | None) -> int:
    """排序/门槛用：max(new_size, |delta|)。"""
    if not isinstance(node, dict):
        return 0
    try:
        new_size = int(node.get("new_size") or 0)
    except (TypeError, ValueError):
        new_size = 0
    try:
        delta = int(node.get("delta") or 0)
    except (TypeError, ValueError):
        delta = 0
    return max(abs(new_size), abs(delta))


def rel_key(node: dict[str, Any] | None) -> str:
    """相对扫描根的路径键；根/空为 \"\"。"""
    if not isinstance(node, dict):
        return ""
    return str(
        node.get("rel")
        or node.get("rel_path")
        or node.get("path")
        or ""
    ).strip().replace("/", "\\")


def should_expand(
    node: dict[str, Any] | None,
    parent: dict[str, Any] | None,
    depth: int,
    *,
    min_abs_size: int = CLEANUP_MIN_ABS_SIZE,
    min_share_of_parent: float = CLEANUP_MIN_SHARE_OF_PARENT,
    max_depth: int = CLEANUP_MAX_DEPTH,
) -> bool:
    """清理场景：目录是否值得再 get_children。"""
    if not isinstance(node, dict):
        return False
    if not node.get("is_dir"):
        return False
    try:
        d = int(depth)
    except (TypeError, ValueError):
        d = 0
    if d >= int(max_depth):
        return False
    m = node_metric(node)
    try:
        abs_floor = int(min_abs_size)
    except (TypeError, ValueError):
        abs_floor = CLEANUP_MIN_ABS_SIZE
    if m < abs_floor:
        return False
    if parent is not None and isinstance(parent, dict):
        pm = node_metric(parent)
        if pm > 0:
            try:
                share = float(min_share_of_parent)
            except (TypeError, ValueError):
                share = CLEANUP_MIN_SHARE_OF_PARENT
            if m / pm < share:
                return False
    return True


def normalize_item(
    node: dict[str, Any] | None,
    *,
    parent_rel: str = "",
    root: str = "",
) -> dict[str, Any] | None:
    """统一 item 字段（与 SoftwareContext / propose 对齐）。"""
    if not isinstance(node, dict):
        return None
    rel = rel_key(node)
    name = str(node.get("name") or rel or "").strip() or "?"
    path = str(node.get("path") or node.get("full") or "").strip()
    if not path and root and rel:
        path = str(root).rstrip("\\/") + "\\" + rel.lstrip("\\/")
    elif not path and root and not rel:
        path = str(root)
    try:
        old_size = int(node.get("old_size") or 0)
    except (TypeError, ValueError):
        old_size = 0
    try:
        new_size = int(node.get("new_size") or 0)
    except (TypeError, ValueError):
        new_size = 0
    try:
        delta = int(node.get("delta") or 0)
    except (TypeError, ValueError):
        delta = 0
    item: dict[str, Any] = {
        "name": name,
        "rel": rel,
        "rel_path": rel,
        "path": path,
        "is_dir": bool(node.get("is_dir")),
        "kind": str(node.get("kind") or ""),
        "old_size": old_size,
        "new_size": new_size,
        "delta": delta,
    }
    if parent_rel:
        item["parent_rel"] = str(parent_rel).replace("/", "\\")
    if node.get("mtime"):
        try:
            item["mtime"] = float(node.get("mtime") or 0)
        except (TypeError, ValueError):
            pass
    return item


def clip_right_click_children(
    children: list[dict[str, Any]] | None,
    *,
    max_n: int = RIGHT_CLICK_MAX_CHILDREN,
) -> list[dict[str, Any]]:
    """右键场景：只保留前 N 条（调用方应已按 |delta| 排序）。"""
    if not children:
        return []
    try:
        limit = int(max_n)
    except (TypeError, ValueError):
        limit = RIGHT_CLICK_MAX_CHILDREN
    if limit <= 0:
        return []
    out: list[dict[str, Any]] = []
    for ch in children:
        if not isinstance(ch, dict):
            continue
        item = normalize_item(ch)
        if item:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def pack_right_click(
    focus: dict[str, Any] | None,
    children: list[dict[str, Any]] | None = None,
    *,
    root: str = "",
    max_children: int = RIGHT_CLICK_MAX_CHILDREN,
) -> dict[str, Any]:
    """右键单项：focus + 一层 children，has_more 恒 false。"""
    focus_item = normalize_item(focus, root=root) if focus else None
    kids = clip_right_click_children(children, max_n=max_children)
    # 子项补 parent_rel
    pref = rel_key(focus) if focus else ""
    for k in kids:
        if pref and not k.get("parent_rel"):
            k["parent_rel"] = pref
    ctx: dict[str, Any] = {
        "scenario": SCENARIO_RIGHT_CLICK,
        "has_more": False,
        "slice": 0,
        "paths_in_slice": (1 if focus_item else 0) + len(kids),
        "children": kids,
        "deferred_top": [],
    }
    if focus_item:
        for key in (
            "path",
            "rel",
            "rel_path",
            "name",
            "is_dir",
            "kind",
            "old_size",
            "new_size",
            "delta",
            "mtime",
        ):
            if key in focus_item:
                ctx[key] = focus_item[key]
        if "rel" in focus_item and "rel_path" not in ctx:
            ctx["rel_path"] = focus_item["rel"]
    return ctx


@dataclass
class CleanupJob:
    """清理任务：跨批共享队列。"""

    seed: dict[str, Any]
    # 待处理：{"node", "parent", "depth"}
    pending: list[dict[str, Any]] = field(default_factory=list)
    slice_index: int = 0
    seen_rels: set[str] = field(default_factory=set)
    root: str = ""

    def has_pending(self) -> bool:
        return bool(self.pending)


def start_cleanup_job(
    seed: dict[str, Any],
    get_children: GetChildrenFn,
    *,
    root: str = "",
) -> CleanupJob:
    """初始化清理任务：seed 的直接子项进入 pending（按 metric 降序）。"""
    seed_node = dict(seed or {})
    seed_rel = rel_key(seed_node)
    job = CleanupJob(seed=seed_node, root=str(root or ""), slice_index=0)
    try:
        raw = list(get_children(seed_rel) or [])
    except Exception:  # noqa: BLE001 — 注入失败当空
        raw = []
    children = [c for c in raw if isinstance(c, dict)]
    children.sort(key=node_metric, reverse=True)
    for ch in children:
        job.pending.append({"node": ch, "parent": seed_node, "depth": 1})
    return job


def pack_cleanup_slice(
    job: CleanupJob,
    get_children: GetChildrenFn,
    *,
    min_abs_size: int | None = None,
    min_share_of_parent: float | None = None,
    max_depth: int | None = None,
    max_paths_per_slice: int | None = None,
    deferred_top_n: int | None = None,
) -> dict[str, Any]:
    """打一批清理上下文；更新 job.pending / seen / slice_index。

    返回可并入 ask.context 的 dict：
    scenario, focus 字段, items, slice, has_more, deferred_top, paths_in_slice

    阈值为 None 时读模块常量（便于测试 monkeypatch）。
    """
    if min_abs_size is None:
        min_abs_size = CLEANUP_MIN_ABS_SIZE
    if min_share_of_parent is None:
        min_share_of_parent = CLEANUP_MIN_SHARE_OF_PARENT
    if max_depth is None:
        max_depth = CLEANUP_MAX_DEPTH
    if deferred_top_n is None:
        deferred_top_n = CLEANUP_DEFERRED_TOP_N
    try:
        budget = int(
            max_paths_per_slice
            if max_paths_per_slice is not None
            else CLEANUP_MAX_PATHS_PER_SLICE
        )
    except (TypeError, ValueError):
        budget = CLEANUP_MAX_PATHS_PER_SLICE
    if budget < 1:
        budget = 1

    items: list[dict[str, Any]] = []
    seed = job.seed
    seed_rel = rel_key(seed)
    root = job.root

    # 每批都带上 seed 作为焦点（便于模型定位），但不重复占用 seen 逻辑外的第二次展开
    seed_item = normalize_item(seed, root=root)
    if seed_item and seed_rel not in job.seen_rels:
        items.append(seed_item)
        job.seen_rels.add(seed_rel)
    elif seed_item:
        # 后续批次仍在 focus 区展示 seed，items 里不再重复塞同一条
        pass

    expand_kwargs = {
        "min_abs_size": min_abs_size,
        "min_share_of_parent": min_share_of_parent,
        "max_depth": max_depth,
    }

    while job.pending and len(items) < budget:
        job.pending.sort(
            key=lambda w: node_metric(w.get("node") if isinstance(w, dict) else None),
            reverse=True,
        )
        work = job.pending.pop(0)
        if not isinstance(work, dict):
            continue
        node = work.get("node")
        parent = work.get("parent")
        try:
            depth = int(work.get("depth") or 0)
        except (TypeError, ValueError):
            depth = 0
        if not isinstance(node, dict):
            continue
        r = rel_key(node)
        if r in job.seen_rels:
            continue
        parent_rel = rel_key(parent) if isinstance(parent, dict) else seed_rel
        item = normalize_item(node, parent_rel=parent_rel, root=root)
        if not item:
            continue
        job.seen_rels.add(r)
        items.append(item)

        if should_expand(node, parent if isinstance(parent, dict) else seed, depth, **expand_kwargs):
            try:
                kids_raw = list(get_children(r) or [])
            except Exception:  # noqa: BLE001
                kids_raw = []
            kids = [c for c in kids_raw if isinstance(c, dict)]
            kids.sort(key=node_metric, reverse=True)
            for ch in kids:
                cr = rel_key(ch)
                if not cr and cr != "":
                    continue
                if cr in job.seen_rels:
                    continue
                # 已在 pending 的同 rel 去重
                if any(
                    isinstance(p, dict) and rel_key(p.get("node")) == cr
                    for p in job.pending
                ):
                    continue
                job.pending.append(
                    {"node": ch, "parent": node, "depth": depth + 1}
                )

    # 本批没轮到的：pending 摘要（禁止静默丢）
    deferred: list[dict[str, Any]] = []
    try:
        d_n = int(deferred_top_n)
    except (TypeError, ValueError):
        d_n = CLEANUP_DEFERRED_TOP_N
    if d_n > 0 and job.pending:
        rest = sorted(
            [p for p in job.pending if isinstance(p, dict)],
            key=lambda w: node_metric(w.get("node")),
            reverse=True,
        )
        for w in rest[:d_n]:
            n = w.get("node")
            if not isinstance(n, dict):
                continue
            try:
                ns = int(n.get("new_size") or 0)
            except (TypeError, ValueError):
                ns = 0
            deferred.append(
                {
                    "name": str(n.get("name") or rel_key(n) or "?"),
                    "rel": rel_key(n),
                    "metric": node_metric(n),
                    "is_dir": bool(n.get("is_dir")),
                    "new_size": ns,
                }
            )

    has_more = bool(job.pending)
    slice_i = int(job.slice_index)
    job.slice_index = slice_i + 1

    # 若 seed 未进 items（后续批），paths 只计本批 items；focus 仍写出 seed
    if seed_item and not any(rel_key(it) == seed_rel for it in items):
        # focus-only seed for later slices: prepend for model visibility within budget
        if len(items) < budget:
            items.insert(0, seed_item)
        # if full, still expose via focus fields only

    ctx: dict[str, Any] = {
        "scenario": SCENARIO_CLEANUP,
        "slice": slice_i,
        "has_more": has_more,
        "paths_in_slice": len(items),
        "items": items,
        "deferred_top": deferred,
        "children": items[1:] if items and rel_key(items[0]) == seed_rel else items,
    }
    if seed_item:
        for key in (
            "path",
            "rel",
            "rel_path",
            "name",
            "is_dir",
            "kind",
            "old_size",
            "new_size",
            "delta",
            "mtime",
        ):
            if key in seed_item:
                ctx[key] = seed_item[key]
        ctx["rel_path"] = seed_item.get("rel") or seed_item.get("rel_path") or ""
    return ctx
