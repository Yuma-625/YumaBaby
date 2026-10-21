"""最小 agent 循环。

一条消息进来，循环做三件事，直到模型不再调用工具：

1. 把「系统提示 + 历史 + 工具清单」交给模型
2. 模型要调工具 → 执行 → 把结果作为 tool 消息塞回去
3. 模型不再调工具 → 它的 content 就是答复

循环自带三道保险：步数上限、重复调用检测、工具超时（在注册表里）。
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.tools.registry import ToolRegistry
from app.utils import llm as llm_module

logger = logging.getLogger(__name__)

StepHook = Callable[[dict[str, Any]], None]


@dataclass
class AgentResult:
    reply: str
    stopped: str                 # done | max_steps | repeated | llm_unavailable | error
    steps: int = 0
    trace: list[dict[str, Any]] = field(default_factory=list)


class AgentLoop:
    def __init__(
        self,
        tools: ToolRegistry,
        *,
        llm: Any | None = None,
        max_steps: int = 6,
        temperature: float = 0.3,
        max_tokens: int | None = None,
        finalize_on_limit: bool = True,
        on_step: StepHook | None = None,
    ):
        self.tools = tools
        self.llm = llm or llm_module
        self.max_steps = max(1, max_steps)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.finalize_on_limit = finalize_on_limit
        self.on_step = on_step

    async def run(
        self,
        user_message: str,
        *,
        system_prompt: str = "",
        history: list[dict[str, Any]] | None = None,
        kind: str = "agent",
    ) -> AgentResult:
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(history or [])
        messages.append({"role": "user", "content": user_message})

        trace: list[dict[str, Any]] = []
        seen: dict[tuple[str, str], int] = {}

        for step in range(1, self.max_steps + 1):
            try:
                resp = await self.llm.complete(
                    messages,
                    tools=self.tools.schemas() or None,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    kind=kind,
                )
            except Exception as e:
                logger.exception("LLM 调用异常")
                return AgentResult(f"（模型调用出错：{type(e).__name__}）", "error", step, trace)

            if resp is None:
                return AgentResult("（模型暂时不可用，先不处理了）", "llm_unavailable", step, trace)

            if not resp.tool_calls:
                reply = (resp.content or "").strip()
                return AgentResult(reply or "（模型没有返回内容）", "done", step, trace)

            messages.append({
                "role": "assistant",
                "content": resp.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in resp.tool_calls
                ],
            })

            for call in resp.tool_calls:
                signature = (call.name, _normalize_arguments(call.arguments))
                if signature in seen:
                    logger.warning(f"检测到重复工具调用，终止: {call.name}")
                    return AgentResult(
                        "我好像卡在同一个操作上了，先停一下。",
                        "repeated",
                        step,
                        trace,
                    )
                seen[signature] = step

                result = await self.tools.run(call.name, call.arguments)
                entry = {
                    "step": step,
                    "tool": call.name,
                    "arguments": call.arguments,
                    "ok": result.ok,
                    "duration_ms": result.duration_ms,
                    "result": _preview(result.to_dict()),
                }
                trace.append(entry)
                if self.on_step:
                    self.on_step(entry)

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result.to_dict(), ensure_ascii=False, default=str),
                })

        if self.finalize_on_limit:
            messages.append({
                "role": "user",
                "content": "（工具调用次数已达上限）请基于已经拿到的信息直接回答，不要再调用工具。",
            })
            try:
                resp = await self.llm.complete(
                    messages,
                    tools=None,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    kind=f"{kind}_finalize",
                )
            except Exception:
                resp = None
            if resp and resp.content:
                return AgentResult(resp.content.strip(), "max_steps", self.max_steps, trace)

        return AgentResult("（步数用尽，先停在这里）", "max_steps", self.max_steps, trace)


def _normalize_arguments(raw: str) -> str:
    """把参数 JSON 规范化后用于重复检测；解析不了就原样比较。"""
    try:
        return json.dumps(json.loads(raw), sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return raw or ""


def _preview(data: Any, limit: int = 200) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"
