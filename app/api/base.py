"""Api 基类 —— 窗口引用、事件推送、线程/锁状态字段、可选模块调度。"""

from __future__ import annotations

import json
import threading

import webview  # pywebview

from core import applog, i18n, store
from core.differ import Diff
from titlebar import TitleBarTheme

from app.bootstrap import _window_title


class ApiBase:
    """暴露给前端 JS 的接口基类。所有方法的返回值都会被 pywebview 序列化为 JSON。

    约定：正常返回业务数据；可预期的错误以 ``{"error": "...消息..."}`` 返回，
    让前端弹友好提示而非崩溃。
    各功能域方法在同级 Mixin 中（snapshots/scan/compare/migrate/settings/
    window/linkfind/misc），最终在 ``app.api`` 组装成 :class:`Api`。
    """

    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._scan_thread: threading.Thread | None = None
        self._cancel = threading.Event()
        # 目录迁移类作业（迁移 / 再次迁移 / 还原）一次只跑一个，进度经事件推送。
        # 占位记的是「作业有没有在动文件」，不是「后台线程有没有退出」：
        # 线程把终态推给界面之前先放开，界面收到结果就能接着开下一项。
        self._link_job_lock = threading.Lock()
        self._link_job: str = ""
        # 当前占位的运行编号：释放只认编号，避免上一项的收尾清掉下一项的占位
        self._link_job_token = 0
        self._link_seq = 0
        self._migrate_cancel = threading.Event()
        self._remigrate_cancel = threading.Event()
        self._restore_cancel = threading.Event()
        # 设置应用 / 快照目录迁移（后台线程，避免阻塞 JS bridge 导致进度画不出来）
        self._settings_thread: threading.Thread | None = None
        # 迁移过程中的「后端问用户」：后端线程挂起等前端把选择送回来
        self._ask_lock = threading.Lock()
        self._ask_id = 0
        self._ask_result: str | None = None
        self._ask_event = threading.Event()
        # 当前对比会话，按需在多次下钻之间复用（避免每次重开连接）。
        # pywebview 的每次 JS-API 调用可能在不同线程执行，
        # SQLite 连接跨线程复用必须串行化，故加锁。
        self._diff: Diff | None = None
        self._diff_key: tuple[str, str] | None = None
        self._diff_lock = threading.Lock()
        self._window_size_timer: threading.Timer | None = None
        self._window_size_lock = threading.Lock()
        self._pending_window_size: tuple[int, int] | None = None
        # resized 事件最近一次上报的窗口尺寸；侧栏开合用它算目标宽度。
        # create_window 后由启动代码填入初值。
        self._last_win_w = 0
        self._last_win_h = 0
        # 侧栏为窗口加宽的量（运行期记账）：展开登记、收起收回。
        # resize_window_for_panel 靠它做幂等伸缩，快速开合不叠加漂移。
        self._panel_win_extra = 0
        self._panel_win_lock = threading.Lock()
        # 搜索是否进行中：cancel_search 只在此期间打断连接，避免误伤其他查询
        self._search_active = False
        # 独立反查任务（扫盘或查快照），同一时刻只允许一个
        self._link_find = None
        # 搜索预热回调代际：换会话后丢弃旧线程的 UI 推送
        self._preheat_token = 0
        # 标题栏主题（Windows 原生暗/浅色）；实现见 titlebar.py。
        self._titlebar = TitleBarTheme(
            get_window=lambda: self._window,
            get_title_candidates=lambda: (_window_title(), "WhoShitsOnMyC"),
        )
        # 构建期可选模块（AI 等）：discover 失败静默；core 零依赖 modules
        self._modules: dict[str, object] = {}
        self._init_modules()

    def set_window(self, window: "webview.Window") -> None:
        """由启动代码注入窗口引用，用于向前端推事件、开原生对话框。"""
        self._window = window

    def _emit(self, event: str, payload: dict) -> None:
        """向前端推送一个事件（调用前端注册的 ``window.__onPyEvent``）。"""
        if self._window is None:
            return
        data = json.dumps(payload, ensure_ascii=False)
        # 事件名与数据都经 JSON 编码，避免注入与转义问题。
        self._window.evaluate_js(
            f"window.__onPyEvent && window.__onPyEvent({json.dumps(event)}, {data})"
        )

    # ---- 迁移过程中问用户 -------------------------------------------------

    def _ask(self, kind: str, payload: dict, cancel: threading.Event | None) -> str:
        """向前端问一句并向当前线程要一个答复，没答就挂在这里等。

        ``kind`` 决定前端弹哪种框，``payload`` 是框里要显示的内容。返回
        ``continue`` / ``cancel`` / ``force``。迁移被别处取消了（用户点了
        队列上的取消）时不等答复，直接返回 ``cancel``，免得线程永远挂在
        一个没人理的弹窗上。pywebview 的 JS 调用与迁移线程不是同一个线程，
        所以这中间不需要释放任何锁。
        """
        with self._ask_lock:
            self._ask_id += 1
            ask_id = self._ask_id
            self._ask_result = None
            self._ask_event.clear()
        self._emit(
            "migrate-dir-ask",
            {"askId": int(ask_id), "kind": str(kind), **dict(payload or {})},
        )
        while True:
            if self._ask_event.wait(0.2):
                break
            if cancel is not None and cancel.is_set():
                with self._ask_lock:
                    self._ask_result = None
                    # 下一位询问重新发号，旧弹窗里再点一次也不会误伤新的那次
                    self._ask_id += 1
                return "cancel"
        with self._ask_lock:
            result = str(self._ask_result or "continue")
            self._ask_result = None
        return result

    def answer_migrate_ask(self, ask_id: int = 0, action: str = "continue") -> dict:
        """前端对 ``migrate-dir-ask`` 的回答；过期的编号一律忽略。"""
        act = str(action or "").strip().lower()
        if act not in ("continue", "cancel", "force"):
            act = "continue"
        with self._ask_lock:
            if int(ask_id or 0) != self._ask_id:
                return {
                    "error": i18n.t("这次询问已经过期", "This prompt has expired")
                }
            self._ask_result = act
            self._ask_event.set()
        return {"ok": True}

    # ---- 可选模块（AI 等） -------------------------------------------------

    def _init_modules(self) -> None:
        """发现并实例化构建期可选模块；失败静默。

        当前产品默认不启用 AI：``modules.discover`` 默认不注册 ai。
        代码与 ``modules/ai/`` 仍保留；重新接入见 ``assets/docs/ai-reenable.md``。
        """
        try:
            from modules import discover
        except ImportError:
            self._modules = {}
            return
        ctx = {
            "emit": self._emit,
            "app_data_dir": store.app_data_dir,
            "t": i18n.t,
            "get_lang": i18n.get_lang,
            # AI 清理 packing：一层子项，与 get_children 同源（AI 启用时才有消费者）
            "get_diff_children": self._get_diff_children_for_modules,
        }
        mods: dict[str, object] = {}
        try:
            factories = discover()
        except Exception as exc:  # noqa: BLE001
            applog.exception("module discover failed", exc)
            self._modules = {}
            return
        for name, factory in (factories or {}).items():
            try:
                inst = factory(ctx)
            except Exception as exc:  # noqa: BLE001
                applog.exception(f"module {name} create failed", exc)
                continue
            if inst is not None:
                mods[str(name)] = inst
        self._modules = mods
        if mods:
            applog.info(f"modules loaded: {', '.join(sorted(mods))}")
        else:
            # 默认路径：无 AI 时这里就是 (none)
            applog.info("modules loaded: (none)")

    def list_modules(self) -> dict:
        """返回可用模块表，如 ``{"ai": True}``。"""
        return {name: True for name in self._modules}

    def module_invoke(
        self, module: str, method: str, kwargs: dict | None = None
    ) -> dict:
        """统一分发到模块实例；只允许 PUBLIC_METHODS 白名单。

        不要给 Api 动态 setattr——pywebview 桥的方法枚举时机不可控。
        """
        name = str(module or "")
        meth = str(method or "")
        inst = self._modules.get(name)
        if inst is None:
            return {
                "error": i18n.t(
                    f"模块不可用：{name or '?'}",
                    f"Module unavailable: {name or '?'}",
                )
            }
        public = getattr(inst, "PUBLIC_METHODS", None)
        if public is None:
            public = frozenset()
        if meth not in public:
            return {
                "error": i18n.t(
                    f"方法不可用：{meth or '?'}",
                    f"Method unavailable: {meth or '?'}",
                )
            }
        fn = getattr(inst, meth, None)
        if not callable(fn):
            return {
                "error": i18n.t(
                    f"方法不可用：{meth or '?'}",
                    f"Method unavailable: {meth or '?'}",
                )
            }
        body = dict(kwargs or {})
        try:
            result = fn(**body)
        except TypeError as exc:
            return {
                "error": i18n.t(
                    f"参数错误：{exc}",
                    f"Invalid arguments: {exc}",
                )
            }
        except Exception as exc:  # noqa: BLE001
            applog.exception(f"module_invoke {name}.{meth} failed", exc)
            return {
                "error": i18n.t(
                    f"调用失败：{exc}",
                    f"Call failed: {exc}",
                )
            }
        if result is None:
            return {"ok": True}
        if isinstance(result, dict):
            return result
        return {"ok": True, "result": result}

    # ---- 快照列举 / 选择目录 -------------------------------------------
