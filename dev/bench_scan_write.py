"""临时扫描写库基准：合成树上连扫两次并打印墙钟/分段。"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

# 保证仓库根在 path
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def ensure_tree(*, dirs: int = 400, files_per: int = 400, nested: int = 100) -> Path:
    """约 dirs * (files_per + nested) 个文件；改规模时换目录名以免混用。"""
    tag = f"d{dirs}_f{files_per}_n{nested}"
    root = Path(tempfile.gettempdir()) / f"wsmc_scan_bench_tree_{tag}"
    if root.exists():
        n = sum(1 for p in root.rglob("*") if p.is_file())
        print(f"reuse tree {root} files≈{n}", flush=True)
        return root
    print(f"building tree {tag}...", flush=True)
    n_files = 0
    for d in range(dirs):
        p = root / f"d{d:04d}"
        p.mkdir(parents=True, exist_ok=True)
        for i in range(files_per):
            (p / f"f{i:04d}.txt").write_bytes(b"x" * 64)
            n_files += 1
        sub = p / "sub"
        sub.mkdir(exist_ok=True)
        for i in range(nested):
            (sub / f"n{i:04d}.bin").write_bytes(b"y" * 32)
            n_files += 1
    print(f"built {n_files} files under {root}", flush=True)
    return root


def run_once(label: str, tree: Path, db: Path, workers: int) -> dict:
    if db.exists():
        db.unlink()
    from core.scanner import scan_to_snapshot
    from dev.scan_timing import ScanTimer

    timer = ScanTimer(root=str(tree), workers=workers, compress_enabled=False)
    t0 = time.perf_counter()
    meta = scan_to_snapshot(str(tree), str(db), workers=workers, timer=timer)
    wall = time.perf_counter() - t0
    rep = timer.finish(status="ok") or {}
    out = {
        "label": label,
        "wall_s": round(wall, 4),
        "files": meta.file_count,
        "dirs": meta.dir_count,
        "spans_s": rep.get("spans_s") or {},
        "files_per_s": rep.get("files_per_s"),
        "entries_per_s": rep.get("entries_per_s"),
        "workers": workers,
    }
    rate = ""
    if out["files_per_s"] is not None:
        rate = f" files/s={out['files_per_s']}"
        if out["entries_per_s"] is not None:
            rate += f" entries/s={out['entries_per_s']}"
    print(
        f"[{label}] wall={out['wall_s']:.3f}s files={out['files']} "
        f"dirs={out['dirs']}{rate} spans={out['spans_s']}",
        flush=True,
    )
    return out


def writer_microbench(n: int = 200_000, *, via_entry: bool = False) -> dict:
    """纯写库：绕过 scandir。via_entry=True 走 Entry，否则走 EntryRow。"""
    from core.models import Entry, SnapshotMeta
    from core.snapshot import EntryRow, SnapshotWriter

    label = "entry" if via_entry else "row"
    db = Path(tempfile.gettempdir()) / f"wsmc_writer_micro_{label}.db"
    if db.exists():
        db.unlink()
    t0 = time.perf_counter()
    with SnapshotWriter(str(db), root="bench") as w:
        if via_entry:
            w.add(Entry(id=1, parent_id=None, name="", size=n * 64, is_dir=True, mtime=0))
        else:
            w.add_row(EntryRow.directory(1, None, "", n * 64, 0))
        batch: list = []
        for i in range(2, n + 2):
            if via_entry:
                batch.append(
                    Entry(
                        id=i,
                        parent_id=1,
                        name=f"f{i}.dat",
                        size=64,
                        is_dir=False,
                        mtime=0,
                    )
                )
            else:
                batch.append(EntryRow.file(i, 1, f"f{i}.dat", 64, 0))
            if len(batch) >= 512:
                if via_entry:
                    w.add_many(batch)
                else:
                    w.add_rows(batch)
                batch.clear()
        if batch:
            if via_entry:
                w.add_many(batch)
            else:
                w.add_rows(batch)

        meta = SnapshotMeta(
            root="bench",
            scanned_at=time.time(),
            total_size=n * 64,
            file_count=n,
            dir_count=1,
        )
        t_fin0 = time.perf_counter()
        w.finalize(meta)
        fin = time.perf_counter() - t_fin0
    wall = time.perf_counter() - t0
    size = db.stat().st_size if db.exists() else 0
    out = {
        "mode": label,
        "wall_s": round(wall, 4),
        "finalize_s": round(fin, 4),
        "n": n,
        "db_bytes": size,
    }
    print(f"[writer-micro] {out}", flush=True)
    return out


def main() -> None:
    label_prefix = sys.argv[1] if len(sys.argv) > 1 else "run"
    mode = sys.argv[2] if len(sys.argv) > 2 else "all"
    if mode in ("all", "writer"):
        print(f"=== writer-micro {label_prefix} ===", flush=True)
        # 同一代码上对比 Entry 路径 vs 行元组路径
        writer_microbench(200_000, via_entry=True)
        writer_microbench(200_000, via_entry=True)
        writer_microbench(200_000, via_entry=False)
        writer_microbench(200_000, via_entry=False)
    if mode == "writer":
        return
    # ~200k files
    tree = ensure_tree(dirs=400, files_per=400, nested=100)
    from core.store import default_scan_workers

    workers = default_scan_workers()
    print(f"workers={workers}", flush=True)
    db = Path(tempfile.gettempdir()) / f"wsmc_scan_bench_{label_prefix}.db"
    print(f"=== full-scan {label_prefix} ===", flush=True)
    a = run_once(f"{label_prefix}-1", tree, db, workers)
    b = run_once(f"{label_prefix}-2", tree, db, workers)
    print(
        f"summary {label_prefix}: "
        f"1={a['wall_s']}s 2={b['wall_s']}s "
        f"drain1={a['spans_s'].get('drain_rows')} "
        f"drain2={b['spans_s'].get('drain_rows')} "
        f"fin1={a['spans_s'].get('finalize')} "
        f"fin2={b['spans_s'].get('finalize')}",
        flush=True,
    )


if __name__ == "__main__":
    main()
