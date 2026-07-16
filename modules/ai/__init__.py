"""AI 服务模块入口。

``create(ctx)`` 由 modules.discover 调用；返回的实例经
``Api.module_invoke`` 按 PUBLIC_METHODS 白名单分发。
"""

from __future__ import annotations

from typing import Any

from modules.ai.service import AiService


def create(ctx: dict) -> AiService:
    """用注入的 ctx 构造 AI 服务实例。"""
    return AiService(ctx)


# 兼容 discover 直接读 create
__all__ = ["create", "AiService"]
