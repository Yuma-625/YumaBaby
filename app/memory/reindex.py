"""为缺失向量的记忆/主题补向量。

用法：
    .\.venv\Scripts\python.exe -m app.memory.reindex
"""

import asyncio
import json

from app.config import settings
from app.memory.embeddings import is_enabled, reindex
from app.memory.store import MemoryStore


def main() -> int:
    if not is_enabled():
        print("EMBEDDING_ENABLED=false 或未配置 EMBEDDING_API_KEY，跳过")
        return 1
    store = MemoryStore(settings.MEMORY_DB)
    try:
        result = asyncio.run(reindex(store, limit=2000))
        print(json.dumps(result, ensure_ascii=False))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
