from typing import Any
from app.skills.base import BaseSkill


class FundSkill(BaseSkill):
    name = "fund"
    description = "基金净值查询、持仓分析"
    keywords = ["基金", "净值", "定投", "收益率", "fund", "持有"]

    async def execute(self, message: str, context: dict[str, Any]) -> str:
        # TODO: 接入 akshare 实际查询
        return f"[基金分析] 收到查询: {message}（功能开发中）"
