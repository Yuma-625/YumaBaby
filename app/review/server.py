"""独立的本地标注台：读取当天回复，逐条打 0–10 分，提交后导出数据集。

启动：

    python -m app.review.server
    # 打开 http://127.0.0.1:8100

只监听 127.0.0.1，不对外网开放。
"""

import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.memory.store import MemoryStore
from app.review.page import PAGE

logger = logging.getLogger(__name__)


def _today() -> str:
    try:
        tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date().isoformat()


def _check_date(day: str | None) -> str:
    day = day or _today()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise HTTPException(status_code=422, detail="date 格式应为 YYYY-MM-DD")
    return day


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = MemoryStore(settings.MEMORY_DB)
    logger.info(f"标注台已连接数据库: {settings.MEMORY_DB}")
    try:
        yield
    finally:
        app.state.store.close()


app = FastAPI(title="小弗标注台", lifespan=lifespan)


def get_store(request: Request) -> MemoryStore:
    return request.app.state.store


class ScoreRequest(BaseModel):
    message_id: int
    score: int = Field(ge=0, le=10)
    note: str = ""


class FinishRequest(BaseModel):
    date: str


@app.get("/", response_class=HTMLResponse)
async def index():
    return PAGE


@app.get("/api/items")
async def items(date: str | None = None, store: MemoryStore = Depends(get_store)):
    day = _check_date(date)
    return {
        "date": day,
        "items": store.review_items(day),
        "stats": store.score_stats(day),
        "finished": store.review_status(day),
    }


@app.get("/api/stats")
async def stats(date: str | None = None, store: MemoryStore = Depends(get_store)):
    day = _check_date(date)
    return store.score_stats(day)


@app.post("/api/score")
async def score(req: ScoreRequest, store: MemoryStore = Depends(get_store)):
    message = store._conn.execute(
        "SELECT id, chat_id, role, created_at FROM messages WHERE id=?", (req.message_id,)
    ).fetchone()
    if message is None:
        raise HTTPException(status_code=404, detail="消息不存在")
    if message["role"] != "assistant":
        raise HTTPException(status_code=400, detail="只能给机器人的回复打分")

    try:
        store.upsert_score(
            req.message_id, message["chat_id"], req.score, note=req.note.strip()
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    day = (message["created_at"] or "")[:10] or _today()
    return {"ok": True, "score": req.score, "stats": store.score_stats(day)}


@app.post("/api/finish")
async def finish(req: FinishRequest, store: MemoryStore = Depends(get_store)):
    day = _check_date(req.date)
    rows = store.export_dataset(day)
    if not rows:
        raise HTTPException(status_code=400, detail="这一天还没有已打分的回复")

    out_dir = Path(settings.DATASETS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{day}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    store.mark_review_finished(day, str(path), len(rows))
    store.add_event("dataset_export", day, "", {"path": str(path), "count": len(rows)})
    logger.info(f"数据集已导出: {path} ({len(rows)} 条)")

    return {
        "exported": len(rows),
        "path": str(path),
        "stats": store.score_stats(day),
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    uvicorn.run(
        "app.review.server:app",
        host=settings.REVIEW_HOST,
        port=settings.REVIEW_PORT,
        reload=False,
    )


if __name__ == "__main__":
    main()
