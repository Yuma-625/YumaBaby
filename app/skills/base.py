from abc import ABC, abstractmethod
from typing import Any


class BaseSkill(ABC):
    name: str = "base"
    description: str = ""
    keywords: list[str] = []

    @abstractmethod
    async def execute(self, message: str, context: dict[str, Any]) -> str:
        ...

    def match(self, message: str) -> bool:
        return any(kw in message for kw in self.keywords)
