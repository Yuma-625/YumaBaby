from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    source: Optional[str] = None
    group_id: Optional[str] = None
    user_id: Optional[str] = None


class MessageResponse(BaseModel):
    reply: str
    skill_used: Optional[str] = None


class StockInfo(BaseModel):
    code: str
    name: str
    current_price: float
    change_pct: float
    volume: Optional[int] = None
    high: Optional[float] = None
    low: Optional[float] = None


class FundInfo(BaseModel):
    code: str
    name: str
    nav: float
    acc_nav: float
    update_date: date


class BroadcastRequest(BaseModel):
    message: str
    groups: Optional[list[str]] = None
