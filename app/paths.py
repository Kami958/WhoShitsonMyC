"""运行时资源路径：开发态取仓库目录，打包态取解包目录。"""

from __future__ import annotations

import os
import sys



_RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


_WEB_DIR = os.path.join(_RES_DIR, "web")


_ICON_PATH = os.path.join(_RES_DIR, "logo.ico")
