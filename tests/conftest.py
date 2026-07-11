"""pytest 共享夹具。"""

import os

import pytest


@pytest.fixture(autouse=True)
def _reset_lang():
    """每个用例前把界面语言复位到默认（英文），避免全局语言态在用例间泄漏。"""
    from core import i18n

    i18n.set_lang("en")
    yield


@pytest.fixture(autouse=True)
def _reset_scan_workers():
    """每个用例前把会话级扫描线程数复位到默认，避免全局态在用例间泄漏。"""
    from core import store

    store._scan_workers = store.default_scan_workers()
    store._compress_snapshots = False
    yield


@pytest.fixture
def make_tree(tmp_path):
    """在临时目录里按规格造一棵真实文件树，返回其根路径。

    规格是一个嵌套 dict：值为 int 表示文件（该字节数的内容），
    值为 dict 表示子目录。例如::

        make_tree({"a.txt": 100, "sub": {"b.txt": 50}})
    """

    def _build(base, spec):
        for name, val in spec.items():
            target = os.path.join(base, name)
            if isinstance(val, dict):
                os.makedirs(target, exist_ok=True)
                _build(target, val)
            else:
                with open(target, "wb") as f:
                    f.write(b"\0" * val)

    def _make(spec, subdir="root"):
        root = os.path.join(tmp_path, subdir)
        os.makedirs(root, exist_ok=True)
        _build(root, spec)
        return root

    return _make


@pytest.fixture
def snapshot_map():
    """把一份 v3 快照的邻接树展开成 ``{相对路径: Entry}``（根为 ""）。

    多个测试文件共用：扫描/写入后用它验证内容。
    """

    def _load(db_path):
        from core.snapshot import children_of, open_readonly

        conn = open_readonly(db_path)
        try:
            out = {}
            root = next(iter(children_of(conn, None)))
            out[""] = root
            stack = [(root.id, "")]
            while stack:
                pid, prefix = stack.pop()
                for e in children_of(conn, pid):
                    p = e.name if prefix == "" else prefix + os.sep + e.name
                    out[p] = e
                    if e.is_dir:
                        stack.append((e.id, p))
            return out
        finally:
            conn.close()

    return _load
