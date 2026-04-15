"""Билдеры компактного контекста для reply-agent.

Полные данные Claude может получить через hh-get-* скилы по необходимости.
"""
from __future__ import annotations

from typing import Any


def build_compact_candidate(user_data: dict) -> str:
    """Краткое резюме кандидата (~1 KB).

    user_data берётся из config.json secci `form_user_data`.
    """
    if not user_data:
        return "Данные кандидата не заполнены."

    parts = []
    name = user_data.get("full_name") or user_data.get("first_name") or ""
    role = user_data.get("current_position") or ""
    company = user_data.get("current_company") or ""
    if name:
        parts.append(f"ФИО: {name}")
    if role:
        parts.append(f"Текущая должность: {role}" + (f" в {company}" if company else ""))
    if exp := user_data.get("total_experience_years"):
        parts.append(f"Общий опыт: {exp}")
    if summary := user_data.get("experience_summary"):
        parts.append(f"Краткое описание: {summary}")
    skills = user_data.get("skills") or []
    if skills:
        top = ", ".join(skills[:8])
        parts.append(f"Ключевые навыки: {top}")
    if city := user_data.get("city"):
        parts.append(f"Город: {city}")
    if fmt := user_data.get("work_format"):
        parts.append(f"Формат работы: {fmt}")
    if sal := user_data.get("salary_expectation"):
        parts.append(f"Ожидания по ЗП: {sal}")
    if tg := user_data.get("telegram"):
        parts.append(f"Telegram: {tg}")

    return "\n".join(parts)


def build_compact_vacancy(negotiation: dict) -> str:
    """Краткие данные о вакансии из объекта negotiation (~500 B).

    vacancy_id даётся явно, чтобы Claude мог при необходимости вызвать
    `hh-get-vacancy <id>` для полного описания.
    """
    vac = negotiation.get("vacancy") or {}
    vid = vac.get("id", "?")
    name = vac.get("name", "?")
    employer = (vac.get("employer") or {}).get("name", "?")
    employer_id = (vac.get("employer") or {}).get("id")
    area = (vac.get("area") or {}).get("name")
    salary = vac.get("salary") or {}
    url = vac.get("alternate_url", "")

    parts = [
        f"ID вакансии: {vid}",
        f"Название: {name}",
        f"Работодатель: {employer}" + (f" (id {employer_id})" if employer_id else ""),
    ]
    if area:
        parts.append(f"Локация: {area}")
    if salary.get("from") or salary.get("to"):
        parts.append(
            f"ЗП: {salary.get('from') or '?'} - {salary.get('to') or '?'} "
            f"{salary.get('currency', '')}"
        )
    if url:
        parts.append(f"URL: {url}")

    # Снипет из объекта negotiation (если API вернул)
    snippet = vac.get("snippet") or {}
    req = snippet.get("requirement")
    resp = snippet.get("responsibility")
    if req:
        parts.append(f"Требования (кратко): {req[:300]}")
    if resp:
        parts.append(f"Обязанности (кратко): {resp[:300]}")

    parts.append(
        "\n(Для полного описания, key_skills, schedule - вызови hh-get-vacancy {id})".format(id=vid)
    )
    return "\n".join(parts)


def format_chat_history(messages: list[dict]) -> str:
    """Форматирует ВСЕ сообщения чата (без обрезки)."""
    if not messages:
        return "(чат пуст)"

    lines = []
    for msg in messages:
        if not msg.get("text"):
            continue
        author_type = (msg.get("author") or {}).get("participant_type")
        author = "Работодатель" if author_type == "employer" else "Я"
        created_at = msg.get("created_at", "")
        lines.append(f"[{created_at}] {author}: {msg['text']}")
    return "\n".join(lines) if lines else "(чат пуст)"


def build_user_prompt(
    candidate: str,
    vacancy: str,
    chat: str,
    extra_note: str = "",
) -> str:
    """Собирает финальный user prompt."""
    blocks = [
        "<candidate>",
        candidate,
        "</candidate>",
        "",
        "<vacancy>",
        vacancy,
        "</vacancy>",
        "",
        "<chat_history>",
        chat,
        "</chat_history>",
    ]
    if extra_note:
        blocks += ["", "<note>", extra_note, "</note>"]
    blocks.append(
        "\nОтветь в формате JSON согласно <response_protocol> системного промпта."
    )
    return "\n".join(blocks)


def scrub_em_dash(text: str) -> str:
    """Заменяет em-dash «—» на обычный «-» (AI-tell cleanup)."""
    if not text:
        return text
    # U+2014 em dash, U+2013 en dash
    return text.replace("\u2014", " - ").replace("\u2013", "-")


__all__ = [
    "build_compact_candidate",
    "build_compact_vacancy",
    "format_chat_history",
    "build_user_prompt",
    "scrub_em_dash",
]


# Для удобства — алиас типа
NegotiationDict = dict[str, Any]
