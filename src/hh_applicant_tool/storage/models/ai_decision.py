from __future__ import annotations

import hashlib
from datetime import datetime

from .base import BaseModel


class AiDecisionModel(BaseModel):
    id: int | None = None
    operation: str
    vacancy_id: int | None = None
    negotiation_id: int | None = None
    prompt_hash: str | None = None
    model: str | None = None
    confidence: float | None = None
    escalated: bool = False
    escalation_reason: str | None = None
    is_sentinel: bool = False
    iterations: int = 0
    status: str
    result_preview: str | None = None
    sample_for_review: bool = False
    flagged: bool = False
    flag_reason: str | None = None
    created_at: datetime | None = None


def hash_prompt(prompt: str) -> str:
    """sha256 первых 4KB промпта — для дедупа в ai_decisions.prompt_hash."""
    return hashlib.sha256(prompt[:4096].encode("utf-8")).hexdigest()
