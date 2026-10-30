"""损坏配置文件的时间戳备份。

设置、链接登记与迁移历史在读取失败时，先把原文件复制成同目录下的
``<原名>.corrupt-时间戳.bak``，再按空表或默认值继续。复制失败时返回空串，
由调用方决定是否拒绝后续写入，避免把唯一原件覆盖掉。
"""

from __future__ import annotations

import os
import shutil
import time


def backup_corrupt_file(path: str) -> str:
    """把读取失败的 ``path`` 复制成带时间戳的副本，返回副本路径。

    文件不存在或复制失败时返回空串；不删除也不改动原文件。
    """
    if not path:
        return ""
    try:
        if not os.path.isfile(path):
            return ""
    except OSError:
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    base = f"{path}.corrupt-{stamp}.bak"
    candidate = base
    index = 1
    while True:
        try:
            if not os.path.exists(candidate):
                shutil.copy2(path, candidate)
                return candidate
        except OSError:
            return ""
        index += 1
        candidate = f"{base[:-4]}-{index}.bak"
