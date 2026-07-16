"""AiService 对比树清理多切片 API（不真删、不调模型）。"""

from __future__ import annotations

from modules.ai import packing as pk
from modules.ai.service import AiService, PUBLIC_METHODS
import core.applog as applog


MB = 1024 * 1024


def setup_function():
    applog.clear()
    applog.set_min_level("INFO")


def teardown_function():
    applog.clear()
    applog.set_min_level(None)


def _svc(tmp_path, get_children=None):
    ctx = {
        "emit": lambda *_a, **_k: None,
        "app_data_dir": lambda: str(tmp_path),
        "t": lambda zh, en: en,
        "get_lang": lambda: "zh",
    }
    if get_children is not None:
        ctx["get_diff_children"] = get_children
    return AiService(ctx)


def _node(rel, *, is_dir=True, new_size=0, delta=None, name=None, kind="grew"):
    if delta is None:
        delta = new_size
    return {
        "path": rel,
        "name": name or (rel.split("\\")[-1] if rel else "root"),
        "is_dir": is_dir,
        "new_size": new_size,
        "old_size": 0,
        "delta": delta,
        "kind": kind,
    }


def test_public_methods_include_cleanup():
    assert "start_compare_cleanup" in PUBLIC_METHODS
    assert "next_compare_cleanup" in PUBLIC_METHODS
    assert "cancel_compare_cleanup" in PUBLIC_METHODS


def test_start_missing_paths(tmp_path):
    svc = _svc(tmp_path, get_children=lambda *_a: [])
    res = svc.start_compare_cleanup(old_path="", new_path=r"C:\b")
    assert "error" in res


def test_start_without_get_children(tmp_path):
    svc = _svc(tmp_path, get_children=None)
    res = svc.start_compare_cleanup(old_path=r"C:\a", new_path=r"C:\b")
    assert "error" in res


def test_start_and_next_multi_slice(tmp_path, monkeypatch):
    """根下大量大户 → has_more；next 继续；结束可 cancel。"""
    # 压小单批预算，确保多切片
    monkeypatch.setattr(pk, "CLEANUP_MAX_PATHS_PER_SLICE", 6)

    siblings = [_node(f"d{i}", new_size=300 * MB, name=f"d{i}") for i in range(20)]
    tree = {"": siblings}
    for s in siblings:
        tree[s["path"]] = []

    calls: list[tuple] = []

    def get_diff_children(old, new, parent):
        calls.append((old, new, parent))
        key = str(parent or "").replace("/", "\\")
        return list(tree.get(key, []))

    svc = _svc(tmp_path, get_children=get_diff_children)
    seed = {
        "path": "",
        "name": "root",
        "is_dir": True,
        "new_size": 50 * 200 * MB,
        "old_size": 0,
        "delta": 50 * 200 * MB,
    }
    r1 = svc.start_compare_cleanup(
        old_path=r"C:\snap\old.dbz",
        new_path=r"C:\snap\new.dbz",
        seed=seed,
        root=r"C:\Scan",
    )
    assert r1.get("ok") is True
    assert r1.get("job_id")
    assert r1.get("has_more") is True
    assert r1.get("context", {}).get("scenario") == pk.SCENARIO_CLEANUP
    assert r1.get("context", {}).get("has_more") is True
    job_id = r1["job_id"]
    assert isinstance(r1.get("context", {}).get("items"), list)
    assert r1["context"]["items"]
    assert r1.get("deferred_top") or r1["context"].get("deferred_top")

    r2 = svc.next_compare_cleanup(job_id=job_id)
    assert r2.get("ok") is True
    assert r2.get("job_id") == job_id
    assert r2.get("context", {}).get("scenario") == pk.SCENARIO_CLEANUP
    assert (r2.get("slice") or 0) >= 1 or (r2.get("context", {}).get("slice") or 0) >= 1

    # cancel 剩余
    c = svc.cancel_compare_cleanup(job_id=job_id)
    assert c.get("ok") is True
    assert c.get("cancelled") in (0, 1)

    # 取消后再 next 应报错
    r3 = svc.next_compare_cleanup(job_id=job_id)
    assert "error" in r3


def test_start_seed_rel_from_compare_path(tmp_path):
    """对比树 node.path 为相对路径时写入 rel。"""
    kids = [_node("Users\\foo\\bar", new_size=400 * MB)]
    tree = {"Users\\foo": kids, "Users\\foo\\bar": []}

    def get_diff_children(old, new, parent):
        return list(tree.get(str(parent or "").replace("/", "\\"), []))

    svc = _svc(tmp_path, get_children=get_diff_children)
    r = svc.start_compare_cleanup(
        old_path="o",
        new_path="n",
        seed={
            "path": "Users\\foo",
            "name": "foo",
            "is_dir": True,
            "new_size": 800 * MB,
            "delta": 100 * MB,
        },
        root="C:\\",
    )
    assert r.get("ok") is True
    ctx = r["context"]
    assert ctx.get("rel") == "Users\\foo" or ctx.get("rel_path") == "Users\\foo"
    assert any(
        (it.get("rel") or it.get("path") or "").endswith("bar")
        for it in (ctx.get("items") or [])
    )


def test_reset_clears_cleanup_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr("modules.ai.config.reset", lambda _d: None)
    tree = {"": [_node("a", new_size=300 * MB)]}

    def get_diff_children(old, new, parent):
        return list(tree.get(str(parent or ""), []))

    svc = _svc(tmp_path, get_children=get_diff_children)
    r = svc.start_compare_cleanup(old_path="o", new_path="n", seed={"is_dir": True})
    assert r.get("ok") is True
    assert svc._cleanup_jobs
    svc.reset()
    assert not svc._cleanup_jobs


def test_cancel_all_cleanup_jobs(tmp_path):
    tree = {"": [_node("a", new_size=300 * MB)]}

    def get_diff_children(old, new, parent):
        return list(tree.get(str(parent or ""), []))

    svc = _svc(tmp_path, get_children=get_diff_children)
    svc.start_compare_cleanup(old_path="o", new_path="n")
    assert svc._cleanup_jobs
    c = svc.cancel_compare_cleanup()
    assert c.get("ok") is True
    assert c.get("cancelled") >= 1
    assert not svc._cleanup_jobs


def test_next_missing_job_id(tmp_path):
    svc = _svc(tmp_path, get_children=lambda *_a: [])
    assert "error" in svc.next_compare_cleanup()
    assert "error" in svc.next_compare_cleanup(job_id="nope")
