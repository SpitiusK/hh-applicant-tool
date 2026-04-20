"""Telegram-реализация MessengerClient поверх aiogram 3.x.

Архитектура event-loop'ов:
- Sync-сервисы (apply-vacancies, reply-employers в обычных cron-вызовах)
  используют send_notification / send_approval_request. Эти методы НЕ
  могут вызывать asyncio.run() на каждый вызов (50 откликов → 50 loop'ов).
  Решение: один module-level long-lived loop, который крутится в
  отдельном daemon-thread. Sync-вызовы шлют корутину в него через
  asyncio.run_coroutine_threadsafe + .result().
- Long-running bot-сервис (П.11 — run_messenger_bot operation) запускает
  `asyncio.run(client.run_polling())` в главной корутине своего процесса.
  Этот loop живёт отдельно от sync-loop'а; они не пересекаются и общаются
  только через SQLite (pending_messages).

aiogram импортируется лениво внутри TelegramClient — базовый импорт
пакета messaging не должен тянуть aiogram для окружений без Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from .base import IncomingCommand, MessengerClient

logger = logging.getLogger(__name__)


class TelegramClient(MessengerClient):
    def __init__(
        self,
        bot_token: str,
        chat_id: int | str | None,
        allowed_user_id: int | None,
        storage_facade: Any,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._allowed_user_id = allowed_user_id
        self._storage = storage_facade
        self._config: dict[str, Any] = config or {}

        # Long-lived sync-loop в отдельном thread (создаётся лениво).
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._bot: Any | None = None  # aiogram.Bot
        self._lock = threading.Lock()

    # ------------------------------------------------------------ sync path

    def _ensure_loop(self) -> tuple[asyncio.AbstractEventLoop, Any]:
        """Лениво создаёт фоновой loop + aiogram.Bot для sync-вызовов."""
        if self._loop is not None and self._bot is not None:
            return self._loop, self._bot

        with self._lock:
            if self._loop is not None and self._bot is not None:
                return self._loop, self._bot

            from aiogram import Bot

            loop = asyncio.new_event_loop()

            def _run_forever() -> None:
                asyncio.set_event_loop(loop)
                loop.run_forever()

            thread = threading.Thread(
                target=_run_forever,
                name="tg-sync-loop",
                daemon=True,
            )
            thread.start()

            self._loop = loop
            self._loop_thread = thread
            self._bot = Bot(token=self._bot_token)
            return self._loop, self._bot

    def _run_coro_sync(self, coro) -> Any:
        loop, _ = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()

    # ------------------------------------------------------------- MessengerClient API

    def send_notification(self, text: str) -> None:
        if self._chat_id is None:
            logger.warning("TelegramClient.send_notification: chat_id не задан, пропускаем")
            return
        _, bot = self._ensure_loop()
        self._run_coro_sync(
            bot.send_message(self._chat_id, text, parse_mode="HTML")
        )

    def send_approval_request(
        self,
        draft_id: int,
        text: str,
        actions: list[str],
    ) -> str:
        if self._chat_id is None:
            raise RuntimeError(
                "TelegramClient.send_approval_request: chat_id обязателен"
            )
        from aiogram.types import (
            InlineKeyboardButton,
            InlineKeyboardMarkup,
        )

        button_labels = {
            "approve": "✅ Approve",
            "modify": "✏️ Modify",
            "reject": "❌ Reject",
        }
        row = [
            InlineKeyboardButton(
                text=button_labels.get(a, a.capitalize()),
                callback_data=f"{a}:{draft_id}",
            )
            for a in actions
        ]
        keyboard = InlineKeyboardMarkup(inline_keyboard=[row])

        _, bot = self._ensure_loop()
        msg = self._run_coro_sync(
            bot.send_message(
                self._chat_id,
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        )
        return str(msg.message_id)

    def poll_commands(self) -> list[IncomingCommand]:
        # Telegram работает через Dispatcher (run_polling), не через poll-pull.
        # Метод оставлен ради совместимости с ABC — другие бэкенды
        # (email-polling, http-webhook-batch) могут его заполнить.
        return []

    def acknowledge_command(
        self,
        cmd: IncomingCommand,
        result: str,
    ) -> None:
        # Хэндлеры run_polling отвечают на CallbackQuery напрямую в месте
        # обработки. Этот метод зарезервирован для внешнего ack'ующего
        # кода (если когда-нибудь IncomingCommand будут выплёвываться
        # наружу). Пока — просто лог.
        logger.debug(
            "acknowledge_command cmd=%s result=%s (noop for Telegram)",
            cmd,
            result,
        )

    # -------------------------------------------------------- async long-running path

    async def run_polling(self) -> None:
        """Запуск long-running aiogram Dispatcher (П.11).

        Handlers читают/пишут pending_messages через self._storage.
        Ожидается, что storage_facade имеет репозиторий pending_messages
        (миграция в П.7 — выполняется storage-teammate параллельно).
        Если репозиторий ещё не готов — хендлеры деградируют в WARNING,
        но процесс не падает.
        """
        from aiogram import Bot, Dispatcher, F, Router
        from aiogram.filters import Command
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.state import State, StatesGroup
        from aiogram.fsm.storage.memory import MemoryStorage
        from aiogram.types import CallbackQuery, Message

        from .modify_handler import handle_modify

        class ModifyFSM(StatesGroup):
            awaiting_comment = State()

        bot = Bot(token=self._bot_token)
        dp = Dispatcher(storage=MemoryStorage())
        router = Router()

        allowed_user_id = self._allowed_user_id
        storage = self._storage
        config = self._config

        if allowed_user_id is None:
            logger.warning(
                "messaging.telegram.allowed_user_id не задан — бот будет "
                "отвергать ВСЕХ пользователей. Задай свой numeric user_id "
                "в config, чтобы разрешить себе доступ."
            )

        def _is_allowed(user_id: int | None) -> bool:
            # Fail-closed: если allowed_user_id не задан — пускаем никого.
            # Раньше было fail-open (любой мог использовать бота) — дыра.
            if allowed_user_id is None:
                return False
            return user_id == allowed_user_id

        def _pending_repo():
            """Получить репозиторий pending_messages с защитой от недоступности (П.7)."""
            return getattr(storage, "pending_messages", None)

        # ---- command handlers (заглушки с корректным UX) ---------------

        @router.message(Command("start"))
        async def _on_start(message: Message) -> None:
            if not _is_allowed(message.from_user.id if message.from_user else None):
                return
            await message.answer(
                "hh-applicant-tool bot on-line.\n"
                "Доступные команды: /stats /pending /events /skipped /sanity /flag"
            )

        @router.message(Command("stats"))
        async def _on_stats(message: Message) -> None:
            if not _is_allowed(message.from_user.id if message.from_user else None):
                return
            repo = _pending_repo()
            if repo is None:
                await message.answer("pending_messages не инициализирован (П.7 pending)")
                return
            try:
                pending = sum(1 for _ in repo.get_by_status("pending"))
                dispatched = sum(1 for _ in repo.get_by_status("dispatched"))
                await message.answer(
                    f"pending: {pending}\ndispatched: {dispatched}"
                )
            except AttributeError:
                await message.answer(
                    "repository API недоступно (ждём П.7)"
                )

        @router.message(Command("pending"))
        async def _on_pending(message: Message) -> None:
            if not _is_allowed(message.from_user.id if message.from_user else None):
                return
            await message.answer("TODO(П.14): listing pending_messages")

        @router.message(Command("events"))
        async def _on_events(message: Message) -> None:
            if not _is_allowed(message.from_user.id if message.from_user else None):
                return
            await message.answer("TODO(Блок 3 П.22): listing events")

        @router.message(Command("skipped"))
        async def _on_skipped(message: Message) -> None:
            if not _is_allowed(message.from_user.id if message.from_user else None):
                return
            await message.answer("TODO: skipped_vacancies summary")

        @router.message(Command("sanity"))
        async def _on_sanity(message: Message) -> None:
            if not _is_allowed(
                message.from_user.id if message.from_user else None
            ):
                return
            decisions_repo = getattr(storage, "ai_decisions", None)
            if decisions_repo is None:
                await message.answer("ai_decisions недоступно")
                return
            parts = (message.text or "").split()
            limit = 10
            if len(parts) >= 2 and parts[1].isdigit():
                limit = min(int(parts[1]), 50)
            try:
                rows = list(
                    decisions_repo.list_samples_for_review(limit=limit)
                )
            except Exception as ex:
                await message.answer(f"error: {ex}")
                return
            if not rows:
                await message.answer("Нет sanity-сэмплов.")
                return
            lines = [f"🔍 Последние {len(rows)} sanity-сэмплов:"]
            for r in rows:
                conf = (
                    f"{r.confidence:.2f}"
                    if r.confidence is not None
                    else "—"
                )
                lines.append(
                    f"#{r.id} {r.operation} conf={conf} "
                    f"flagged={'✅' if r.flagged else '—'}"
                )
            await message.answer("\n".join(lines))

        @router.message(Command("flag"))
        async def _on_flag(message: Message) -> None:
            if not _is_allowed(
                message.from_user.id if message.from_user else None
            ):
                return
            decisions_repo = getattr(storage, "ai_decisions", None)
            if decisions_repo is None:
                await message.answer("ai_decisions недоступно")
                return
            parts = (message.text or "").split(maxsplit=2)
            if len(parts) < 2 or not parts[1].isdigit():
                await message.answer(
                    "Использование: /flag <decision_id> [reason]"
                )
                return
            decision_id = int(parts[1])
            reason = parts[2] if len(parts) > 2 else "user_flagged"
            try:
                decisions_repo.mark_flagged(decision_id, reason)
            except Exception as ex:
                await message.answer(f"error: {ex}")
                return
            await message.answer(
                f"🚩 Decision #{decision_id} отмечен: {reason}"
            )

        # ---- callback-query approve/modify/reject ----------------------

        @router.callback_query(F.data.startswith("approve:"))
        async def _on_approve(cq: CallbackQuery) -> None:
            if not _is_allowed(cq.from_user.id if cq.from_user else None):
                await cq.answer("not authorized", show_alert=False)
                return
            draft_id = int((cq.data or "approve:0").split(":", 1)[1])
            repo = _pending_repo()
            if repo is None:
                await cq.answer("pending_messages недоступно")
                return
            try:
                repo.update_status(draft_id, "approved")
            except Exception as ex:
                await cq.answer(f"error: {ex}", show_alert=True)
                return
            await cq.answer("Approved")
            if cq.message is not None:
                await cq.message.edit_reply_markup(reply_markup=None)

        @router.callback_query(F.data.startswith("reject:"))
        async def _on_reject(cq: CallbackQuery) -> None:
            if not _is_allowed(cq.from_user.id if cq.from_user else None):
                await cq.answer("not authorized", show_alert=False)
                return
            draft_id = int((cq.data or "reject:0").split(":", 1)[1])
            repo = _pending_repo()
            if repo is None:
                await cq.answer("pending_messages недоступно")
                return
            try:
                repo.update_status(draft_id, "rejected")
            except Exception as ex:
                await cq.answer(f"error: {ex}", show_alert=True)
                return
            await cq.answer("Rejected")
            if cq.message is not None:
                await cq.message.edit_reply_markup(reply_markup=None)

        @router.callback_query(F.data.startswith("modify:"))
        async def _on_modify(cq: CallbackQuery, state: FSMContext) -> None:
            if not _is_allowed(cq.from_user.id if cq.from_user else None):
                await cq.answer("not authorized", show_alert=False)
                return
            draft_id = int((cq.data or "modify:0").split(":", 1)[1])
            await state.set_state(ModifyFSM.awaiting_comment)
            await state.update_data(draft_id=draft_id)
            await cq.answer("Жду коррекцию")
            if cq.message is not None:
                await cq.message.answer(
                    f"✏️ Пришли коррекцию для pm#{draft_id} одним сообщением "
                    "(или /cancel чтобы отменить)."
                )

        @router.message(Command("cancel"), ModifyFSM.awaiting_comment)
        async def _on_cancel_modify(
            message: Message, state: FSMContext
        ) -> None:
            if not _is_allowed(
                message.from_user.id if message.from_user else None
            ):
                return
            await state.clear()
            await message.answer("Отмена Modify-ввода.")

        @router.message(ModifyFSM.awaiting_comment)
        async def _on_modify_comment(
            message: Message, state: FSMContext
        ) -> None:
            if not _is_allowed(
                message.from_user.id if message.from_user else None
            ):
                return
            data = await state.get_data()
            draft_id = data.get("draft_id")
            await state.clear()

            if not draft_id:
                await message.answer("не нашёл draft_id, попробуй снова")
                return

            user_comment = (message.text or "").strip()
            if not user_comment:
                await message.answer("пустой комментарий — пропускаю")
                return

            repo = _pending_repo()
            if repo is None:
                await message.answer(
                    "pending_messages недоступно"
                )
                return

            # Чтобы юзер видел что бот жив пока крутится claude -p
            # (10-60 сек), шлём send_chat_action='typing' каждые 4 сек —
            # Telegram держит индикатор «печатает» около 5 сек, поэтому
            # цикл с запасом.
            _stop_typing = asyncio.Event()

            async def _typing_loop() -> None:
                try:
                    chat_id = message.chat.id
                except Exception:
                    return
                while not _stop_typing.is_set():
                    try:
                        await bot.send_chat_action(chat_id, "typing")
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(
                            _stop_typing.wait(), timeout=4.0
                        )
                    except asyncio.TimeoutError:
                        continue

            typing_task = asyncio.create_task(_typing_loop())
            try:
                # handle_modify — sync, но мы внутри async-handler'а;
                # кладём в threadpool, чтобы не блокировать loop на
                # длинном claude -p вызове (10-60s).
                result = await asyncio.to_thread(
                    handle_modify,
                    storage,
                    int(draft_id),
                    user_comment,
                    config,
                    messenger=self,
                )
            except Exception as ex:
                logger.exception(
                    "handle_modify упал для pm#%s", draft_id
                )
                _stop_typing.set()
                await typing_task
                await message.answer(f"error: {ex}")
                return
            finally:
                _stop_typing.set()
            try:
                await typing_task
            except Exception:
                pass

            status = result.get("status")
            iteration = result.get("iteration")
            reason = result.get("reason")
            summary = (
                f"Modify pm#{draft_id}: status={status}, "
                f"iter={iteration}, reason={reason}"
            )
            if status == "approved":
                summary = (
                    f"✅ {summary}\n"
                    "Отправится через send-approved.\n\n"
                    "<b>Финальный текст:</b>"
                )
                await message.answer(summary, parse_mode="HTML")
                # Подтянуть свежий draft_payload и показать что улетит,
                # чтобы юзер увидел что именно auto-approved (не просто
                # "✅ approved" в вакууме).
                try:
                    pm = repo.get_by_id(int(draft_id))
                    payload = (pm.draft_payload or {}) if pm else {}
                    body = (payload.get("message") or payload.get("answer") or "").strip()
                    if body:
                        # Telegram limit ~4096; даём до 3500 в pre.
                        await message.answer(
                            f"<pre>{body[:3500]}</pre>",
                            parse_mode="HTML",
                        )
                    else:
                        await message.answer(
                            "<i>пусто (вакансия без сопроводительного — отправится клик «Откликнуться»)</i>",
                            parse_mode="HTML",
                        )
                except Exception as ex:
                    logger.exception(
                        "не удалось показать финальный текст pm#%s: %s",
                        draft_id,
                        ex,
                    )
            elif status == "rejected":
                await message.answer(f"❌ {summary}")
            elif status == "re_escalated":
                # При re-escalation приходит новый approval-card отдельным
                # сообщением (от escalate_to_user), здесь только короткий
                # summary чтобы юзер видел итерацию и причину.
                await message.answer(
                    f"♻️ {summary}\nНовый запрос с иттерацией выше."
                )
            else:
                await message.answer(summary)

        dp.include_router(router)
        await dp.start_polling(bot)
