"""pywebview 桥接层 —— 连接 HTML 前端与纯 Python 核心引擎。

前端（``web/``）通过 pywebview 的 JS-API 调用 :class:`Api` 上的方法：
扫描、列举快照、对比、下钻、删除等。扫描在后台线程运行，
进度经 ``window.evaluate_js`` 主动推给前端，界面全程不卡。

启动应用运行项目根目录的 ``app.py``。
"""

from app.api import Api

__all__ = ["Api"]
