from abc import ABC, abstractmethod
from typing import Any, Callable

MessageCallback = Callable[[dict[str, Any]], Any]


class BaseAdapter(ABC):
    """平台适配器基类 - 对接 QQ / 微信 / Discord 等"""

    @abstractmethod
    async def start(self):
        """启动适配器"""
        ...

    @abstractmethod
    async def stop(self):
        """停止适配器"""
        ...

    @abstractmethod
    async def send_group_msg(self, group_id: str, message: str):
        """发送群消息"""
        ...

    @abstractmethod
    async def send_private_msg(self, user_id: str, message: str):
        """发送私聊消息"""
        ...
