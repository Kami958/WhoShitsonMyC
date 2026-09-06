"""窗口几何：侧边栏宽度与窗口宽度的换算。

本模块只做纯计算，不 import pywebview，便于单测（``tests/`` 不导入 app.py）。

宽度语义约定（重要）：
    ``settings.yaml`` 里的 ``window_width`` 存的是**侧边栏展开时**的窗口宽度。
    收起态的窗口宽度 = 存储宽度 - 侧边栏宽度。这样侧栏开合时主区（对比树）
    宽度恒定，只有窗口右边缘在动。

    pywebview winforms 后端全程使用 Form.Size（外框）的逻辑像素，
    ``create_window(width=)``、``window.resize()``、``events.resized`` 三者同单位，
    EdgeChromium 的 CSS 像素又与逻辑像素 1:1，故本模块不需要 DPI 换算。
"""

# 与 web/js/pending.js 的 _TOOL_PANEL_W_MIN / _MAX / _DEFAULT 必须一致，
# 否则拖动侧栏后重启会回弹。
PANEL_W_MIN = 240
PANEL_W_MAX = 640
PANEL_W_DEFAULT = 340


def clamp_panel_width(value) -> int:
    """把侧边栏宽度收到 [PANEL_W_MIN, PANEL_W_MAX]；非数字回默认。"""
    try:
        w = int(round(float(value)))
    except (TypeError, ValueError):
        return PANEL_W_DEFAULT
    return max(PANEL_W_MIN, min(w, PANEL_W_MAX))


def to_stored_width(actual_w, *, panel_open: bool, panel_width: int) -> int:
    """实际窗口宽度 → 存储宽度（侧栏展开时的宽度）。"""
    try:
        w = int(actual_w)
    except (TypeError, ValueError):
        w = 0
    if panel_open:
        return w
    return w + clamp_panel_width(panel_width)


def to_launch_width(stored_w, *, panel_open: bool, panel_width: int) -> int:
    """存储宽度 → 启动时的实际窗口宽度。"""
    try:
        w = int(stored_w)
    except (TypeError, ValueError):
        w = 0
    if panel_open:
        return w
    return w - clamp_panel_width(panel_width)
