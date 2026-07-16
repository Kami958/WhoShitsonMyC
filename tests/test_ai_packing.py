"""AI packing：右键固定一层 vs 清理有限递归 / 多切片。"""

from __future__ import annotations

from modules.ai import packing as pk
from modules.ai import prompts as ai_prompts
from modules.ai import tools as ai_tools


def _node(rel, *, is_dir=True, new_size=0, delta=None, name=None, kind="grew"):
    if delta is None:
        delta = new_size
    return {
        "path": rel,
        "rel": rel,
        "name": name or (rel.split("\\")[-1] if rel else "root"),
        "is_dir": is_dir,
        "new_size": new_size,
        "old_size": 0,
        "delta": delta,
        "kind": kind,
    }


MB = 1024 * 1024


def test_right_click_clips_to_ten():
    kids = [_node(f"a\\f{i}", is_dir=False, new_size=i * MB) for i in range(20)]
    ctx = pk.pack_right_click(_node("a", new_size=100 * MB), kids)
    assert ctx["scenario"] == pk.SCENARIO_RIGHT_CLICK
    assert ctx["has_more"] is False
    assert len(ctx["children"]) == pk.RIGHT_CLICK_MAX_CHILDREN
    assert ctx["paths_in_slice"] == 1 + pk.RIGHT_CLICK_MAX_CHILDREN


def test_should_expand_abs_and_share():
    parent = _node("p", new_size=10 * 200 * MB)  # ~2GB
    big = _node("p\\big", new_size=500 * MB)
    tiny = _node("p\\tiny", new_size=50 * MB)  # < 200MB
    small_share = _node("p\\ss", new_size=100 * MB)  # >=200? no 100MB
    mid = _node("p\\mid", new_size=250 * MB)  # 250/2000=0.125 ~ borderline

    assert pk.should_expand(big, parent, 1) is True
    assert pk.should_expand(tiny, parent, 1) is False
    # 100MB < 200MB abs
    assert pk.should_expand(small_share, parent, 1) is False
    # depth cap
    assert pk.should_expand(big, parent, pk.CLEANUP_MAX_DEPTH) is False
    # file never
    assert pk.should_expand(_node("p\\f", is_dir=False, new_size=900 * MB), parent, 1) is False
    # mid: 250MB abs ok; share 250/2000=0.125 >= 0.12
    assert pk.should_expand(mid, parent, 1) is True
    # low share: 250MB under 10GB parent
    huge_parent = _node("hp", new_size=10 * 1024 * MB)
    low = _node("hp\\low", new_size=250 * MB)
    assert pk.should_expand(low, huge_parent, 1) is False


def test_cleanup_multi_slice_has_more_and_deferred():
    """根下多个大户：第一批装不下 → has_more + deferred。"""
    seed = _node("", name="root", new_size=50 * 200 * MB)
    # 20 个并列 300MB 目录
    siblings = [
        _node(f"d{i}", new_size=300 * MB, name=f"d{i}") for i in range(20)
    ]

    tree = {"": siblings}
    for s in siblings:
        tree[s["rel"]] = []  # 无孙项

    def get_children(parent_rel: str):
        return list(tree.get(parent_rel.replace("/", "\\"), []))

    job = pk.start_cleanup_job(seed, get_children, root=r"C:\Scan")
    assert len(job.pending) == 20

    slice1 = pk.pack_cleanup_slice(
        job,
        get_children,
        max_paths_per_slice=8,  # seed + 7 子项
        min_abs_size=200 * MB,
        min_share_of_parent=0.01,  # 比例放宽，主要测分批
        max_depth=3,
    )
    assert slice1["scenario"] == pk.SCENARIO_CLEANUP
    assert slice1["slice"] == 0
    assert slice1["has_more"] is True
    assert len(slice1["items"]) <= 8
    assert slice1["deferred_top"]
    assert any(d.get("name", "").startswith("d") for d in slice1["deferred_top"])

    slice2 = pk.pack_cleanup_slice(
        job,
        get_children,
        max_paths_per_slice=8,
        min_abs_size=200 * MB,
        min_share_of_parent=0.01,
        max_depth=3,
    )
    assert slice2["slice"] == 1
    # 第二批应继续消化 pending
    assert slice2["paths_in_slice"] >= 1


def test_cleanup_does_not_expand_below_abs():
    seed = _node("root", new_size=5 * 200 * MB)
    small_dir = _node("root\\small", new_size=50 * MB, name="small")
    tree = {
        "root": [small_dir],
        "root\\small": [_node("root\\small\\x", is_dir=False, new_size=10 * MB)],
    }

    def get_children(parent_rel: str):
        return list(tree.get(parent_rel, []))

    job = pk.start_cleanup_job(seed, get_children)
    out = pk.pack_cleanup_slice(
        job,
        get_children,
        min_abs_size=200 * MB,
        min_share_of_parent=0.01,
        max_paths_per_slice=20,
    )
    rels = {it.get("rel") for it in out["items"]}
    assert "root\\small" in rels or "small" in str(rels)
    # 未展开 small 的子项
    assert not any("root\\small\\x" == it.get("rel") for it in out["items"])


def test_format_context_cleanup_not_capped_at_ten():
    items = [
        {
            "name": f"n{i}",
            "rel": f"p\\n{i}",
            "is_dir": True,
            "kind": "grew",
            "delta": i,
            "new_size": i,
        }
        for i in range(15)
    ]
    text = ai_prompts.format_context(
        {
            "scenario": "cleanup",
            "slice": 0,
            "has_more": True,
            "paths_in_slice": 15,
            "path": r"C:\Scan",
            "is_dir": True,
            "items": items,
            "deferred_top": [{"name": "later", "metric": 999}],
        },
        lang="zh",
    )
    assert "场景：cleanup" in text
    assert "是否还有后续批：是" in text
    assert "n14" in text  # 超过 10 仍展示
    assert "本批未纳入" in text
    assert "later" in text


def test_count_meta_cleanup_vs_right_click():
    items = [{"name": f"x{i}", "rel": f"x{i}"} for i in range(15)]
    n_clean = ai_tools.count_context_meta_paths(
        {"scenario": "cleanup", "items": items}
    )
    assert n_clean == 15

    n_rc = ai_tools.count_context_meta_paths(
        {
            "path": r"C:\a",
            "children": [{"name": f"c{i}", "rel": f"c{i}"} for i in range(15)],
        }
    )
    assert n_rc == 1 + 10  # 主项 + 最多 10 子项
