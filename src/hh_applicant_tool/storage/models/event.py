from __future__ import annotations

from datetime import datetime

from .base import BaseModel


class EventModel(BaseModel):
    id: int | None = None
    negotiation_id: int | None = None
    vacancy_id: int | None = None
    type: str
    title: str = ""
    when_ts: datetime | None = None
    source_msg_id: str | None = None
    raw_text: str | None = None
    confidence: float | None = None
    status: str = "detected"
    created_at: datetime | None = None
