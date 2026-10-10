"""解析进程数的推导：按核数与记录量选进程数。"""

from __future__ import annotations

import os


def _env_mft_procs() -> int | None:
    """开发覆盖：``WSMC_MFT_WORKERS`` 或 ``WSMC_MFT_PROCS``。"""
    for key in ("WSMC_MFT_WORKERS", "WSMC_MFT_PROCS"):
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        try:
            return max(1, min(_MAX_PROCS, int(raw)))
        except ValueError:
            continue
    return None


def choose_mft_procs(n_records: int) -> int:
    """按核数 + 记录量推导解析进程数。

    ::

        procs = min(
            max(1, cpu_count - 1),       # 留 1 逻辑核给 UI/主进程
            max(1, n_records // 50_000), # 每进程至少约 5 万条
        )

    环境变量强制覆盖时忽略活量公式（仍 cap 到 :data:`_MAX_PROCS`）。
    算出 ``1`` 时调用方应走串行，勿起 Pool。
    """
    forced = _env_mft_procs()
    if forced is not None:
        return forced
    n = max(0, int(n_records))
    if n <= 0:
        return 1
    cpu = max(1, os.cpu_count() or 1)
    by_cpu = max(1, cpu - 1)
    by_work = max(1, n // _RECORDS_PER_PROC)
    return max(1, min(_MAX_PROCS, by_cpu, by_work))


def default_mft_workers(preferred: int | None = None) -> int:
    """兼容旧名。

    ``preferred`` **已忽略**（不再继承扫描线程数）。无记录数时按核数估上限，
    真正按活量裁剪在 :func:`choose_mft_procs` / :func:`parse_records_parallel`。
    """
    _ = preferred
    forced = _env_mft_procs()
    if forced is not None:
        return forced
    cpu = max(1, os.cpu_count() or 1)
    return max(1, min(_MAX_PROCS, cpu - 1 if cpu > 1 else 1))


# 每进程至少约这么多条才值得开池（小卷单进程更快）
_RECORDS_PER_PROC = 50_000


# 进程数软上限：超线程收益薄，再多 spawn/IPC 易倒贴
_MAX_PROCS = 16
