"""AI 服务对前端暴露的方法白名单。"""

from __future__ import annotations

PUBLIC_METHODS = frozenset(
    {
        "get_config",
        "set_config",
        "list_models",
        "test_connection",
        "ask",
        "continue_tools",
        "cancel",
        "reset",
        # 对比树清理：有限递归多切片（不真删；context 交前端再 ask）
        "start_compare_cleanup",
        "next_compare_cleanup",
        "cancel_compare_cleanup",
    }
)
