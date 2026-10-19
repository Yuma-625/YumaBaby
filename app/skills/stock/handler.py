from typing import Any
from app.skills.base import BaseSkill


class StockSkill(BaseSkill):
    name = "stock"
    description = "股票行情查询、K线分析"
    keywords = ["股票", "股价", "涨停", "跌停", "K线", "行情", "stock", "大盘"]

    async def execute(self, message: str, context: dict[str, Any]) -> str:
        # TODO: 接入 akshare 实际查询
        return f"[股票分析] 收到查询: {message}（功能开发中）"
