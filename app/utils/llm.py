import logging
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from app.config import settings
from app.utils import usage

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(slots=True)
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0


def get_llm() -> AsyncOpenAI | None:
    """惰性创建 LLM 客户端；未配置有效 Key 时返回 None。"""
    global _client
    if _client is None and settings.llm_configured:
        _client = AsyncOpenAI(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
            timeout=settings.LLM_TIMEOUT,
            max_retries=1,
        )
    return _client


async def complete(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    kind: str = "chat",
) -> LLMResponse | None:
    """一次完整的 chat completion，支持工具调用。

    返回 ``None`` 表示这次调用没有发生（未配置 / 超预算 / 网络错误），
    由调用方决定兜底策略。
    """
    client = get_llm()
    if client is None:
        logger.debug("LLM 未配置，跳过调用")
        return None

    if usage.should_block_call():
        return None

    kwargs: dict[str, Any] = {
        "model": settings.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens or settings.LLM_MAX_TOKENS,
    }
    if (
        settings.LLM_THINKING in ("enabled", "disabled")
        and ("flash" in settings.LLM_MODEL or "v4" in settings.LLM_MODEL)
    ):
        kwargs["extra_body"] = {"thinking": {"type": settings.LLM_THINKING}}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    try:
        resp = await client.chat.completions.create(**kwargs)
    except Exception as e:
        logger.error(f"LLM 调用失败: {type(e).__name__}: {e}")
        return None

    stats = getattr(resp, "usage", None)
    prompt_tokens = getattr(stats, "prompt_tokens", 0) or 0
    completion_tokens = getattr(stats, "completion_tokens", 0) or 0
    if stats is not None:
        usage.record_usage(settings.LLM_MODEL, prompt_tokens, completion_tokens, kind=kind)

    message = resp.choices[0].message
    calls = [
        ToolCall(
            id=tc.id,
            name=tc.function.name,
            arguments=tc.function.arguments or "{}",
        )
        for tc in (getattr(message, "tool_calls", None) or [])
    ]
    content = (message.content or "").strip() or None
    return LLMResponse(
        content=content,
        tool_calls=calls,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


async def chat(
    message: str,
    system_prompt: str | None = None,
    *,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    kind: str = "chat",
) -> str | None:
    """单轮调用 LLM，失败或超预算时返回 None（由调用方决定兜底文案）。"""
    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": message})

    resp = await complete(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        kind=kind,
    )
    return resp.content if resp else None
