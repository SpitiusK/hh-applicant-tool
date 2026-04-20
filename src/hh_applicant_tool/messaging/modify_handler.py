"""Modify-handler: re-generate draft по комментарию пользователя (П.14).

Используется FSM-веткой bot'а (см. run_polling Modify-flow). Принимает
user_comment, перегенерирует draft_payload через AI с подмешанной
коррекцией, проверяет confidence/escalate, решает одно из трёх:

- rejected (max_iterations exhausted) → status='rejected',
  escalation_reason='user_rejected_max_iter'.
- approved (новая генерация уверенная) → status='approved', send-approved
  подхватит на следующем cron-такте.
- re-escalated (всё ещё неуверенно) → status='pending', iterations++,
  бот отправит новое approval_request (без дубля в pending_messages).

Каждая итерация пишется в ai_decisions(status='modified', iterations=N).

Возврат: {"status", "iteration", "reason"}.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from ..ai.claude import ChatClaude
from ..approval import (
    generate_with_self_assessment,
    persist_ai_decision,
    should_escalate,
)
from ..storage.models.pending_message import PendingMessageModel

logger = logging.getLogger(__name__)


def _build_regeneration_prompt(
    pm: PendingMessageModel,
    user_comment: str,
    persona: str = "",
) -> str:
    """Собрать prompt для перегенерации с учётом user-комментария."""
    payload = pm.draft_payload or {}
    action_type = pm.action_type
    context_summary = pm.context_summary or ""
    previous_message = payload.get("message") or payload.get(
        "answer"
    ) or ""

    lines: list[str] = []
    if persona:
        lines.append("# PROFESSIONAL PROFILE (контекст для ответов от первого лица)")
        lines.append("")
        lines.append(persona.strip())
        lines.append("")
    lines.extend(
        [
            "Ты помогаешь пересобрать ранее сгенерированный ответ с учётом коррекции от пользователя.",
            f"Тип действия: {action_type}",
        ]
    )
    if context_summary:
        lines.append(f"Контекст: {context_summary}")
    if action_type == "apply_vacancy":
        vn = payload.get("vacancy_name") or ""
        if vn:
            lines.append(f"Вакансия: {vn}")
        en = payload.get("employer_name") or ""
        if en:
            lines.append(f"Работодатель: {en}")
        vu = payload.get("vacancy_url") or ""
        if vu:
            lines.append(f"Ссылка на вакансию: {vu}")
        sf, st, sc = (
            payload.get("salary_from"),
            payload.get("salary_to"),
            payload.get("salary_currency"),
        )
        if sf or st:
            lines.append(
                f"Зарплата: {sf or '?'}–{st or '?'} {sc or ''}".strip()
            )
        exp = payload.get("experience")
        if exp:
            lines.append(f"Требуемый опыт: {exp}")
        sch = payload.get("schedule")
        if sch:
            lines.append(f"График: {sch}")
        vd = payload.get("vacancy_description") or ""
        if vd:
            lines.append("")
            lines.append("Описание вакансии (с hh.ru):")
            lines.append(vd[:6000])
    elif action_type == "reply_employer":
        vn = payload.get("vacancy_name") or ""
        en = payload.get("employer_name") or ""
        if vn or en:
            lines.append(f"Вакансия: {vn}; работодатель: {en}")

    lines.append("")
    lines.append("Предыдущий вариант ответа (его нужно улучшить):")
    if previous_message:
        lines.append(str(previous_message)[:2000])
    else:
        lines.append("(пусто — вакансия не требовала сопроводительного, но пользователь хочет добавить)")
    lines.append("")
    lines.append(
        "КОРРЕКЦИЯ ОТ ПОЛЬЗОВАТЕЛЯ:"
    )
    lines.append(user_comment)
    lines.append("")
    lines.append(
        "Перегенерируй ответ с учётом коррекции и professional profile. "
        "Пиши от первого лица, без AI-tells. Сохрани тон и стиль. "
        "Верни только новый текст ответа (plain text) в поле answer."
    )
    return "\n".join(lines)


def _get_ai_client(
    config: dict[str, Any], action_type: str
) -> ChatClaude:
    """Получить ChatClaude для перегенерации.

    Единый бэкенд — Claude (на нём уже построен approval-loop,
    complete_json поддерживает AIResponse через П.6). OpenAI-путь
    для modify не задействован — это осознанный narrow scope.
    """
    claude_cfg = config.get("claude", {})
    return ChatClaude(
        model=claude_cfg.get("model"),
        timeout=claude_cfg.get("timeout", 120.0),
        rate_limit=claude_cfg.get("rate_limit", 10),
    )


def handle_modify(
    storage: Any,
    pending_id: int,
    user_comment: str,
    config: dict[str, Any],
    *,
    messenger: Any = None,
) -> dict[str, Any]:
    """Обработать Modify-итерацию. См. module docstring."""
    repo = storage.pending_messages
    pm: PendingMessageModel | None = repo.get_by_id(pending_id)
    if pm is None:
        return {
            "status": "not_found",
            "iteration": 0,
            "reason": f"pending_messages id={pending_id} не найден",
        }

    approval_cfg = dict(config.get("approval", {}) or {})
    max_iter = int(approval_cfg.get("max_iterations", 3))
    threshold = float(approval_cfg.get("confidence_threshold", 0.7))

    # История drafts (предыдущий draft уходит в history перед генерацией нового).
    history: list[dict[str, Any]] = list(pm.draft_history or [])
    history.append(
        {
            "at": datetime.now().isoformat(),
            "draft_payload": pm.draft_payload,
            "user_comment": user_comment,
            "iteration": pm.iterations or 0,
        }
    )
    new_iteration = (pm.iterations or 0) + 1

    if new_iteration > max_iter:
        repo.update(
            pending_id,
            status="rejected",
            escalation_reason="user_rejected_max_iter",
            iterations=new_iteration - 1,  # не инкрементим, просто фиксируем
            draft_history=history,
        )
        return {
            "status": "rejected",
            "iteration": new_iteration,
            "reason": "max_iter",
        }

    # Перегенерация — подмешиваем persona, чтобы AI знал, кто он
    # (без неё regen в вакууме: только vacancy_name и предыдущий draft).
    from ..ai.context import get_persona_context
    from pathlib import Path
    persona_cfg = (config.get("persona") or {})
    config_dir = persona_cfg.get("source_reports_dir") or "/app/config"
    # Если у persona есть свой path — get_persona_context его использует
    persona_text = get_persona_context(config, Path(config_dir))

    ai_client = _get_ai_client(config, pm.action_type)
    prompt = _build_regeneration_prompt(pm, user_comment, persona=persona_text)
    ai_resp = generate_with_self_assessment(ai_client, prompt)

    new_answer = (
        ai_resp.answer
        if isinstance(ai_resp.answer, str)
        else json.dumps(ai_resp.answer, ensure_ascii=False)
    )

    new_draft_payload = dict(pm.draft_payload or {})
    # message для apply_vacancy и reply_employer; answer — общий ключ.
    if "message" in new_draft_payload or pm.action_type in (
        "apply_vacancy",
        "reply_employer",
    ):
        new_draft_payload["message"] = new_answer
    new_draft_payload["answer"] = new_answer

    # Запись в ai_decisions об этой итерации (без зависимости от исхода).
    persist_ai_decision(
        storage,
        operation=pm.action_type,
        ai_response=ai_resp,
        status="modified",
        result_preview=new_answer[:200],
    )

    escalated = ai_resp.escalate or ai_resp.confidence < threshold or ai_resp.is_sentinel

    if not escalated:
        repo.update(
            pending_id,
            status="approved",
            iterations=new_iteration,
            draft_payload=new_draft_payload,
            draft_history=history,
            confidence=ai_resp.confidence,
            escalation_reason=None,
            question_for_user=None,
            context_summary=ai_resp.context_summary,
        )
        return {
            "status": "approved",
            "iteration": new_iteration,
            "reason": "confidence_ok",
        }

    # re-escalate: оставляем в pending, но обновляем payload + question.
    repo.update(
        pending_id,
        status="pending",
        iterations=new_iteration,
        draft_payload=new_draft_payload,
        draft_history=history,
        confidence=ai_resp.confidence,
        escalation_reason=ai_resp.escalation_reason or "ai_unclear",
        question_for_user=ai_resp.question_for_user,
        context_summary=ai_resp.context_summary,
    )

    if messenger is not None:
        try:
            from ..approval import _format_approval_text  # re-use formatter

            text = _format_approval_text(
                action_type=pm.action_type,
                draft_payload=new_draft_payload,
                ai_response=ai_resp,
            )
            text = (
                f"♻️ Итерация {new_iteration} из {max_iter}\n\n"
                + text
            )
            external_id = messenger.send_approval_request(
                draft_id=pending_id,
                text=text,
                actions=["approve", "modify", "reject"],
            )
            repo.update(
                pending_id, messenger_message_id=external_id
            )
        except Exception:
            logger.exception(
                "handle_modify: не удалось отправить re-escalation request для pm#%s",
                pending_id,
            )

    return {
        "status": "re_escalated",
        "iteration": new_iteration,
        "reason": ai_resp.escalation_reason or "ai_unclear",
    }


__all__ = ["handle_modify"]
