"""OpenAI 兼容的 embedding 适配器（DeepSeek 本身没有 embedding）。

未配置 / 调用失败时返回 None，检索自动降级为关键词。
"""

import asyncio
import logging
import time
from collections import OrderedDict

from openai import AsyncOpenAI

from app.config import settings
from app.memory.store import MemoryStore
from app.memory.vectors import pack_vector

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None
_query_cache: OrderedDict[str, list[float]] = OrderedDict()
_CACHE_MAX = 256
_BATCH = 32
_disabled_until = 0.0


def is_enabled() -> bool:
    return bool(
        settings.EMBEDDING_ENABLED
        and settings.EMBEDDING_API_KEY
    )


def _get_client() -> AsyncOpenAI | None:
    global _client
    if not is_enabled():
        return None
    if _client is None:
        base_url = settings.EMBEDDING_BASE_URL or "https://api.openai.com/v1"
        _client = AsyncOpenAI(
            api_key=settings.EMBEDDING_API_KEY,
            base_url=base_url,
            timeout=settings.EMBEDDING_TIMEOUT,
            max_retries=1,
        )
    return _client


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    global _disabled_until
    client = _get_client()
    if client is None or not texts:
        return None
    if time.monotonic() < _disabled_until:
        return None
    vectors: list[list[float]] = []
    try:
        for start in range(0, len(texts), _BATCH):
            batch = texts[start:start + _BATCH]
            resp = await asyncio.wait_for(
                client.embeddings.create(model=settings.EMBEDDING_MODEL, input=batch),
                timeout=min(5.0, settings.EMBEDDING_TIMEOUT),
            )
            order = sorted(resp.data, key=lambda d: d.index)
            vectors.extend([list(item.embedding) for item in order])
    except Exception as e:
        _disabled_until = time.monotonic() + 60
        logger.warning(f"embedding 调用失败，降级关键词 60 秒: {type(e).__name__}: {e}")
        return None
    return vectors if len(vectors) == len(texts) else None


async def embed_query(text: str) -> list[float] | None:
    text = (text or "").strip()
    if not text or not is_enabled():
        return None
    key = f"{settings.EMBEDDING_MODEL}|{text}"
    if key in _query_cache:
        _query_cache.move_to_end(key)
        return _query_cache[key]
    vectors = await embed_texts([text])
    if not vectors:
        return None
    vector = vectors[0]
    _query_cache[key] = vector
    if len(_query_cache) > _CACHE_MAX:
        _query_cache.popitem(last=False)
    return vector


async def embed_memory_ids(store: MemoryStore, memory_ids: list[int]) -> int:
    if not is_enabled() or not memory_ids:
        return 0
    rows = [store.get_memory(mid) for mid in memory_ids]
    rows = [row for row in rows if row]
    if not rows:
        return 0
    vectors = await embed_texts([row["text"] for row in rows])
    if not vectors:
        return 0
    for row, vector in zip(rows, vectors):
        store.set_vector(
            "memory", int(row["id"]), settings.EMBEDDING_MODEL,
            pack_vector(vector), len(vector),
        )
    return len(vectors)


async def embed_topic_ids(store: MemoryStore, topic_ids: list[int]) -> int:
    if not is_enabled() or not topic_ids:
        return 0
    rows = [store.get_topic(tid) for tid in topic_ids]
    rows = [row for row in rows if row]
    if not rows:
        return 0
    texts = [
        " ".join(
            [row.get("name") or "", row.get("keywords") or "",
             row.get("summary") or ""]
        ).strip()
        for row in rows
    ]
    vectors = await embed_texts(texts)
    if not vectors:
        return 0
    for row, vector in zip(rows, vectors):
        store.set_vector(
            "topic", int(row["id"]), settings.EMBEDDING_MODEL,
            pack_vector(vector), len(vector),
        )
    return len(vectors)


async def reindex(store: MemoryStore, limit: int = 300) -> dict[str, int]:
    """为缺失向量的记忆/主题补向量，返回补了多少。"""
    if not is_enabled():
        return {"memories": 0, "topics": 0}
    memory_ids = store.memory_ids_missing_vectors(settings.EMBEDDING_MODEL, limit=limit)
    topic_ids = store.topic_ids_missing_vectors(settings.EMBEDDING_MODEL, limit=limit)
    return {
        "memories": await embed_memory_ids(store, memory_ids),
        "topics": await embed_topic_ids(store, topic_ids),
    }
