"""组装前端 JS-API：各功能域 Mixin 叠加到 ApiBase 上构成 Api。

pywebview 自动暴露实例的全部公开方法，Mixin 拆分不影响方法发现。
方法命名与返回结构保持与拆分前一致（前端 state.api 依赖）。
"""

from app.api.base import ApiBase
from app.api.compare import CompareMixin
from app.api.linkfind import LinkFindMixin
from app.api.migrate import MigrateMixin
from app.api.misc import MiscMixin
from app.api.scan import ScanMixin
from app.api.settings import SettingsMixin
from app.api.snapshots import SnapshotsMixin
from app.api.window import WindowMixin


class Api(
    SnapshotsMixin,
    ScanMixin,
    CompareMixin,
    LinkFindMixin,
    MigrateMixin,
    SettingsMixin,
    WindowMixin,
    MiscMixin,
    ApiBase,
):
    """暴露给前端 JS 的完整接口。"""


__all__ = ["Api", "ApiBase"]
