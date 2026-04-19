from __future__ import annotations

from typing import Literal

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


class EventClassification(AIResponse):
    """Классификация сообщения работодателя как event (П.22b).

    Наследует AIResponse (answer/confidence/escalate/escalation_reason/
    question_for_user/context_summary/is_sentinel) и добавляет поля
    детекции события. Для sentinel-fallback вспомогательные поля
    используют свои defaults (is_event=False, event_type=None, ...).
    """

    is_event: bool = False
    event_type: Literal["interview", "offer", "deadline"] | None = None
    when_iso: str | None = None
    title: str = ""
    notes: str = ""


class TestSolution(BaseModel):
    """Ответ модели на вопрос теста вакансии с выбором из вариантов.

    `selected_id` может прийти как int или str — hh.ru в разных местах
    присылает разные типы, нормализуем на стороне вызывающего кода.
    """

    model_config = ConfigDict(extra="ignore")

    selected_id: int | str
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    escalate: bool = False
