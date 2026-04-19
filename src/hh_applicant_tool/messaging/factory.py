"""Фабрика мессенджер-клиента.

Импорт TelegramClient ленивый — aiogram не обязан быть установлен
в окружениях, которые не используют Telegram-бэкенд (например, dev
без задач Блока 2).
"""

from __future__ import annotations

from typing import Any

from .base import MessengerClient


def get_messenger_client(
    config: dict[str, Any],
    storage_facade: Any,
) -> MessengerClient:
    """Создать клиент по секции `config["messaging"]`.

    Ожидается структура:
        {"messaging": {"backend": "telegram", "telegram": {
            "bot_token": ..., "chat_id": ..., "allowed_user_id": ...}}}
    """
    messaging_cfg = config.get("messaging", {}) or {}
    backend = messaging_cfg.get("backend", "telegram")

    if backend == "telegram":
        from .telegram_client import TelegramClient

        tg = messaging_cfg.get("telegram", {}) or {}
        bot_token = tg.get("bot_token")
        if not bot_token:
            raise ValueError(
                "config.messaging.telegram.bot_token не задан"
            )
        return TelegramClient(
            bot_token=bot_token,
            chat_id=tg.get("chat_id"),
            allowed_user_id=tg.get("allowed_user_id"),
            storage_facade=storage_facade,
        )

    raise ValueError(f"unsupported messenger backend: {backend}")
