"""平台适配器抽象基类：新增平台（如拉勾、牛客）时实现同一接口即可。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class PlatformAdapter(ABC):
    """招聘平台适配器：只读采集，不包含任何写操作。"""

    @abstractmethod
    def search(self, keyword: str, city_code: str, max_pages: int) -> list[dict]:
        """按关键词 + 城市搜索岗位列表，返回岗位 dict 列表。"""
        raise NotImplementedError

    @abstractmethod
    def fetch_detail(self, page: Any, item: dict) -> dict:
        """打开岗位详情页，补全 JD 正文等信息（只浏览，不点击任何按钮）。"""
        raise NotImplementedError
