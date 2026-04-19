"""Одноразовая операция для генерации persona.md (П.17).

Читает каталог с аналитическими отчётами пользователя (okami-reports)
через Claude CLI и выдаёт professional profile в markdown. НЕ для cron —
только ручной запуск. Idempotent с подтверждением перезаписи.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ..ai.base import AIError
from ..ai.claude import ChatClaude
from ..main import BaseNamespace, BaseOperation

if TYPE_CHECKING:
    from ..main import HHApplicantTool


logger = logging.getLogger(__package__)


_PERSONA_PROMPT = (
    "Проанализируй каталог {source} (аналитические отчёты маркетолога "
    "автобизнеса ОКАМИ — Geely/Belgee Екатеринбург). Сгенерируй "
    "professional profile в markdown с разделами:\n"
    "# Role\n"
    "# Skills (с категориями — например Маркетинг / Аналитика / Продукт)\n"
    "# Achievements (с конкретными цифрами и результатами из отчётов)\n"
    "# Tone & voice\n"
    "# Domain context\n\n"
    "Требования:\n"
    "- Используй инструменты Glob/Read/Grep для чтения файлов каталога.\n"
    "- Не выдумывай цифры — бери реальные из отчётов.\n"
    "- Размер 3-5 KB.\n"
    "- Язык русский, деловой тон.\n"
    "- Верни ТОЛЬКО markdown, без ```-обёрток, без пояснений до или после."
)


class Namespace(BaseNamespace):
    source: Path
    output: Path | None
    model: str | None
    dry_run: bool


class Operation(BaseOperation):
    """Сгенерировать persona.md из okami-reports."""

    __aliases__ = ("generate-persona", "persona-gen")

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--source",
            type=Path,
            default=Path("/app/okami-reports"),
            help="Путь до каталога с отчётами (должен быть смонтирован в контейнер — см. docker-compose volume).",
        )
        parser.add_argument(
            "--output",
            type=Path,
            default=None,
            help="Куда записать persona.md. По умолчанию — <config_dir>/persona.md текущего профиля.",
        )
        parser.add_argument(
            "--model",
            type=str,
            default=None,
            help="Переопределить модель Claude (иначе из config.claude.model).",
        )
        parser.add_argument(
            "--dry-run",
            "--dry",
            default=False,
            action=argparse.BooleanOptionalAction,
            help="Не вызывать AI и не писать файл — только показать что было бы сделано.",
        )

    def run(
        self,
        tool: HHApplicantTool,
        args: Namespace,
    ) -> None:
        if not args.source.exists():
            logger.error(
                "okami-reports не найден по пути %s; "
                "смонтируй volume или укажи --source",
                args.source,
            )
            print(
                "❌ Источник не найден:",
                args.source,
            )
            print(
                "Подсказка: в docker-compose.yml раскомментируй volume "
                "../okami-reports:/app/okami-reports:ro или передай --source <path>."
            )
            return

        output_path: Path = (
            args.output
            if args.output is not None
            else tool.config_path / "persona.md"
        )

        if output_path.exists() and not args.dry_run:
            print(f"⚠️ Файл уже существует: {output_path}")
            try:
                answer = input("Перезаписать? [y/N]: ").strip().lower()
            except EOFError:
                answer = ""
            if answer not in ("y", "yes", "да"):
                print("Отмена.")
                return

        claude_cfg = tool.config.get("claude", {}) or {}
        model = args.model or claude_cfg.get("model")

        if args.dry_run:
            print(
                f"[dry-run] source={args.source} output={output_path} "
                f"model={model or '<default>'}"
            )
            print(
                "[dry-run] Промпт был бы отправлен в claude -p с "
                "allowed_tools=[Glob,Read,Grep], timeout=600s."
            )
            return

        ai = ChatClaude(
            model=model,
            timeout=float(claude_cfg.get("persona_timeout", 600.0)),
            rate_limit=int(claude_cfg.get("rate_limit", 10)),
            allowed_tools=["Glob", "Read", "Grep"],
        )

        prompt = _PERSONA_PROMPT.format(source=args.source)
        logger.info(
            "generate-persona: source=%s output=%s model=%s",
            args.source,
            output_path,
            model,
        )
        print(
            "🧠 Генерирую persona.md из", args.source,
            "— это может занять 1-3 минуты."
        )
        try:
            content = ai.complete(prompt)
        except AIError as ex:
            logger.error("generate-persona: claude упал: %s", ex)
            print("❌ Claude упал:", ex, file=sys.stderr)
            return

        content = content.strip()
        if content.startswith("```"):
            # страховка: срезать markdown-fence, если модель всё-таки обернула
            lines = content.split("\n")
            lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            content = "\n".join(lines).strip()

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content + "\n", encoding="utf-8")

        size_kb = output_path.stat().st_size / 1024
        print(
            f"✅ persona.md записан: {output_path} ({size_kb:.1f} KB)"
        )
