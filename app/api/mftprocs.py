"""解析进程数的本机测试。

只跑「读盘 + 解析」两段，不建树、不写快照、不留任何文件，结果只回给界面。
据此给出建议进程数，供用户自己填入设置项。
"""

from __future__ import annotations

import math
import os
import threading
import time

from core import applog, i18n, memprobe, store

from . import dirprobe
from core.mft.pipeline import read_and_parse_mft
from core.mft.procs import _MAX_PROCS
from core.mft.scan import _drive_letter_and_root, is_mft_eligible
from core.mft.volume import (
    close_handle,
    fill_mft_buffer,
    open_volume,
    parse_boot,
    read_at,
    resolve_mft_layout,
)

# 读速取样长度：读这块大小时长按字节数外推全表读盘时间。
# 取小一点省内存也省时间，盘上顺序读速在这段长度内已经稳定。
_PROBE_BYTES = 64 * 1024 * 1024

# 测试固定用两个进程：两个进程时解析明显慢于读盘，量到的才是解析速度；
# 若解析比读盘还快，量到的是「等读盘」，算不出解析速度。
_TEST_WORKERS = 2

# 每个解析进程按这个量折算内存占用，只用于界面上「大约省多少」的估算。

# 进度与实时资源占用每秒回传一次，界面上的用时按秒跳动，
# 与进度条一起走，看起来才是一条顺的线。
_PROGRESS_GAP = 1.0

# 单次测试的时长上限。超过这个时间机器明显偏慢或盘正忙，
# 继续测下去没有意义，先停下并把已经量到的部分交回界面。
_TIMEOUT_S = 150.0


def _guess_drive_root() -> str:
    """没指定盘符时，优先测最近一份快照所在的盘，没有就用系统盘。"""
    try:
        from core.snapshots_store import list_snapshots

        for snap in list_snapshots():
            root = str(getattr(snap, "root", "") or "")
            if root and _drive_letter_and_root(root):
                return root
    except Exception:  # noqa: BLE001 - 取不到就回退系统盘
        pass
    return os.environ.get("SystemDrive", "C:") + "\\"


class MftProcsMixin:
    """测一次本机的读盘与解析速度，给出建议的解析进程数。"""

    def test_mft_procs(self, root: str = "") -> dict:
        """在后台线程测一次；进度与结果都经事件推送。

        事件：``mft-proc-test-progress`` {phase, done, total, pct} /
        ``mft-proc-test-done`` {suggest, ratio, read_s, parse_s, ...} /
        ``mft-proc-test-error`` {message} / ``mft-proc-test-cancelled`` {}。

        Args:
            root: 要测的盘符根目录，例如 ``C:\\``。

        Returns:
            立即返回 ``{"started": True}``；条件不满足时返回 ``{"error"}``。
        """
        if self._scan_thread and self._scan_thread.is_alive():
            return {
                "error": i18n.t(
                    "正在扫描，请等这次扫描结束后再测试。",
                    "A scan is running. Try again after it finishes.",
                )
            }
        if self._mft_test_thread and self._mft_test_thread.is_alive():
            return {
                "error": i18n.t(
                    "测试正在进行。", "A test is already running."
                )
            }
        target = str(root or "").strip() or _guess_drive_root()
        try:
            eligible = bool(is_mft_eligible(target))
        except Exception:  # noqa: BLE001 - 判定失败按不满足处理
            eligible = False
        if not eligible:
            return {
                "error": i18n.t(
                    "需要以管理员身份运行程序，且目标盘符为 NTFS 文件系统。",
                    "Run the program as administrator and target an NTFS drive.",
                )
            }
        self._mft_test_cancel.clear()
        self._mft_test_thread = threading.Thread(
            target=self._run_mft_proc_test, args=(target,), daemon=True
        )
        self._mft_test_thread.start()
        return {"started": True}

    def test_perf(self, root: str = "") -> dict:
        """一次测试跑完两项：先测读取与解析，再测目录遍历。

        两条扫描路径各自的参数用户都要知道，分两次点测试没有意义，
        所以这里串行跑完一起交回。管理员与 NTFS 不满足时只跑目录遍历那一段，
        并在结果里带上 ``mft_skipped`` 说明原因。

        Returns:
            立即返回 ``{"started": True}``；条件不满足时返回 ``{"error"}``。
        """
        if self._scan_thread and self._scan_thread.is_alive():
            return {
                "error": i18n.t(
                    "正在扫描，请等这次扫描结束后再测试。",
                    "A scan is running. Try again after it finishes.",
                )
            }
        if self._mft_test_thread and self._mft_test_thread.is_alive():
            return {"error": i18n.t("测试正在进行。", "A test is already running.")}
        target = str(root or "").strip() or _guess_drive_root()
        located = _drive_letter_and_root(target)
        if not located:
            return {
                "error": i18n.t(
                    "只能测试整个盘符，请选择盘符根目录。",
                    "Only a whole drive can be tested; pick the drive root.",
                )
            }
        self._mft_test_cancel.clear()
        self._mft_test_thread = threading.Thread(
            target=self._run_perf_test,
            args=(located[1],),
            daemon=True,
        )
        self._mft_test_thread.start()
        return {"started": True}

    def test_dir_threads(self, root: str = "") -> dict:
        """测目录遍历那条路径的线程收益，不需要管理员权限。

        事件与解析进程数测试共用一套，载荷里带 ``mode`` 区分。
        """
        if self._scan_thread and self._scan_thread.is_alive():
            return {
                "error": i18n.t(
                    "正在扫描，请等这次扫描结束后再测试。",
                    "A scan is running. Try again after it finishes.",
                )
            }
        if self._mft_test_thread and self._mft_test_thread.is_alive():
            return {
                "error": i18n.t("测试正在进行。", "A test is already running.")
            }
        target = str(root or "").strip() or _guess_drive_root()
        located = _drive_letter_and_root(target)
        if not located:
            return {
                "error": i18n.t(
                    "只能测试整个盘符，请选择盘符根目录。",
                    "Only a whole drive can be tested; pick the drive root.",
                )
            }
        self._mft_test_cancel.clear()
        self._mft_test_thread = threading.Thread(
            target=self._run_dir_thread_test,
            args=(located[1],),
            daemon=True,
        )
        self._mft_test_thread.start()
        return {"started": True}

    def cancel_mft_proc_test(self) -> dict:
        """请求停止正在进行的测试。"""
        self._mft_test_cancel.set()
        return {"ok": True}

    # ---- 线程体 ---------------------------------------------------------

    def _run_perf_test(self, root: str) -> None:
        """串行跑两段：先读取与解析，再目录遍历；中途超时也把已测的段落交回。"""
        cancel = self._mft_test_cancel
        self._mft_test_timed_out = False
        self._mft_test_partial = {}
        self._mft_test_start = time.perf_counter()
        timer = threading.Timer(_TIMEOUT_S, self._on_timeout)
        timer.daemon = True
        timer.start()
        result: dict = {"procs": {}, "threads": {}}
        skipped = ""
        try:
            located = _drive_letter_and_root(root)
            if not located:
                raise RuntimeError(
                    i18n.t(
                        "只能测试整个盘符，请选择盘符根目录。",
                        "Only a whole drive can be tested; pick the drive root.",
                    )
                )
            drive, drive_root = located

            # 第一段：读全表并解析，占整体进度的前一半
            try:
                eligible = bool(is_mft_eligible(drive_root))
            except Exception:  # noqa: BLE001 - 判定失败按不满足处理
                eligible = False
            if eligible:
                read_s, total_read_s = self._measure_read_speed(drive, cancel, 0, 12)
                self._mft_test_partial = {
                    "read_s": round(total_read_s, 2),
                    "probe_s": round(read_s, 2),
                }
                if cancel.is_set():
                    self._finish_merged(result, skipped)
                    return
                parse_s, workers, records, per_proc_mb = self._measure_parse(
                    drive, cancel, 12, 55
                )
                if cancel.is_set():
                    self._finish_merged(result, skipped)
                    return
                single_s = parse_s * max(1, workers)
                ratio = single_s / total_read_s if total_read_s > 0 else 0.0
                suggest = (
                    1 if ratio <= 0 else max(1, min(_MAX_PROCS, int(math.ceil(ratio))))
                )
                result["procs"] = {
                    "mode": "procs",
                    "timed_out": False,
                    "suggest": int(suggest),
                    "ratio": round(ratio, 2),
                    "read_s": round(total_read_s, 2),
                    "probe_s": round(read_s, 2),
                    "parse_s": round(parse_s, 2),
                    "single_parse_s": round(single_s, 2),
                    "records": int(records),
                    "workers": int(workers),
                    "max_procs": int(_MAX_PROCS),
                    "per_proc_mb": per_proc_mb,
                }
                # 第一段测完就落盘，第二段即使超时这一段也不白测
                store.save_perf_test(drive, {"procs": result["procs"]})
            else:
                skipped = i18n.t(
                    "需要以管理员身份运行程序且目标盘为 NTFS，本次只测了目录遍历。",
                    "Administrator rights on an NTFS drive are required; only the directory walk was measured.",
                )

            if cancel.is_set():
                self._finish_merged(result, skipped)
                return

            # 第二段：目录遍历，占整体进度的后半段
            def _tick(done: float, total: int) -> None:
                pct = self._span(
                    int(100 * float(done) / total) if total else 0, 55, 96
                )
                self._emit_progress("threads", done, total, pct)

            data = dirprobe.measure_dir_threads(drive_root, cancel, on_progress=_tick)
            if data.get("points"):
                result["threads"] = {
                    "mode": "threads",
                    # 用户取消时这一组也只是部分档位，不能当成完整曲线存下
                    "timed_out": bool(self._mft_test_timed_out or cancel.is_set()),
                    "points": data.get("points") or [],
                    "mem": data.get("mem") or [],
                    "sample": data.get("sample"),
                    "sample_seconds": data.get("sample_seconds"),
                    "levels": data.get("levels") or [],
                }
                store.save_perf_test(drive, {"threads": result["threads"]})

            # 只要取消过就按中断收尾：用户取消发 cancelled，超时发带部分数据的 done。
            # 不能因为第二段已经跑出几个点就当成正常完成，那会把取消当成功。
            if cancel.is_set():
                self._finish_merged(result, skipped)
                return

            self._emit(
                "mft-proc-test-done",
                {
                    "mode": "all",
                    "timed_out": bool(self._mft_test_timed_out),
                    "mft_skipped": skipped,
                    "procs": result["procs"],
                    "threads": result["threads"],
                    "pct": 100,
                    "elapsed_s": round(time.perf_counter() - self._mft_test_start, 1),
                },
            )
        except Exception as exc:  # noqa: BLE001
            if cancel.is_set():
                self._finish_merged(result, skipped)
                return
            applog.exception("perf test failed", exc)
            self._emit(
                "mft-proc-test-error",
                {"message": i18n.t(f"测试失败：{exc}", f"Test failed: {exc}")},
            )
        finally:
            timer.cancel()

    def _finish_merged(self, result: dict, skipped: str) -> None:
        """中途停下时交回已经测到的段落；用户取消与超时给不同事件。"""
        if not getattr(self, "_mft_test_timed_out", False):
            self._emit("mft-proc-test-cancelled", {})
            return
        self._emit(
            "mft-proc-test-done",
            {
                "mode": "all",
                "timed_out": True,
                "mft_skipped": skipped,
                "procs": result.get("procs") or {},
                "threads": result.get("threads") or {},
                "timeout_s": int(_TIMEOUT_S),
                "pct": int(self._mft_test_partial.get("pct", 0) or 0),
            },
        )

    def _run_mft_proc_test(self, root: str) -> None:
        cancel = self._mft_test_cancel
        self._mft_test_timed_out = False
        self._mft_test_partial = {}
        self._mft_test_start = time.perf_counter()
        # 一次性定时器，只对这一轮生效。本轮结束时在 finally 里取消，
        # 否则上一轮的计时器会继续数到点，把下一轮测试当成超时掐掉。
        timer = threading.Timer(_TIMEOUT_S, self._on_timeout)
        timer.daemon = True
        timer.start()
        try:
            located = _drive_letter_and_root(root)
            if not located:
                raise RuntimeError(
                    i18n.t(
                        "只能测试整个盘符，请选择盘符根目录。",
                        "Only a whole drive can be tested; pick the drive root.",
                    )
                )
            drive = located[0]
            read_s, total_read_s = self._measure_read_speed(drive, cancel)
            self._mft_test_partial = {
                "read_s": round(total_read_s, 2),
                "probe_s": round(read_s, 2),
            }
            if cancel.is_set():
                self._finish_interrupted()
                return
            parse_s, workers, records, per_proc_mb = self._measure_parse(drive, cancel)
            if cancel.is_set():
                self._finish_interrupted()
                return
            # 读盘与解析是重叠的，实测墙钟约等于「解析总量除以进程数」，
            # 乘回实际进程数即单进程解析总量。乘数不能用常量，进程池建不起来
            # 退化成单进程时这里必须跟着变成 1，否则建议值会凭空翻倍。
            single_s = parse_s * max(1, workers)
            ratio = single_s / total_read_s if total_read_s > 0 else 0.0
            if ratio <= 0:
                suggest = 1
            else:
                suggest = max(1, min(_MAX_PROCS, int(math.ceil(ratio))))
            result = {
                "mode": "procs",
                "timed_out": False,
                "read_s": round(total_read_s, 2),
                "single_parse_s": round(single_s, 2),
                "per_proc_mb": per_proc_mb,
                "workers": int(workers),
            }
            # 实测结果直接写进配置，下次打开设置页不用重测
            store.save_perf_test(drive, result)
            self._emit(
                "mft-proc-test-done",
                {
                    "mode": "procs",
                    "timed_out": False,
                    "suggest": int(suggest),
                    "ratio": round(ratio, 2),
                    "read_s": round(total_read_s, 2),
                    "probe_s": round(read_s, 2),
                    "parse_s": round(parse_s, 2),
                    "single_parse_s": round(single_s, 2),
                    "records": int(records),
                    "workers": int(workers),
                    "max_procs": int(_MAX_PROCS),
                    # 实测到的每个解析进程占用，0 表示没采到，界面据此不显示内存
                    "per_proc_mb": per_proc_mb,
                    "pct": 100,
                    "parse_ratio": 1.0,
                },
            )
        except Exception as exc:  # noqa: BLE001
            if cancel.is_set():
                self._finish_interrupted()
                return
            applog.exception("mft proc test failed", exc)
            self._emit(
                "mft-proc-test-error",
                {
                    "message": i18n.t(
                        f"测试失败：{exc}", f"Test failed: {exc}"
                    )
                },
            )
        finally:
            timer.cancel()

    def _run_dir_thread_test(self, root: str) -> None:
        """目录遍历线程数的测试线程体。"""
        cancel = self._mft_test_cancel
        self._mft_test_timed_out = False
        self._mft_test_partial = {}
        self._mft_test_start = time.perf_counter()
        timer = threading.Timer(_TIMEOUT_S, self._on_timeout)
        timer.daemon = True
        timer.start()
        try:
            def _tick(done: float, total: int) -> None:
                # done 是浮点，含当前档内已跑的比例，进度条因此连续推进
                pct = int(6 + 90 * float(done) / total) if total else 6
                self._emit_progress("threads", done, total, min(96, pct))

            data = dirprobe.measure_dir_threads(root, cancel, on_progress=_tick)
            if cancel.is_set() and not data.get("points"):
                self._finish_interrupted()
                return
            located = _drive_letter_and_root(root)
            if located and data.get("points"):
                store.save_perf_test(
                    located[0],
                    {
                        "mode": "threads",
                        # 取消也说明这一轮没跑完，不能当成完整曲线存下
                        "timed_out": bool(self._mft_test_timed_out or cancel.is_set()),
                        "points": data.get("points") or [],
                        "mem": data.get("mem") or [],
                        "sample": data.get("sample"),
                        "sample_seconds": data.get("sample_seconds"),
                        "levels": data.get("levels") or [],
                    },
                )
            self._emit(
                "mft-proc-test-done",
                {
                    "mode": "threads",
                    "timed_out": bool(self._mft_test_timed_out or cancel.is_set()),
                    "points": data.get("points") or [],
                    "mem": data.get("mem") or [],
                    "sample": data.get("sample"),
                    "sample_seconds": data.get("sample_seconds"),
                    "levels": data.get("levels") or [],
                    "note": data.get("note") or "",
                    "pct": 100,
                    "parse_ratio": 1.0,
                },
            )
        except Exception as exc:  # noqa: BLE001
            applog.exception("dir thread test failed", exc)
            self._emit(
                "mft-proc-test-error",
                {"message": i18n.t(f"测试失败：{exc}", f"Test failed: {exc}")},
            )
        finally:
            timer.cancel()

    def _on_timeout(self) -> None:
        """到点仍没结束就请求停止测试，并记下这是超时而非用户取消。"""
        self._mft_test_timed_out = True
        self._mft_test_cancel.set()

    def _finish_interrupted(self) -> None:
        """测试被打断时的收尾：超时就把已经量到的数据交回界面。"""
        if not getattr(self, "_mft_test_timed_out", False):
            self._emit("mft-proc-test-cancelled", {})
            return
        partial = dict(getattr(self, "_mft_test_partial", {}) or {})
        self._emit(
            "mft-proc-test-done",
            {
                "timed_out": True,
                "suggest": 0,
                "ratio": 0.0,
                "read_s": partial.get("read_s", 0.0),
                "probe_s": partial.get("probe_s", 0.0),
                "parse_s": 0.0,
                "single_parse_s": 0.0,
                "records": 0,
                "workers": _TEST_WORKERS,
                "max_procs": int(_MAX_PROCS),
                "per_proc_mb": 0,
                "timeout_s": int(_TIMEOUT_S),
                "pct": int(partial.get("pct", 0) or 0),
                "parse_ratio": float(partial.get("parse_ratio", 0.0) or 0.0),
            },
        )

    def _span(self, pct: int, lo: int, hi: int) -> int:
        """把 0 到 100 的局部进度映射到整体进度的某一段。"""
        if hi <= lo:
            return lo
        return int(lo + (hi - lo) * max(0, min(100, pct)) / 100.0)

    def _measure_read_speed(
        self, drive: str, cancel, lo: int = 0, hi: int = 100
    ) -> tuple[float, float]:
        """读一小段主文件表量读速，按字节数外推全表读盘时间。"""
        handle = open_volume(drive)
        try:
            boot = parse_boot(read_at(handle, 0, 512))
            runs, usable, _rec = resolve_mft_layout(handle, boot)
            if usable <= 0:
                raise RuntimeError(
                    i18n.t("这份卷的 MFT 读不到。", "The volume's MFT is unreadable.")
                )
            probe = max(1, min(usable, _PROBE_BYTES))
            buf = bytearray(probe)
            self._emit_progress("read", 0, probe, self._span(2, lo, hi))
            t0 = time.perf_counter()
            fill_mft_buffer(
                handle,
                boot,
                buf,
                runs=runs,
                usable=probe,
                drive_letter=drive,
                apply_usa_now=False,
                cancel=cancel.is_set,
            )
            elapsed = max(1e-6, time.perf_counter() - t0)
            self._emit_progress("read", probe, probe, self._span(8, lo, hi))
            return elapsed, usable / (probe / elapsed)
        finally:
            close_handle(handle)

    def _measure_parse(
        self, drive: str, cancel, lo: int = 0, hi: int = 100
    ) -> tuple[float, int, int, float]:
        """用两个进程把全表读一遍并解析，返回墙钟时长与每个进程的实测占用。

        内存取进程树私有内存的峰值减去测试前的基线，再除以实际进程数。
        采不到时返回 0，界面据此不显示内存，不拿常量凑数。
        """
        handle = open_volume(drive)
        try:
            boot = parse_boot(read_at(handle, 0, 512))
            last = [0.0]
            # 只量解析子进程自己的内存：主进程在解析阶段要收结果、建父子关系表，
            # 把这部分平摊到每个解析进程上，再乘回进程数会成倍虚高。
            try:
                base_mb = memprobe.children_private_bytes() / (1024 * 1024)
            except Exception:  # noqa: BLE001 - 采不到就不显示内存
                base_mb = None
            peak_mb = [base_mb or 0.0]

            def _tick(phase: str, done: int, total: int) -> None:
                now = time.perf_counter()
                if now - last[0] < _PROGRESS_GAP and done < total:
                    return
                last[0] = now
                # 借进度节拍顺带采一次解析进程内存，取峰值
                try:
                    cur_mb = memprobe.children_private_bytes() / (1024 * 1024)
                    if cur_mb > peak_mb[0]:
                        peak_mb[0] = cur_mb
                except Exception:  # noqa: BLE001
                    pass
                pct = 10 + int(88 * done / total) if total > 0 else 10
                pct = self._span(min(98, pct), lo, hi)
                self._mft_test_partial["pct"] = pct
                self._mft_test_partial["parse_ratio"] = (
                    round(done / total, 3) if total > 0 else 0.0
                )
                self._emit_progress(phase, done, total, pct)

            t0 = time.perf_counter()
            result = read_and_parse_mft(
                handle,
                boot,
                drive_letter=drive,
                workers=_TEST_WORKERS,
                on_bytes=lambda done, total: _tick("read", done, total),
                on_parse=lambda done, total: _tick("parse", done, total),
                cancel=cancel.is_set,
            )
            elapsed = max(1e-6, time.perf_counter() - t0)
            self._emit_progress("parse", 1, 1, hi)
            workers = int(getattr(result, "workers", _TEST_WORKERS) or _TEST_WORKERS)
            per_proc = 0.0
            if base_mb is not None and workers > 0:
                grown = peak_mb[0] - base_mb
                if grown > 0:
                    per_proc = round(grown / workers, 1)
            return (
                elapsed,
                workers,
                int(getattr(result, "n_records", 0) or 0),
                per_proc,
            )
        finally:
            close_handle(handle)

    def _emit_progress(self, phase: str, done: int, total: int, pct: int) -> None:
        started = getattr(self, "_mft_test_start", None)
        payload = {
            "phase": phase,
            "done": round(float(done), 2),
            "total": int(total),
            "pct": int(max(0, min(100, pct))),
            "elapsed_s": round(time.perf_counter() - started, 1) if started else 0.0,
        }
        try:
            payload.update(memprobe.sample())
        except Exception:  # noqa: BLE001 - 取不到资源占用不影响测试本身
            payload["cpu_pct"] = 0.0
            payload["mem_mb"] = 0.0
        self._emit("mft-proc-test-progress", payload)
