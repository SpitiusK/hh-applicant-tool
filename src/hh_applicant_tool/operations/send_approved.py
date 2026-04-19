"""Dispatcher для approved pending_messages (П.10).

Cron-операция. Забирает записи со status='approved' и применяет
реальное действие на hh.ru (отклик / сообщение работодателю).
Ошибки пишет в status='error' + уведомляет мессенджер.

form_field-действия в этом пункте НЕ диспатчатся (П.21 их переведёт
на pending_messages через messaging; сейчас они всё ещё в
review_queue.jsonl и живут своим пайплайном).
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..ai.schema import AIResponse  # noqa: F401 — контракт confidence/escalate
from ..api import ApiError
from ..main import BaseNamespace, BaseOperation
from ..messaging import MessengerClient, get_messenger_client
from ..storage.models.ai_decision import AiDecisionModel

if TYPE_CHECKING:
    from ..main import HHApplicantTool
    from ..storage.models.pending_message import PendingMessageModel


logger = logging.getLogger(__package__)


class Namespace(BaseNamespace):
    dry_run: bool


class Operation(BaseOperation):
    """Разослать ранее одобренные pending_messages в hh.ru."""

    __aliases__ = ("send-approved", "dispatch")

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--dry-run",
            "--dry",
            help="Не отправлять HTTP-запросы и не обновлять статусы, только показать что было бы сделано.",
            default=False,
            action=argparse.BooleanOptionalAction,
        )

    def run(
        self,
        tool: HHApplicantTool,
        args: Namespace,
    ) -> None:
        self.tool = tool
        self.dry_run = bool(args.dry_run)
        self.api_client = tool.api_client
        self.repo = tool.storage.pending_messages
        self.decisions = tool.storage.ai_decisions

        # Messenger нужен только для уведомления об ошибках — инициализируем
        # лениво, чтобы отсутствие секции messaging в конфиге не мешало
        # успешному dry-run на пустой таблице.
        self._messenger: MessengerClient | None = None

        approved = list(self.repo.get_by_status("approved"))
        if not approved:
            logger.info("send-approved: 0 approved messages")
            print("Нет одобренных сообщений для отправки.")
            return

        logger.info("send-approved: %d approved messages", len(approved))
        ok = 0
        errs = 0
        skipped = 0
        for pm in approved:
            result = self._dispatch_one(pm)
            if result == "ok":
                ok += 1
            elif result == "skipped":
                skipped += 1
            else:
                errs += 1

        print(
            f"Готово. dispatched: {ok}, errors: {errs}, skipped: {skipped}"
        )

    # ------------------------------------------------------------ helpers

    def _get_messenger(self) -> MessengerClient | None:
        if self._messenger is not None:
            return self._messenger
        try:
            self._messenger = get_messenger_client(
                self.tool.config, self.tool.storage
            )
        except Exception as ex:
            logger.warning(
                "messenger не инициализирован (%s); error-нотификации в лог",
                ex,
            )
            self._messenger = None
        return self._messenger

    def _dispatch_one(self, pm: PendingMessageModel) -> str:
        action_type = pm.action_type
        payload: dict[str, Any] = pm.draft_payload or {}

        if action_type == "form_field":
            logger.warning(
                "pm id=%s form_field: не реализовано (П.21); помечаем error",
                pm.id,
            )
            self._mark_error(
                pm,
                "form_field not yet wired (pending П.21)",
            )
            return "skipped"

        if action_type == "apply_vacancy":
            endpoint = "/negotiations"
        elif action_type == "reply_employer":
            endpoint = "/messages"
        else:
            logger.error(
                "pm id=%s неизвестный action_type=%r, помечаем error",
                pm.id,
                action_type,
            )
            self._mark_error(pm, f"unknown action_type: {action_type}")
            return "error"

        if self.dry_run:
            logger.info(
                "[dry-run] pm id=%s action=%s endpoint=%s payload_keys=%s",
                pm.id,
                action_type,
                endpoint,
                list(payload.keys()),
            )
            print(
                f"[dry-run] pm#{pm.id} {action_type} → POST {endpoint}"
            )
            return "ok"

        try:
            self.api_client.post(endpoint, payload)
        except ApiError as ex:
            self._mark_error(pm, f"dispatch_failed: {ex}")
            logger.error(
                "pm id=%s dispatch_failed: %s", pm.id, ex
            )
            return "error"
        except Exception as ex:
            # Неожиданное (network / parsing) — тоже в error, но логируем жёстче.
            self._mark_error(pm, f"dispatch_failed: {ex!r}")
            logger.exception(
                "pm id=%s неожиданная ошибка dispatch", pm.id
            )
            return "error"

        now = datetime.now()
        self.repo.update_status(
            pm.id, "dispatched", dispatched_at=now
        )
        self._log_decision(pm, status="approved")
        logger.info("pm id=%s dispatched (%s)", pm.id, action_type)
        print(f"✅ pm#{pm.id} {action_type} отправлено")
        return "ok"

    def _mark_error(
        self, pm: PendingMessageModel, reason: str
    ) -> None:
        if self.dry_run:
            logger.info(
                "[dry-run] pm id=%s error: %s", pm.id, reason
            )
            return
        try:
            self.repo.update(
                pm.id,
                status="error",
                escalation_reason=reason,
            )
        except Exception:
            logger.exception(
                "pm id=%s не удалось записать error-статус", pm.id
            )

        messenger = self._get_messenger()
        if messenger is not None:
            try:
                messenger.send_notification(
                    f"❌ Dispatch failed (pm#{pm.id}): {reason}"
                )
            except Exception:
                logger.exception(
                    "pm id=%s не удалось отправить error-нотификацию",
                    pm.id,
                )

    def _log_decision(
        self, pm: PendingMessageModel, status: str
    ) -> None:
        try:
            self.decisions.create(
                AiDecisionModel(
                    operation=pm.action_type,
                    confidence=pm.confidence,
                    escalated=True,
                    escalation_reason=pm.escalation_reason,
                    iterations=pm.iterations or 0,
                    status=status,
                    result_preview=str(pm.draft_payload)[:200]
                    if pm.draft_payload
                    else None,
                )
            )
        except Exception:
            logger.exception(
                "pm id=%s не удалось записать ai_decision", pm.id
            )
