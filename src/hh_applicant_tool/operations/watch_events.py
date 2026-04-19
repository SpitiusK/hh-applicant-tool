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

from ..ai.claude import ChatClaude
from ..ai.schema import EventClassification
from ..approval import (
    escalate_to_user,
    generate_with_self_assessment,
    persist_ai_decision,
    should_escalate,
)
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
                self._stage_messages()
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

    # -------------------------------------------------- stage: messages (AI)

    _MESSAGES_PROMPT = (
        "Ты анализируешь сообщение работодателя в чате hh.ru и определяешь, "
        "является ли оно событием (interview — приглашение на интервью, "
        "offer — оффер, deadline — дедлайн по задаче/ответу).\n\n"
        "Вакансия: {vacancy_name}\n"
        "Работодатель: {employer_name}\n"
        "Сообщение: {msg_text}\n\n"
        "Если это НЕ событие — is_event=false и confidence=0.9.\n"
        "Если событие — is_event=true, event_type (interview|offer|deadline), "
        "when_iso (ISO8601 дата-время из сообщения, или null если не указано), "
        "title (краткое название события), notes (детали из текста). "
        "Если дата неоднозначна или формат встречи неясен — escalate=true, "
        "question_for_user задай конкретно."
    )

    def _get_messenger(self):
        """Ленивая инициализация MessengerClient (для эскалаций)."""
        mgr = getattr(self, "_messenger_cached", None)
        if mgr is not None:
            return mgr
        try:
            from ..messaging import get_messenger_client

            self._messenger_cached = get_messenger_client(
                self.tool.config, self.tool.storage
            )
        except Exception as ex:
            logger.warning(
                "messenger не инициализирован (%s); эскалации без нотификации",
                ex,
            )
            self._messenger_cached = None
        return self._messenger_cached

    def _get_event_classifier(self) -> ChatClaude:
        claude_cfg = self.tool.config.get("claude", {}) or {}
        return ChatClaude(
            model=claude_cfg.get("model"),
            timeout=float(claude_cfg.get("timeout", 120.0)),
            rate_limit=int(claude_cfg.get("rate_limit", 10)),
        )

    def _stage_messages(self) -> None:
        events_repo = getattr(self.tool.storage, "events", None)
        settings_repo = self.tool.storage.settings
        approval_cfg = self.tool.get_approval_defaults()
        threshold = float(approval_cfg.get("confidence_threshold", 0.7))
        ai = self._get_event_classifier()
        detected = 0
        escalated = 0
        scanned = 0

        for raw in self.tool.get_negotiations():
            neg_id = raw.get("id")
            if not neg_id:
                continue
            vacancy = raw.get("vacancy") or {}
            employer = vacancy.get("employer") or {}
            vacancy_name = vacancy.get("name") or f"vacancy#{vacancy.get('id')}"
            employer_name = employer.get("name") or "—"

            last_seen_key = f"watch_events_last_msg:{neg_id}"
            last_seen_raw = settings_repo.get_value(last_seen_key) or ""

            try:
                messages_page = self.tool.api_client.get(
                    f"/negotiations/{neg_id}/messages", page=0
                )
            except Exception:
                logger.exception(
                    "neg#%s: не прочитать messages", neg_id
                )
                continue

            items = (messages_page or {}).get("items") or []
            max_seen = last_seen_raw
            for msg in items:
                author = msg.get("author") or {}
                if author.get("participant_type") != "employer":
                    continue
                created_at = msg.get("created_at") or ""
                if last_seen_raw and created_at <= last_seen_raw:
                    continue
                if created_at > max_seen:
                    max_seen = created_at
                scanned += 1

                msg_text = (msg.get("text") or "").strip()
                if not msg_text:
                    continue

                prompt = self._MESSAGES_PROMPT.format(
                    vacancy_name=vacancy_name,
                    employer_name=employer_name,
                    msg_text=msg_text[:2000],
                )
                try:
                    # complete_json возвращает EventClassification (наследник
                    # AIResponse). sentinel-fallback работает через defaults.
                    resp = ai.complete_json(
                        prompt, response_model=EventClassification
                    )
                    if not isinstance(resp, EventClassification):
                        resp = generate_with_self_assessment(ai, prompt)
                        # generate_with_self_assessment вернёт AIResponse —
                        # маппим в EventClassification с is_event=False,
                        # если истинное complete_json-path провалился.
                        resp = EventClassification(
                            answer=resp.answer,
                            confidence=resp.confidence,
                            escalate=resp.escalate,
                            escalation_reason=resp.escalation_reason,
                            is_sentinel=resp.is_sentinel,
                        )
                except Exception:
                    logger.exception(
                        "neg#%s msg: AI classification упала", neg_id
                    )
                    continue

                persist_ai_decision(
                    self.tool.storage,
                    operation="event_detect",
                    ai_response=resp,
                    status="auto_dispatched"
                    if not should_escalate(
                        resp, "event_detect", {**approval_cfg, "mode": "on_escalation"}
                    )
                    else "escalated",
                    negotiation_id=int(neg_id) if str(neg_id).isdigit() else None,
                    vacancy_id=vacancy.get("id"),
                    result_preview=(resp.title or msg_text)[:200],
                    messenger=self._get_messenger(),
                    approval_cfg=approval_cfg,
                )

                if not resp.is_event:
                    continue

                # is_event=True: либо эскалируем, либо авто-создаём event.
                if resp.escalate or resp.confidence < threshold or resp.is_sentinel:
                    escalated += 1
                    if self.dry_run:
                        print(
                            f"[dry-run] escalate event neg#{neg_id} "
                            f"{resp.event_type}: {resp.title}"
                        )
                        continue
                    escalate_to_user(
                        self.tool.storage,
                        self._get_messenger(),
                        action_type="event_detect",
                        draft_payload={
                            "neg_id": neg_id,
                            "event_type": resp.event_type,
                            "when_iso": resp.when_iso,
                            "title": resp.title,
                            "notes": resp.notes,
                            "source_msg_id": msg.get("id"),
                            "msg_text": msg_text[:1000],
                        },
                        ai_response=resp,
                        approval_cfg={
                            **approval_cfg,
                            "mode": "on_escalation",
                        },
                    )
                else:
                    detected += 1
                    if self.dry_run:
                        print(
                            f"[dry-run] event neg#{neg_id} "
                            f"{resp.event_type}: {resp.title} @ {resp.when_iso}"
                        )
                        continue
                    if events_repo is None:
                        logger.warning(
                            "events-repo not ready; skip neg#%s", neg_id
                        )
                        continue
                    when_ts = None
                    if resp.when_iso:
                        try:
                            when_ts = parse_api_datetime(resp.when_iso)
                        except Exception:
                            when_ts = None
                    try:
                        events_repo.create(
                            EventModel(
                                negotiation_id=int(neg_id)
                                if str(neg_id).isdigit()
                                else None,
                                vacancy_id=vacancy.get("id"),
                                type=resp.event_type or "interview",
                                title=resp.title or vacancy_name,
                                when_ts=when_ts,
                                source_msg_id=str(msg.get("id") or ""),
                                raw_text=msg_text[:2000],
                                confidence=resp.confidence,
                                status="detected",
                            )
                        )
                    except Exception:
                        logger.exception(
                            "events.create упал для neg#%s", neg_id
                        )

            if not self.dry_run and max_seen and max_seen != last_seen_raw:
                try:
                    settings_repo.set_value(last_seen_key, max_seen)
                except Exception:
                    logger.exception(
                        "не обновить last_seen для neg#%s", neg_id
                    )

        print(
            f"messages: scanned={scanned}, detected={detected}, "
            f"escalated={escalated}, dry_run={self.dry_run}"
        )
