"""界面语言状态（中/英）。

全程序（前端与后端）共享同一个语言值：前端启动时按系统语言判定，
经 ``Api.set_language`` 同步到这里；后端各处会冒泡到界面的报错用
:func:`t` 取对应语言的文案。默认英文——中文系统会在启动时被切回中文。
"""

from __future__ import annotations

_LANG = "en"


def set_lang(lang: str) -> None:
    """设置当前界面语言；只认 ``"zh"`` 与 ``"en"``，其余一律按英文。"""
    global _LANG
    _LANG = "zh" if lang == "zh" else "en"


def get_lang() -> str:
    """返回当前界面语言（``"zh"`` 或 ``"en"``）。"""
    return _LANG


def t(zh: str, en: str) -> str:
    """按当前语言在中/英文案之间二选一。"""
    return zh if _LANG == "zh" else en
