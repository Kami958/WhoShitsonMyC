"""扫盘模式的反查：扫一遍盘就出结果，不写任何快照文件。"""

import os
import time

import pytest

from core.link_find import LinkFind


def _wait(task: LinkFind, timeout: float = 30.0) -> None:
    t0 = time.time()
    while task.status()["running"]:
        if time.time() - t0 > timeout:
            pytest.fail("link find did not finish in time")
        time.sleep(0.02)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction only")
def test_scan_mode_finds_link_without_writing_files(make_tree, tmp_path):
    """扫盘查得到联接，且不动磁盘上任何文件（以前要写一份临时快照库）。"""
    import _winapi

    root = make_tree({"real": {"a.txt": 10}})
    try:
        _winapi.CreateJunction(os.path.join(root, "real"), os.path.join(root, "link"))
    except (OSError, ValueError) as exc:  # 无权限/非 NTFS 时跳过，不误报
        pytest.skip(f"Cannot create junction: {exc}")

    before = {p for _d, _s, fs in os.walk(tmp_path) for p in fs}
    task = LinkFind(os.path.join(root, "real"), "scan", drives=[root])
    task.start()
    _wait(task)
    res = task.result()
    after = {p for _d, _s, fs in os.walk(tmp_path) for p in fs}

    assert res["state"] == "done"
    assert after == before
    assert res["total"] == 1
    hit = res["hits"][0]
    assert hit["link"] == os.path.join(root, "link")
    assert hit["kind"] == "junction"
    assert hit["source"] == root.rstrip("\\/")
