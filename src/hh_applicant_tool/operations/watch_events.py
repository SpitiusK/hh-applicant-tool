"""Event detector (П.22a/b/c).

Stage "state" (П.22a): diff negotiations.state — пишем event при
любом переходе состояния (response → invitation → interview → hired
и т.п.). AI не задействован.

Stages "messages" (22b) и "tasks" (22c) — заглушки, реализуются
следующими пунктами.
"""

from __future__ import annotations

import argparse
import logging
from typing import TYPE_CHECKING, Any, Literal

from ..main import BaseNamespace, BaseOperation
from ..storage.models.event import EventModel
from ..storage.models.negotiation import NegotiationModel
from ..utils.date import parse_api_datetime

if TYPE_CHECKING:
    from ..main import HHApplicantTool


logger = logging.getLogger(__package__)


class Namespace(BaseNamespace):
    stage: Literal["state", "messages", "tasks", "all"]
    dry_run: bool


class Operation(BaseOperation):
    """Детектор событий из negotiations (state diff + AI-анализ сообщений)."""

    __aliases__ = ("watch-events", "events-watch")

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--stage",
            choices=["state", "messages", "tasks", "all"],
            default="state",
            help=(
                "Какие стадии запустить: state — SQL-diff (П.22a, дефолт), "
                "messages — AI-классификация новых сообщений (П.22b), "
                "tasks — ТЗ/дедлайны (П.22c), all — все по порядку."
            ),
        )
        parser.add_argument(
            "--dry-run",
            "--dry",
            default=False,
            action=argparse.BooleanOptionalAction,
            help="Только показать, что было бы записано — без UPDATE и без INSERT.",
        )

    def run(
        self,
        tool: HHApplicantTool,
        args: Namespace,
    ) -> None:
        self.tool = tool
        self.dry_run = bool(args.dry_run)
        stages_to_run = (
            ["state", "messages", "tasks"]
            if args.stage == "all"
            else [args.stage]
        )

        for stage in stages_to_run:
            if stage == "state":
                self._stage_state()
            elif stage == "messages":
                logger.info(
                    "stage=messages: TODO(П.22b) — AI-классификация сообщений"
                )
                print("messages: TODO (П.22b)")
            elif stage == "tasks":
                logger.info(
                    "stage=tasks: TODO(П.22c) — детект ТЗ/дедлайнов"
                )
                print("tasks: TODO (П.22c)")

    # ----------------------------------------------------- stage: state diff

    def _stage_state(self) -> None:
        events_repo = getattr(self.tool.storage, "events", None)
        negotiations_repo = self.tool.storage.negotiations

        detected = 0
        new_records = 0
        for raw in self.tool.get_negotiations():
            try:
                neg_model = NegotiationModel.from_api(raw)
            except Exception:
                logger.exception(
                    "watch-events: не распарсить negotiation, пропускаем"
                )
                continue

            prev = negotiations_repo.get(neg_model.id)
            prev_state = getattr(prev, "state", None) if prev else None
            new_state = neg_model.state

            if prev is None:
                # Новая запись — просто сохраняем, событий не генерим
                # (кроме случая, когда state сразу необычное — например,
                # cron пропустил много шагов и видим "invitation" для неизвестного
                # negotiation'а).
                if not self.dry_run:
                    try:
                        negotiations_repo.save(neg_model)
                    except Exception:
                        logger.exception(
                            "neg#%s: не удалось сохранить", neg_model.id
                        )
                new_records += 1

                if new_state and new_state != "response":
                    self._emit_state_event(
                        events_repo,
                        neg_model,
                        raw,
                        prev_state=None,
                        new_state=new_state,
                    )
                    detected += 1
                continue

            if new_state and new_state != prev_state:
                self._emit_state_event(
                    events_repo,
                    neg_model,
                    raw,
                    prev_state=prev_state,
                    new_state=new_state,
                )
                detected += 1

                if not self.dry_run:
                    try:
                        negotiations_repo.save(neg_model)
                    except Exception:
                        logger.exception(
                            "neg#%s: не обновить запись", neg_model.id
                        )

        print(
            f"state diff: detected={detected}, new_records={new_records}, "
            f"dry_run={self.dry_run}"
        )

    def _emit_state_event(
        self,
        events_repo: Any,
        neg: NegotiationModel,
        raw: dict[str, Any],
        *,
        prev_state: str | None,
        new_state: str,
    ) -> None:
        vacancy = raw.get("vacancy") or {}
        vacancy_name = vacancy.get("name") or f"vacancy#{neg.vacancy_id}"
        title = f"{new_state}: {vacancy_name}"
        updated_at_raw = raw.get("updated_at")
        when_ts = None
        if updated_at_raw:
            try:
                when_ts = parse_api_datetime(updated_at_raw)
            except Exception:
                when_ts = None

        logger.info(
            "event: neg#%s %s → %s (%s)",
            neg.id,
            prev_state,
            new_state,
            vacancy_name,
        )
        if self.dry_run:
            print(
                f"[dry-run] event neg#{neg.id} {prev_state} → {new_state}: {vacancy_name}"
            )
            return

        if events_repo is None:
            logger.warning(
                "events-repo not ready (П.23 pending); пропускаем запись для neg#%s",
                neg.id,
            )
            return

        try:
            events_repo.create(
                EventModel(
                    negotiation_id=neg.id,
                    vacancy_id=neg.vacancy_id,
                    type="negotiation_state_changed",
                    title=title,
                    when_ts=when_ts,
                    status="detected",
                    raw_text=(
                        f"{prev_state or '∅'} → {new_state}"
                    ),
                )
            )
        except Exception:
            logger.exception(
                "events.create упал для neg#%s", neg.id
            )
