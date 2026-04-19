from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import BaseModel, mapped


class PendingMessageModel(BaseModel):
    id: int | None = None
    messenger_type: str
    messenger_message_id: str | None = None
    action_type: str
    draft_payload: dict[str, Any] = mapped(store_json=True, default_factory=dict)
    draft_history: list[dict[str, Any]] | None = mapped(
        store_json=True, default=None
    )
    status: str = "pending"
    question_for_user: str | None = None
    context_summary: str | None = None
    confidence: float | None = None
    escalation_reason: str | None = None
    iterations: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    dispatched_at: datetime | None = None
