"""组装 AI 服务：各功能域 Mixin 叠加到 AiServiceBase 上构成 AiService。

经 ``Api.module_invoke`` 按 PUBLIC_METHODS 白名单调用，方法名与返回结构
保持与拆分前一致（前端 state.api 依赖）。
"""

from modules.ai.service_pkg.ask import AskMixin
from modules.ai.service_pkg.base import AiServiceBase
from modules.ai.service_pkg.cleanup import CleanupMixin
from modules.ai.service_pkg.config_api import ConfigMixin
from modules.ai.service_pkg.logfmt import (
    _ai_field_log_value,
    _diff_ai_config,
    _format_messages_for_log,
    _format_response_for_log,
    _norm_model_options,
)
from modules.ai.service_pkg.models import ModelsMixin
from modules.ai.service_pkg.public import PUBLIC_METHODS
from modules.ai.service_pkg.toolflow import ToolFlowMixin


class AiService(
    ConfigMixin,
    CleanupMixin,
    ModelsMixin,
    AskMixin,
    ToolFlowMixin,
    AiServiceBase,
):
    """AI 模块运行时实例。"""


__all__ = [
    "AiService",
    "AiServiceBase",
    "PUBLIC_METHODS",
    "_ai_field_log_value",
    "_diff_ai_config",
    "_format_messages_for_log",
    "_format_response_for_log",
    "_norm_model_options",
]
