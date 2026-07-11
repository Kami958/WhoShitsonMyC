"""快照存储管理测试。"""

import os

from core.models import Entry, SnapshotMeta
from core.snapshot import write_snapshot
from core.store import (
    default_scan_workers,
    delete_snapshot,
    get_compress_snapshots,
    get_scan_workers,
    list_snapshots,
    new_snapshot_path,
    set_compress_snapshots,
    set_scan_workers,
)


def _write(db, root, total, when):
    entries = [Entry(id=1, parent_id=None, name="", size=total, is_dir=True)]
    meta = SnapshotMeta(root=root, scanned_at=when, total_size=total)
    write_snapshot(db, root, entries, meta)


def test_new_snapshot_path_naming(tmp_path):
    p = new_snapshot_path("C:\\", when=0.0, out_dir=str(tmp_path))
    assert os.path.basename(p).startswith("C_")
    assert p.endswith(".db")

    p2 = new_snapshot_path("D:\\Games\\Steam", when=0.0, out_dir=str(tmp_path))
    assert "D_Games_Steam" in os.path.basename(p2)


def test_list_snapshots_sorted_newest_first(tmp_path):
    _write(os.path.join(tmp_path, "a.db"), "C:\\", 100, when=10.0)
    _write(os.path.join(tmp_path, "b.db"), "C:\\", 200, when=50.0)
    _write(os.path.join(tmp_path, "c.db"), "C:\\", 300, when=30.0)

    infos = list_snapshots(str(tmp_path))
    assert [i.total_size for i in infos] == [200, 300, 100]  # 按 scanned_at 降序


def test_list_snapshots_skips_bad_files(tmp_path):
    _write(os.path.join(tmp_path, "ok.db"), "C:\\", 100, when=1.0)
    with open(os.path.join(tmp_path, "broken.db"), "wb") as f:
        f.write(b"garbage")
    with open(os.path.join(tmp_path, "ignore.txt"), "w") as f:
        f.write("not a db")

    infos = list_snapshots(str(tmp_path))
    assert len(infos) == 1
    assert infos[0].total_size == 100


def test_delete_snapshot(tmp_path):
    db = os.path.join(tmp_path, "x.db")
    _write(db, "C:\\", 100, when=1.0)
    assert os.path.exists(db)
    delete_snapshot(db)
    assert not os.path.exists(db)
    delete_snapshot(db)  # 再删不存在的不报错


def test_scan_workers_defaults_to_all_cores():
    """默认线程数是 max(1, CPU 核数)，未设置时即返回该默认。"""
    assert default_scan_workers() == max(1, os.cpu_count() or 1)
    assert get_scan_workers() == default_scan_workers()


def test_set_and_get_scan_workers_roundtrip():
    """设置只在内存中生效：设了能读回，不涉及任何配置文件。"""
    assert set_scan_workers(4) == 4
    assert get_scan_workers() == 4


def test_set_scan_workers_clamps_out_of_range():
    assert set_scan_workers(0) == 1        # 下限收拢到 1
    assert set_scan_workers(999) == 32     # 上限收拢到 32


def test_compress_snapshots_defaults_off():
    assert get_compress_snapshots() is False
    assert set_compress_snapshots(True) is True
    assert get_compress_snapshots() is True
