from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AIResponse(BaseModel):
    """Единая схема self-assessment для всех AI-вызовов.

    Поля confidence / escalate позволяют вызывающему коду решить,
    отправлять ли результат на approval человеку. is_sentinel=True
    выставляется только для fallback-заглушек, когда модель вернула
    мусор или бэкенд упал — для различения в audit-логе осознанной
    эскалации от технической ошибки.
    """

    model_config = ConfigDict(extra="ignore")

    answer: str | dict
    confidence: float = Field(ge=0.0, le=1.0)
    escalate: bool
    escalation_reason: str | None = None
    question_for_user: str | None = None
    context_summary: str | None = None
    is_sentinel: bool = False
