"""目录遍历线程数的本机实测。

与 MFT 那条测试分开：这里不读系统文件表，靠逐个目录遍历量出不同线程数的
相对快慢。每档扫描直到累计若干条为止，各档扫的是同一片目录，比的是扫完同样
条数各用多久；慢盘扫不满目标时退化为按已扫条数折算。
"""

import time

from core import applog, i18n, memprobe
from core.scanner import ScanCancelled, scan_to_snapshot

# 每档扫到这么多条就停，各档比的是同一片目录，速率才可比。
DIR_PROBE_SAMPLE = 150_000
# 慢盘（机械盘）扫不满目标时的兜底时限，到点按已扫条数折算。
DIR_PROBE_MAX_SECONDS = 6.0
# 折算基准即每档目标条数，保留旧名以便外部引用
DIR_PROBE_SECONDS = DIR_PROBE_MAX_SECONDS
# 档位：1、2、4、8 线程。实测八个线程以后再加只会更慢，不再往上测。
DIR_PROBE_LEVELS = (1, 2, 4, 8)
# 进度回报间隔，与解析进程测试保持同一节奏
DIR_PROBE_GAP = 1.0



def _probe_once(root: str, workers: int, cancel, on_tick=None) -> tuple[float, float, float]:
    """用指定线程数扫一段时间，返回 (扫到的条数, 实际用时, 扫描多占的内存兆数)。

    ``on_tick`` 每秒收到一次 0 到 1 的完成比例，供界面持续刷新进度。
    比例按已用时间与兜底时限之比算，扫满目标条数会提前结束。
    内存按进程私有内存计，并减掉还没开始扫描时的基线，得到扫描本身带来的增量。
    """
    base_mem = memprobe.tree_private_bytes()
    samples: list[int] = []
    sampled = [0.0]
    deadline = time.perf_counter() + DIR_PROBE_MAX_SECONDS
    seen = [0]
    t0 = time.perf_counter()
    ticked = [0.0]

    def _tick() -> None:
        now = time.perf_counter()
        # 内存单独按 0.2 秒采一次，最后取九十分位，比单次峰值稳
        if now - sampled[0] >= 0.2:
            sampled[0] = now
            samples.append(memprobe.tree_private_bytes())
        if on_tick is None:
            return
        if now - ticked[0] < DIR_PROBE_GAP:
            return
        ticked[0] = now
        on_tick(min(1.0, (now - t0) / DIR_PROBE_MAX_SECONDS))

    def _progress(files: int, _current: str) -> None:
        if files > seen[0]:
            seen[0] = files
        _tick()

    def _stop() -> bool:
        _tick()
        if cancel is not None and cancel.is_set():
            return True
        if seen[0] >= DIR_PROBE_SAMPLE:
            return True
        return time.perf_counter() >= deadline

    try:
        scan_to_snapshot(
            root,
            None,
            workers=workers,
            progress=_progress,
            cancel=_stop,
            no_mft=True,
        )
    except ScanCancelled:
        pass
    except OSError as exc:
        applog.warn("directory probe failed | %s" % exc)
    used = max(1e-6, time.perf_counter() - t0)
    _tick()
    if samples:
        samples.sort()
        # 九十分位近似峰值，避开垃圾回收前后的个别毛刺
        idx = max(0, int(len(samples) * 0.9) - 1)
        top = samples[idx]
    else:
        top = base_mem
    extra_mb = max(0.0, (top - base_mem) / (1024.0 * 1024.0))
    return float(seen[0]), used, round(extra_mb, 1)


def measure_dir_threads(root: str, cancel, on_progress=None) -> dict:
    """量出 1 个线程与多个线程的遍历速率，折算成固定规模下的耗时。

    Returns:
        ``{"points": [[线程数, 折算耗时], ...], "sample": 条数, "workers": 实际档位}``
    """
    import os

    cores = os.cpu_count() or 4
    levels = [n for n in DIR_PROBE_LEVELS if n <= max(2, cores)]
    if len(levels) < 2:
        levels = [1, 2]
    points: list[list[float]] = []
    mems: list[list[float]] = []
    n_levels = len(levels)
    for idx, workers in enumerate(levels):
        # 每档占整体进度的 n 分之一，档内按已用时间平滑推进，
        # 否则只在每档结束时回报一次，界面上看就是十几秒不动。
        def _tick_level(frac: float, _idx=idx) -> None:
            if on_progress is not None:
                on_progress(_idx + frac, n_levels)

        files, used, mem_mb = _probe_once(root, workers, cancel, on_tick=_tick_level)
        if files <= 0:
            continue
        if files >= DIR_PROBE_SAMPLE:
            # 扫满了目标条数，耗时就是实测量，不再折算
            cost = used
        else:
            cost = used * DIR_PROBE_SAMPLE / files
        points.append([workers, round(cost, 2)])
        mems.append([workers, mem_mb])
        if on_progress is not None:
            on_progress(idx + 1, n_levels)
        if cancel is not None and cancel.is_set():
            break
    return {
        "points": points,
        "mem": mems,
        "sample": DIR_PROBE_SAMPLE,
        "sample_seconds": DIR_PROBE_SECONDS,
        "levels": levels,
        "note": i18n.t(
            "每档扫到 %d 条为止，各档比的是同一片目录；慢盘扫不满时最多等 %d 秒"
            % (DIR_PROBE_SAMPLE, int(DIR_PROBE_MAX_SECONDS)),
            "Each level walks until %d entries; slow disks are cut off at %d s."
            % (DIR_PROBE_SAMPLE, int(DIR_PROBE_MAX_SECONDS)),
        ),
    }
