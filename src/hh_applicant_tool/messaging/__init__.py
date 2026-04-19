"""Messaging abstraction: approval / notification через абстрактный
MessengerClient. Конкретные бэкенды (TelegramClient) изолированы —
только фабрика импортирует их лениво, чтобы базовый импорт не тянул
aiogram, если он не установлен.
"""

from .base import (
    ApprovalRequest,
    IncomingCommand,
    MessengerClient,
)
from .factory import get_messenger_client

__all__ = [
    "ApprovalRequest",
    "IncomingCommand",
    "MessengerClient",
    "get_messenger_client",
]
