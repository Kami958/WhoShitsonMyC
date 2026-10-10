"""历史入口：实现已拆到 :mod:`modules.ai.service_pkg`，此模块保留导入兼容。

新代码请直接从 ``modules.ai.service_pkg`` 导入。
"""

from modules.ai.service_pkg import (
    AiService,
    PUBLIC_METHODS,
    _ai_field_log_value,
    _diff_ai_config,
    _format_messages_for_log,
    _format_response_for_log,
    _norm_model_options,
)

__all__ = [
    "AiService",
    "PUBLIC_METHODS",
    "_ai_field_log_value",
    "_diff_ai_config",
    "_format_messages_for_log",
    "_format_response_for_log",
    "_norm_model_options",
]
