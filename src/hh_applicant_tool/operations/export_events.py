"""Экспорт подтверждённых events в календарь (П.24).

По config.events.calendar.exporter: `ics` — VCALENDAR-файл, `markdown` —
append в agenda.md. Пишет только events.status='confirmed' (статус
ставит пользователь при Approve в TG или auto при высокой confidence).

Hand-rolled VCALENDAR — без icalendar-библиотеки, хватает минимального
подмножества для импорта в Google Calendar / Apple Calendar / etc.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Literal

from ..main import BaseNamespace, BaseOperation
from ..storage.models.event import EventModel

if TYPE_CHECKING:
    from ..main import HHApplicantTool


logger = logging.getLogger(__package__)


class Namespace(BaseNamespace):
    output: Path | None
    format: Literal["ics", "markdown"] | None


class Operation(BaseOperation):
    """Экспорт confirmed-events в .ics или markdown."""

    __aliases__ = ("export-events", "export")

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--output",
            type=Path,
            default=None,
            help="Путь файла для экспорта. Default — из config.events.calendar.output_path.",
        )
        parser.add_argument(
            "--format",
            choices=["ics", "markdown"],
            default=None,
            help="Формат экспорта. Default — из config.events.calendar.exporter (ics).",
        )

    def run(
        self,
        tool: HHApplicantTool,
        args: Namespace,
    ) -> None:
        events_cfg = tool.get_events_cfg()
        calendar_cfg = events_cfg["calendar"]
        fmt = args.format or calendar_cfg.get("exporter") or "ics"
        default_output = calendar_cfg.get("output_path") or "data/calendar.ics"
        output: Path = args.output or Path(default_output)

        events_repo = getattr(tool.storage, "events", None)
        if events_repo is None:
            logger.error(
                "export-events: storage.events не доступен (П.23 pending?)"
            )
            print("❌ events-репозиторий недоступен")
            return

        events = list(events_repo.list_by_status("confirmed", limit=500))

        output.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "ics":
            content = _render_ics(events)
            output.write_text(content, encoding="utf-8")
        elif fmt == "markdown":
            _append_markdown(output, events)
        else:
            raise ValueError(f"Неизвестный формат: {fmt}")

        logger.info(
            "export-events: exported %d events to %s (%s)",
            len(events),
            output,
            fmt,
        )
        print(f"✅ exported {len(events)} events to {output} ({fmt})")


# ---------------------------------------------------------- ics rendering


def _ics_dt(dt: datetime) -> str:
    """UTC-Z формат YYYYMMDDTHHMMSSZ."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.strftime("%Y%m%dT%H%M%SZ")


def _ics_escape(text: str) -> str:
    """Экранирование по RFC 5545: \\ , ; → \\\\ \\, \\;; newline → \\n."""
    return (
        text.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _render_ics(events: Iterable[EventModel]) -> str:
    now_stamp = _ics_dt(datetime.now(timezone.utc))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//hh-applicant-tool//agent//RU",
        "CALSCALE:GREGORIAN",
    ]
    for ev in events:
        if ev.when_ts is None:
            # без даты событие не ложится на календарь — пропускаем,
            # пусть живёт в agenda.md если надо.
            continue
        dtstart = _ics_dt(ev.when_ts)
        # Дефолт — часовое событие; точные длительности hh.ru не отдаёт.
        dtend_dt = ev.when_ts
        try:
            from datetime import timedelta

            dtend = _ics_dt(dtend_dt + timedelta(hours=1))
        except Exception:
            dtend = dtstart
        summary = _ics_escape(ev.title or f"{ev.type} #{ev.id}")
        description_parts = []
        if ev.raw_text:
            description_parts.append(ev.raw_text)
        if ev.confidence is not None:
            description_parts.append(f"confidence={ev.confidence:.2f}")
        if ev.negotiation_id:
            description_parts.append(f"negotiation={ev.negotiation_id}")
        if ev.vacancy_id:
            description_parts.append(f"vacancy={ev.vacancy_id}")
        description = _ics_escape("; ".join(description_parts))
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:hh-event-{ev.id}@hh-applicant-tool",
                f"DTSTAMP:{now_stamp}",
                f"DTSTART:{dtstart}",
                f"DTEND:{dtend}",
                f"SUMMARY:{summary}",
                f"DESCRIPTION:{description}",
                f"CATEGORIES:{_ics_escape(ev.type)}",
                "END:VEVENT",
            ]
        )
    lines.append("END:VCALENDAR")
    lines.append("")  # финальный \n
    return "\r\n".join(lines)


# ---------------------------------------------------------- markdown appender


def _append_markdown(path: Path, events: Iterable[EventModel]) -> None:
    if not path.exists():
        path.write_text(
            "# Agenda\n\nАвто-экспорт подтверждённых событий (hh-applicant-tool).\n\n",
            encoding="utf-8",
        )
    chunks: list[str] = []
    for ev in events:
        when = ev.when_ts.isoformat() if ev.when_ts else "—"
        chunks.append(
            f"## {when} — {ev.type}: {ev.title or ''}\n"
            f"- type: {ev.type}\n"
            f"- when: {when}\n"
            f"- negotiation_id: {ev.negotiation_id or '—'}\n"
            f"- vacancy_id: {ev.vacancy_id or '—'}\n"
            f"- confidence: {ev.confidence if ev.confidence is not None else '—'}\n"
            f"- event_id: {ev.id}\n"
        )
    if chunks:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n")
            f.write("\n".join(chunks))
            f.write("\n")
