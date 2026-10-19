import logging
from typing import Any

from app.skills.base import BaseSkill
from app.skills.stock.handler import StockSkill
from app.skills.fund.handler import FundSkill
from app.skills.chat.handler import ChatSkill

logger = logging.getLogger(__name__)


class AgentCore:
    """智能体核心 - 技能注册 & 意图分发"""

    def __init__(self):
        self.skills: list[BaseSkill] = [
            StockSkill(),
            FundSkill(),
            ChatSkill(),
        ]

    def register_skill(self, skill: BaseSkill):
        self.skills.append(skill)
        logger.info(f"技能已注册: {skill.name}")

    async def process(self, message: str, context: dict[str, Any] | None = None) -> str:
        """处理消息, 自动匹配技能并执行"""
        reply, _ = await self.process_with_skill(message, context)
        return reply

    async def process_with_skill(
        self, message: str, context: dict[str, Any] | None = None
    ) -> tuple[str, str]:
        """同 process，但额外返回命中的技能名（兜底对话返回 chat）。"""
        context = context or {}

        for skill in self.skills:
            if skill.match(message):
                logger.info(f"命中技能: {skill.name}")
                return await skill.execute(message, context), skill.name

        chat_skill = next((s for s in self.skills if s.name == "chat"), None)
        if chat_skill:
            logger.info("未命中技能，使用兜底对话")
            return await chat_skill.execute(message, context), chat_skill.name
        return "抱歉，我还不太理解你的意思。", "none"

    @property
    def skill_list(self) -> list[dict[str, str]]:
        return [{"name": s.name, "description": s.description} for s in self.skills]


agent = AgentCore()
