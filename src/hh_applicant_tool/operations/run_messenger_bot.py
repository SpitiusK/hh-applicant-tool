"""Long-running messenger-бот (aiogram-polling).

Отдельная операция для запуска в выделенном контейнере. Общается с
cron-сервисами (apply-vacancies, reply-employers) только через SQLite
(pending_messages / ai_decisions). Все UI-handlers живут внутри
TelegramClient.run_polling (см. messaging/telegram_client.py).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import TYPE_CHECKING

from ..main import BaseNamespace, BaseOperation
from ..messaging import get_messenger_client

if TYPE_CHECKING:
    from ..main import HHApplicantTool


logger = logging.getLogger(__package__)


__aliases__ = ("messenger-bot", "bot")


class Namespace(BaseNamespace):
    pass


class Operation(BaseOperation):
    """Запустить messenger-бота (long-running polling)."""

    __aliases__ = ("messenger-bot", "bot")

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        # Флагов нет — вся конфигурация идёт через config.messaging.
        pass

    def run(
        self,
        tool: HHApplicantTool,
        args: Namespace,
    ) -> None:
        client = get_messenger_client(tool.config, tool.storage)
        logger.info(
            "messenger-bot: запуск long-running polling (%s)",
            type(client).__name__,
        )
        try:
            asyncio.run(client.run_polling())
        except KeyboardInterrupt:
            logger.info("messenger-bot: остановлен пользователем")
