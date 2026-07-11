"""WhoShitsOnMyC —— 磁盘空间对比工具。

对比同一目录树在两个时间点的磁盘占用，定位「哪个路径下的内容变多了」。

包结构::

    core/       纯 Python 核心引擎（不依赖 GUI，可独立测试）
      models.py     数据结构定义
      snapshot.py   SQLite 快照读写
      scanner.py    磁盘遍历、生成快照
      differ.py     两份快照对比、生成变化树
    app.py      pywebview 桥接层（连接前端与核心引擎）
    web/        HTML/CSS/JS 前端界面
"""

__version__ = "0.1.0"
