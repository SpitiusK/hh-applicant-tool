"""Журналы событий и подтверждений для reply-agent.

Пишем в JSONL (append-only) + дублируем в markdown для человекочитаемости.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__package__)


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _append_jsonl(path: Path, entry: dict) -> None:
    _ensure_parent(path)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False))
        f.write("\n")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_when(when: str | None) -> datetime | None:
    if not when:
        return None
    try:
        # поддерживаем как полный ISO, так и без таймзоны
        return datetime.fromisoformat(when.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def append_event(
    event: dict,
    *,
    chat_context: dict,
    jsonl_path: Path,
    md_path: Path,
) -> None:
    """Добавляет назначенную встречу/задачу в журнал.

    event: {"type": "call|meeting|task", "when": "ISO-8601 or None",
            "title": str, "notes": str}
    chat_context: {"chat_id", "vacancy_id", "vacancy_url",
                   "vacancy_name", "employer_name"}
    """
    entry = {
        "timestamp": _now_iso(),
        **chat_context,
        "type": event.get("type", "task"),
        "when": event.get("when"),
        "title": event.get("title", "").strip(),
        "notes": event.get("notes", "").strip(),
    }
    _append_jsonl(jsonl_path, entry)
    _append_agenda_md(entry, md_path)
    logger.info(
        "event logged: %s @ %s for %s",
        entry["title"],
        entry.get("when") or "unscheduled",
        entry.get("vacancy_name") or "?",
    )


def _append_agenda_md(entry: dict, md_path: Path) -> None:
    _ensure_parent(md_path)

    when_dt = _parse_when(entry.get("when"))
    date_header = (
        when_dt.strftime("## %Y-%m-%d")
        if when_dt
        else "## Без даты"
    )
    time_str = when_dt.strftime("%H:%M") if when_dt else "—"

    emoji = {
        "call": "📞",
        "meeting": "📅",
        "task": "📝",
    }.get(entry["type"], "•")

    vacancy_name = entry.get("vacancy_name") or "?"
    employer = entry.get("employer_name") or "?"
    url = entry.get("vacancy_url") or ""
    title = entry.get("title") or entry["type"]
    notes = entry.get("notes") or ""

    link = (
        f"[{vacancy_name} @ {employer}]({url})"
        if url
        else f"{vacancy_name} @ {employer}"
    )
    line = f"- **{time_str}** {emoji} {title} - {link}"
    if notes:
        line += f"\n  - {notes}"

    # Простейший append: если текущий date_header уже последний в файле - не дублируем
    existing = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    if not existing.rstrip().endswith(date_header):
        if existing and not existing.endswith("\n"):
            existing += "\n"
        existing += f"\n{date_header}\n\n"
    existing += line + "\n"
    md_path.write_text(existing, encoding="utf-8")


def append_confirmation(
    confirmation: dict,
    *,
    chat_context: dict,
    proposed_reply: str,
    jsonl_path: Path,
) -> None:
    """Добавляет запрос на подтверждение в очередь.

    confirmation: {"question": str, "reason": str}
    """
    entry: dict[str, Any] = {
        "timestamp": _now_iso(),
        **chat_context,
        "question": confirmation.get("question", "").strip(),
        "reason": confirmation.get("reason", "").strip(),
        "proposed_reply": proposed_reply.strip(),
        "status": "pending",
    }
    _append_jsonl(jsonl_path, entry)
    logger.info(
        "confirmation queued: %s (%s)",
        entry["question"],
        entry.get("vacancy_name") or "?",
    )


__all__ = ["append_event", "append_confirmation"]
