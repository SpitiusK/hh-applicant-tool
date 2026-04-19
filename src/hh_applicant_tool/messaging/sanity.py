"""Sanity-check sampling (П.15).

Идея: при автономном исполнении действия на каждую N-ную запись
в ai_decisions (N=sanity_frequency) помечаем sample_for_review=True
и шлём retrospective-summary в TG. Пользователь может позже
/flag <decision_id> — это подсвечивает запись как калибровочный
контрпример для промптов, но действие НЕ откатывается (оно уже
выполнено).
"""

from __future__ import annotations

import logging
import random
from typing import Any

logger = logging.getLogger(__name__)


def should_sample(ai_decision_id: int, frequency: int) -> bool:
    """Одна запись из frequency попадает в sanity-выборку."""
    if frequency <= 0:
        return False
    return random.randint(1, frequency) == 1


def send_for_retro_review(
    messenger: Any,
    storage: Any,
    ai_decision_id: int,
) -> None:
    """Прочитать ai_decisions по id и отправить summary в TG."""
    if messenger is None:
        logger.debug(
            "send_for_retro_review: messenger is None, skip #%s",
            ai_decision_id,
        )
        return

    try:
        repo = storage.ai_decisions
        rec = repo.get(ai_decision_id)
    except Exception:
        logger.exception(
            "send_for_retro_review: не прочитать ai_decisions #%s",
            ai_decision_id,
        )
        return

    if rec is None:
        logger.warning(
            "send_for_retro_review: запись #%s не найдена",
            ai_decision_id,
        )
        return

    text_lines = [
        f"🔍 <b>Retrospective review</b> #{ai_decision_id}",
        f"operation: <code>{rec.operation}</code>",
    ]
    if rec.confidence is not None:
        text_lines.append(f"confidence: <code>{rec.confidence:.2f}</code>")
    if rec.vacancy_id:
        text_lines.append(f"vacancy: {rec.vacancy_id}")
    if rec.negotiation_id:
        text_lines.append(f"negotiation: {rec.negotiation_id}")
    if rec.is_sentinel:
        text_lines.append("⚠️ sentinel=True")
    if rec.result_preview:
        text_lines.append("")
        text_lines.append(f"<pre>{rec.result_preview}</pre>")
    text_lines.append("")
    text_lines.append(
        f"Если решение выглядит неудачным — /flag {ai_decision_id}"
    )

    try:
        messenger.send_notification("\n".join(text_lines))
    except Exception:
        logger.exception(
            "send_for_retro_review: send_notification упал для #%s",
            ai_decision_id,
        )


__all__ = ["should_sample", "send_for_retro_review"]
