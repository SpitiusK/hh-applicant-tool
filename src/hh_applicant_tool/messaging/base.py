"""Абстракция мессенджера. Используется approval-loop'ом и dispatcher'ом.

Цель — отвязать код apply-vacancies / reply-employers от конкретного
бэкенда (Telegram / Max / Email / ...). Реализация Telegram —
в `telegram_client.py`, импортируется только фабрикой.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ApprovalRequest:
    """Данные запроса на подтверждение действия.

    Отправляется в мессенджер вместе с inline-кнопками actions.
    draft_id — id в таблице pending_messages (источник истины).
    """

    draft_id: int
    text: str
    actions: list[str]


@dataclass
class IncomingCommand:
    """Команда от пользователя из мессенджера.

    Нормализованный вид для всех бэкендов. Dispatcher в mainstream-режиме
    обрабатывает события через handlers (callback_query), поэтому этот
    тип используется только поллинг-режимом sync-сервисов, которым
    нужно разобрать пачку команд батчем (fallback-путь).
    """

    user_id: int
    command: str
    draft_id: int | None = None
    payload: str | None = None


class MessengerClient(ABC):
    """Контракт клиента мессенджера.

    Sync-методы send_* вызываются из обычных cron-операций
    (apply-vacancies, reply-employers). Реализация обязана обеспечить,
    что внутри не создаётся новый event loop на каждый вызов — это
    путь в ад при 50+ действиях за прогон.

    `run_polling` — async-точка входа для long-running сервиса
    (см. П.11 — `operations/run_messenger_bot.py`). Запускается через
    asyncio.run() в процессе бота и не пересекается с sync-path.
    """

    @abstractmethod
    def send_notification(self, text: str) -> None:
        """Простое сообщение без интерактива."""

    @abstractmethod
    def send_approval_request(
        self,
        draft_id: int,
        text: str,
        actions: list[str],
    ) -> str:
        """Сообщение с inline-кнопками. Возвращает external_message_id
        (для сохранения в pending_messages.messenger_message_id).
        """

    @abstractmethod
    def poll_commands(self) -> list[IncomingCommand]:
        """Fallback-опрос для бэкендов без push-хендлеров. Для Telegram
        возвращает пустой список — там events идут через Dispatcher
        (run_polling).
        """

    @abstractmethod
    def acknowledge_command(
        self,
        cmd: IncomingCommand,
        result: str,
    ) -> None:
        """Подтвердить приём команды (для Telegram — answer_callback_query)."""

    @abstractmethod
    async def run_polling(self) -> None:
        """Long-running async event loop бота. Запускается из
        `run-messenger-bot`. Все handlers читают/пишут state через
        pending_messages (SQLite — единственная граница sync↔async).
        """
