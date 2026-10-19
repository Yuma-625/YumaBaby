import logging

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


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


async def chat(
    message: str,
    system_prompt: str | None = None,
    *,
    temperature: float = 0.7,
    max_tokens: int | None = None,
) -> str | None:
    """单轮调用 LLM，失败时返回 None（由调用方决定兜底文案）。"""
    client = get_llm()
    if client is None:
        logger.debug("LLM 未配置，跳过调用")
        return None

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": message})

    try:
        resp = await client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens or settings.LLM_MAX_TOKENS,
        )
        content = resp.choices[0].message.content
        return content.strip() if content else None
    except Exception as e:
        logger.error(f"LLM 调用失败: {type(e).__name__}: {e}")
        return None
