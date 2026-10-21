"""M0 演示：命令行里跑通「模型自己决定调不调工具」。

    python -m app.agent.demo                     # 离线演示（默认，不花钱）
    python -m app.agent.demo --live              # 走真实 DeepSeek API
    python -m app.agent.demo --message "帮我算 12 乘 34"
"""

import argparse
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.agent.loop import AgentLoop
from app.config import settings
from app.tools.registry import ToolRegistry
from app.utils.llm import LLMResponse, ToolCall


async def get_time() -> dict:
    now = datetime.now(ZoneInfo(settings.TIMEZONE))
    return {"time": now.strftime("%Y-%m-%d %H:%M:%S"), "timezone": settings.TIMEZONE}


def multiply(a: float, b: float) -> dict:
    return {"a": a, "b": b, "result": a * b}


async def echo_text(text: str) -> dict:
    return {"echo": text}


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.add(
        "get_time",
        "获取当前日期和时间。用户问“现在几点”“今天几号”时调用。",
        {"type": "object", "properties": {}, "required": []},
        get_time,
    )
    registry.add(
        "multiply",
        "计算两个数字的乘积。用户要求乘法运算时调用。",
        {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "被乘数"},
                "b": {"type": "number", "description": "乘数"},
            },
            "required": ["a", "b"],
        },
        multiply,
    )
    registry.add(
        "echo_text",
        "原样回显一段文本。用户要求复述时调用。",
        {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文本"}},
            "required": ["text"],
        },
        echo_text,
    )
    return registry


class ScriptedLLM:
    """离线剧本：先调一次 get_time，再给出最终答复。"""

    def __init__(self):
        self.calls = 0

    async def complete(self, messages, *, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="call_demo_1", name="get_time", arguments="{}")],
            )
        return LLMResponse(content="（离线演示）我查过时间了。接上真实 API 后，这里会是模型自己的回答。")


async def main() -> int:
    parser = argparse.ArgumentParser(description="YumaBaby agent M0 demo")
    parser.add_argument("--live", action="store_true", help="使用真实 LLM（消耗 token）")
    parser.add_argument("--message", default="现在几点了？", help="用户消息")
    parser.add_argument("--max-steps", type=int, default=4)
    args = parser.parse_args()

    registry = build_registry()
    if args.live:
        agent = AgentLoop(registry, max_steps=args.max_steps, max_tokens=300)
        if not settings.llm_configured:
            print("LLM_API_KEY 未配置，无法 --live；去掉该参数可跑离线演示。")
            return 2
    else:
        agent = AgentLoop(registry, llm=ScriptedLLM(), max_steps=args.max_steps)

    result = await agent.run(
        args.message,
        system_prompt="你是鱼宝宝，回答简洁。需要数据时调用工具，不要编造。",
    )

    print(f"\n模式: {'live' if args.live else '离线剧本'}")
    print(f"消息: {args.message}")
    print(f"停止: {result.stopped}（{result.steps} 步）")
    for item in result.trace:
        mark = "✓" if item["ok"] else "✗"
        print(f"  {mark} [{item['step']}] {item['tool']}({item['arguments']}) "
              f"{item['duration_ms']}ms -> {item['result']}")
    print(f"\n回答: {result.reply}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
