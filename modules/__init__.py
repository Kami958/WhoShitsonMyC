"""构建期可选模块的发现与注册。

被 build 排除的模块 import 失败即视为不存在，静默降级。
依赖方向：modules → core；core 与主流程对 modules 零反向 import
（仅 app.py 在启动时 try-import 本包）。
"""

from __future__ import annotations

from typing import Any, Callable


ModuleFactory = Callable[[dict], Any]


def discover() -> dict[str, ModuleFactory]:
    """发现可用模块，返回 ``{name: create}``。

    每个模块包应暴露 ``create(ctx) -> object``。
    import 失败（构建期排除 / 缺依赖）时跳过，不抛错，但写日志便于排查。
    """
    found: dict[str, ModuleFactory] = {}

    # 逐个 try-import；被 PyInstaller --exclude-module 裁掉时 ImportError
    try:
        from modules import ai as ai_mod  # noqa: WPS433 — 可选模块

        if hasattr(ai_mod, "create"):
            found["ai"] = ai_mod.create
        else:
            try:
                from core import applog

                applog.warn("module ai has no create(), skipped")
            except Exception:  # noqa: BLE001
                pass
    except ImportError as exc:
        # 常见：运行环境缺 httpx 等 AI 依赖；lite 包排除 AI 也走这里
        try:
            import sys

            from core import applog

            applog.warn(
                f"module ai unavailable: {exc}"
                f" | python={sys.executable}"
            )
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        try:
            import sys

            from core import applog

            applog.exception(
                f"module ai import failed | python={sys.executable}", exc
            )
        except Exception:  # noqa: BLE001
            pass

    return found
