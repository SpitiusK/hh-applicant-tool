"""Approval-loop helpers (П.13).

- should_escalate() — политика решения по AIResponse + approval_cfg.
- persist_ai_decision() — запись в ai_decisions (audit log).
- escalate_to_user() — создание pending_messages + отправка approval_request
  в мессенджер.

Все три функции не зависят от конкретного мессенджера: messenger передаётся
через аргумент (MessengerClient из П.12). Сохранение в БД идёт через
storage_facade (хранит ссылки на репозитории pending_messages / ai_decisions
из П.7/П.8).
"""

from __future__ import annotations

import logging
from typing import Any

from .ai.base import AIError
from .ai.prompts import AI_RESPONSE_JSON_SUFFIX
from .ai.schema import AIResponse
from .messaging import ApprovalRequest, MessengerClient  # noqa: F401
from .storage.models.ai_decision import AiDecisionModel
from .storage.models.pending_message import PendingMessageModel

logger = logging.getLogger(__name__)


DEFAULT_CONFIDENCE_THRESHOLD = 0.7


def should_escalate(
    ai_response: AIResponse,
    action_type: str,
    approval_cfg: dict[str, Any],
) -> bool:
    """Решение: нужна ли эскалация человеку.

    - mode="never" → всегда автономно (даже если AI попросил escalate).
    - mode="always" → всегда эскалировать.
    - mode="on_escalation" (дефолт) → эскалация если
      AI.escalate=True, confidence<threshold или action_type в
      always_escalate_actions.
    """
    mode = approval_cfg.get("mode", "on_escalation")
    if mode == "never":
        return False
    if mode == "always":
        return True

    if ai_response.escalate:
        return True

    threshold = approval_cfg.get(
        "confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD
    )
    if ai_response.confidence < threshold:
        return True

    always_actions = approval_cfg.get("always_escalate_actions", []) or []
    if action_type in always_actions:
        return True

    return False


def persist_ai_decision(
    storage: Any,
    *,
    operation: str,
    ai_response: AIResponse,
    status: str = "auto_dispatched",
    vacancy_id: int | None = None,
    negotiation_id: int | None = None,
    model: str | None = None,
    result_preview: str | None = None,
    messenger: Any = None,
    approval_cfg: dict[str, Any] | None = None,
) -> int | None:
    """Записать строку в ai_decisions. Вернуть id или None при ошибке.

    Для status='auto_dispatched' запускается sanity-sampling (П.15):
    с частотой approval_cfg['sanity_frequency'] (default 20) запись
    помечается sample_for_review=True и шлётся retro-summary в
    мессенджер. Если messenger=None — сэмпл ставится, отправка
    пропускается.
    """
    if result_preview is None:
        answer = ai_response.answer
        result_preview = (
            answer if isinstance(answer, str) else str(answer)
        )[:200]
    try:
        decision_id = storage.ai_decisions.create(
            AiDecisionModel(
                operation=operation,
                vacancy_id=vacancy_id,
                negotiation_id=negotiation_id,
                model=model,
                confidence=ai_response.confidence,
                escalated=bool(ai_response.escalate),
                escalation_reason=ai_response.escalation_reason,
                is_sentinel=bool(ai_response.is_sentinel),
                iterations=0,
                status=status,
                result_preview=result_preview,
            )
        )
    except Exception:
        logger.exception(
            "persist_ai_decision: не удалось записать ai_decision (op=%s)",
            operation,
        )
        return None

    if status == "auto_dispatched" and decision_id is not None:
        from .messaging.sanity import send_for_retro_review, should_sample

        frequency = int(
            (approval_cfg or {}).get("sanity_frequency", 20)
        )
        if should_sample(decision_id, frequency):
            try:
                storage.ai_decisions.mark_sample_for_review(decision_id)
            except Exception:
                logger.exception(
                    "persist_ai_decision: mark_sample_for_review упал #%s",
                    decision_id,
                )
            else:
                send_for_retro_review(messenger, storage, decision_id)

    return decision_id


def escalate_to_user(
    storage: Any,
    messenger: MessengerClient | None,
    *,
    action_type: str,
    draft_payload: dict[str, Any],
    ai_response: AIResponse,
    approval_cfg: dict[str, Any],
    messenger_type: str = "telegram",
) -> int | None:
    """Создать pending_messages(status='pending') и уведомить пользователя.

    Возвращает pending_messages.id или None при ошибке (тогда вызывающий
    код должен выбрать fallback — например, пропустить действие, чтобы
    не выполнить его автономно в обход approval).
    """
    try:
        pm_id = storage.pending_messages.create(
            PendingMessageModel(
                messenger_type=messenger_type,
                action_type=action_type,
                draft_payload=draft_payload,
                status="pending",
                question_for_user=ai_response.question_for_user,
                context_summary=ai_response.context_summary,
                confidence=ai_response.confidence,
                escalation_reason=ai_response.escalation_reason,
                iterations=0,
            )
        )
    except Exception:
        logger.exception(
            "escalate_to_user: не удалось создать pending_messages"
        )
        return None

    if messenger is None:
        logger.warning(
            "escalate_to_user: messenger не инициализирован, pm#%s остаётся "
            "в pending (подхватит бот при следующем /pending)",
            pm_id,
        )
        return pm_id

    text = _format_approval_text(
        action_type=action_type,
        draft_payload=draft_payload,
        ai_response=ai_response,
    )
    try:
        external_id = messenger.send_approval_request(
            draft_id=pm_id,
            text=text,
            actions=["approve", "modify", "reject"],
        )
        storage.pending_messages.update(
            pm_id, messenger_message_id=external_id
        )
    except Exception:
        logger.exception(
            "escalate_to_user: send_approval_request упал, pm#%s остаётся в pending",
            pm_id,
        )

    return pm_id


def _format_approval_text(
    *,
    action_type: str,
    draft_payload: dict[str, Any],
    ai_response: AIResponse,
) -> str:
    answer = ai_response.answer
    answer_str = answer if isinstance(answer, str) else str(answer)
    preview = (draft_payload.get("preview") or answer_str or "").strip()

    lines = [
        "⚠️ <b>Нужен твой ввод</b>",
        f"Действие: <code>{action_type}</code>",
    ]
    # Шапка: вакансия / работодатель / ссылки. Одинаковая для apply_vacancy
    # и reply_employer — оба несут vacancy_name/employer_name/url в payload.
    vacancy_name = draft_payload.get("vacancy_name")
    if vacancy_name:
        lines.append(f"Вакансия: <b>{vacancy_name}</b>")
    employer_name = draft_payload.get("employer_name")
    if employer_name:
        lines.append(f"Работодатель: {employer_name}")
    vacancy_url = draft_payload.get("vacancy_url")
    if vacancy_url:
        lines.append(f"Вакансия: {vacancy_url}")
    chat_url = draft_payload.get("chat_url")
    if chat_url:
        lines.append(f"Чат: {chat_url}")
    last_msgs = draft_payload.get("last_employer_messages") or []
    if last_msgs:
        lines.append("")
        lines.append("<i>Последние сообщения работодателя:</i>")
        for _m in last_msgs:
            _preview = (_m or "").strip().replace("\n", " ")
            if len(_preview) > 350:
                _preview = _preview[:350] + "…"
            lines.append(f"• {_preview}")

    if ai_response.context_summary:
        lines.append(f"Контекст: {ai_response.context_summary}")
    if ai_response.question_for_user:
        lines.append(f"Вопрос: {ai_response.question_for_user}")
    if ai_response.escalation_reason:
        lines.append(
            f"Почему эскалирую: <i>{ai_response.escalation_reason}</i>"
        )
    lines.append(
        f"Confidence: <code>{ai_response.confidence:.2f}</code>"
    )
    lines.append("")
    lines.append("Предложение AI:")
    if preview:
        lines.append(f"<pre>{preview[:1500]}</pre>")
    else:
        # Пустой letter означает, что вакансия не требует сопроводительного
        # (hh.ru response_letter_required=False и --force-message не задан).
        # POST /negotiations пойдёт с пустым message — по сути «клик Откликнуться».
        lines.append("<i>не требуется (вакансия без сопроводительного)</i>")
    return "\n".join(lines)


def generate_with_self_assessment(
    ai_client: Any,
    prompt: str,
    *,
    fallback_confidence: float = 0.95,
) -> AIResponse:
    """Единообразный вызов AI с возвратом AIResponse.

    1. Пробует ai_client.complete_json(prompt + JSON-инструкция,
       response_model=AIResponse). Для ChatClaude на sentinel вернёт
       AIResponse(is_sentinel=True, escalate=True). Для ChatOpenAI
       (до реализации complete_json) поймает NotImplementedError.
    2. Fallback: ai_client.complete(prompt) + обёртка в AIResponse
       с fallback_confidence и escalate=False. Это сохраняет работу
       на OpenAI-бэкенде до доделки П.3-стиля complete_json для него.
    3. AIError → sentinel AIResponse(is_sentinel=True, escalate=True,
       reason="ai_unclear").
    """
    suffixed_prompt = f"{prompt}\n\n{AI_RESPONSE_JSON_SUFFIX}"
    try:
        resp = ai_client.complete_json(
            suffixed_prompt, response_model=AIResponse
        )
        if isinstance(resp, AIResponse):
            return resp
    except NotImplementedError:
        logger.debug(
            "complete_json не реализован у %s, fallback к complete()",
            type(ai_client).__name__,
        )
    except AIError as ex:
        logger.warning(
            "complete_json упал AIError: %s; возвращаем sentinel", ex
        )
        return AIResponse(
            answer="",
            confidence=0.0,
            escalate=True,
            escalation_reason="ai_unclear",
            is_sentinel=True,
        )

    try:
        text = ai_client.complete(prompt)
    except AIError as ex:
        logger.warning(
            "complete() упал AIError: %s; возвращаем sentinel", ex
        )
        return AIResponse(
            answer="",
            confidence=0.0,
            escalate=True,
            escalation_reason="ai_unclear",
            is_sentinel=True,
        )
    return AIResponse(
        answer=text,
        confidence=fallback_confidence,
        escalate=False,
    )


__all__ = [
    "should_escalate",
    "persist_ai_decision",
    "escalate_to_user",
    "generate_with_self_assessment",
]
