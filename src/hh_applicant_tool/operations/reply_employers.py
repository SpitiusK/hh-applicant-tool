from __future__ import annotations

import argparse
import json
import logging
import random
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ..ai.agent import ReplyAgent
from ..ai.base import AIError
from ..ai.claude import ChatClaude
from ..ai.schema import AIResponse
from ..api import ApiError, datatypes
from ..approval import (
    escalate_to_user,
    generate_with_self_assessment,
    persist_ai_decision,
    should_escalate,
)
from ..forms.filler import FormFiller
from ..forms.journal import append_confirmation, append_event
from ..main import BaseNamespace, BaseOperation
from ..utils.date import parse_api_datetime
from ..utils.string import rand_text

if TYPE_CHECKING:
    from ..main import HHApplicantTool


try:
    import readline

    readline.add_history("/cancel ")
    readline.add_history("/ban")
    readline.set_history_length(10_000)
except ImportError:
    pass


logger = logging.getLogger(__package__)


class Namespace(BaseNamespace):
    reply_message: str
    max_pages: int
    only_invitations: bool
    dry_run: bool
    use_ai: bool
    use_claude: bool
    fill_forms: bool
    approval_mode: Literal["never", "on_escalation", "always"] | None
    system_prompt: str
    message_prompt: str
    period: int


class Operation(BaseOperation):
    """Ответ всем работодателям."""

    __aliases__ = ["reply-empls", "reply-chats", "reall"]

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--resume-id",
            help="Идентификатор резюме. Если не указан, то просматриваем чаты для всех резюме",
        )
        parser.add_argument(
            "-m",
            "--reply-message",
            "--reply",
            help="Отправить сообщение во все чаты. Если не передать сообщение, то нужно будет вводить его в интерактивном режиме.",  # noqa: E501
        )
        parser.add_argument(
            "--period",
            type=int,
            help="Игнорировать отклики, которые не обновлялись больше N дней",
        )
        parser.add_argument(
            "-p",
            "--max-pages",
            type=int,
            default=25,
            help="Максимальное количество страниц для проверки",
        )
        parser.add_argument(
            "-oi",
            "--only-invitations",
            help="Отвечать только на приглашения",
            default=False,
            action=argparse.BooleanOptionalAction,
        )
        parser.add_argument(
            "--dry-run",
            "--dry",
            help="Не отправлять сообщения, а только выводить параметры запроса",
            default=False,
            action=argparse.BooleanOptionalAction,
        )
        ai_group = parser.add_mutually_exclusive_group()
        ai_group.add_argument(
            "--use-ai",
            "--ai",
            help="Использовать OpenAI для автоматической генерации ответов",
            action=argparse.BooleanOptionalAction,
        )
        ai_group.add_argument(
            "--use-claude",
            help="Использовать Claude CLI для генерации ответов (подписка, не API)",
            action=argparse.BooleanOptionalAction,
        )
        parser.add_argument(
            "--fill-forms",
            help="Заполнять анкеты/формы по ссылкам из сообщений работодателей",
            default=False,
            action=argparse.BooleanOptionalAction,
        )
        parser.add_argument(
            "--approval-mode",
            help="Когда эскалировать действие человеку: never — автономно всегда, on_escalation — только при AI.escalate или низкой confidence (дефолт из config.approval.mode, иначе 'on_escalation'), always — на каждое действие.",
            choices=["never", "on_escalation", "always"],
            default=None,
        )
        parser.add_argument(
            "--system-prompt",
            "--ai-system",
            help="Системный промпт для AI",
            default=(
                "Ты отвечаешь работодателю В ЧАТЕ на hh.ru ОТ ИМЕНИ СОИСКАТЕЛЯ "
                "(Карнаухов Александр, руководитель отдела маркетинга). "
                "Строгие правила:\n"
                "1. Пиши ТОЛЬКО от первого лица (я, мой, мне). НИКОГДА не "
                "пиши от лица HR/работодателя/рекрутёра.\n"
                "2. Не раскрывай, что ты ИИ. Никаких фраз вроде 'как языковая "
                "модель', 'я бот', 'у меня нет опыта как у человека'.\n"
                "3. Не выдумывай навыки/опыт, которых нет в данных кандидата. "
                "Если чего-то нет — прямо признай и подсвети смежный опыт.\n"
                "4. Формат работы: ТОЛЬКО удалёнка или гибрид (предпочтительно "
                "удалёнка). Фулл-тайм офис не рассматриваю. Про переезд не "
                "упоминай, это обсудим на собеседовании.\n"
                "5. Зарплатные ожидания: ориентируюсь на вилку из вакансии, "
                "минимум 100–120 тыс. руб., предпочтительно от 150 тыс. Для "
                "руководящих позиций — от 150 тыс. Конкретную сумму готов "
                "обсудить.\n"
                "6. Тон: деловой, тёплый, краткий. Без канцелярита, без "
                "подписей вроде 'С уважением, команда'. Подпись не нужна "
                "(это чат, не письмо).\n"
                "7. Длина ответа: 2–4 предложения максимум, если контекст не "
                "требует иного.\n"
                "8. Если работодатель задаёт конкретные вопросы — отвечай "
                "предметно. Если просто написал приветствие — задай "
                "встречный уточняющий вопрос о задачах/команде."
            ),
        )
        parser.add_argument(
            "--message-prompt",
            "--prompt",
            help="Промпт для генерации сообщения",
            default=(
                "Напиши ответ работодателю от лица соискателя на основе "
                "истории переписки и данных кандидата. Соблюдай все правила "
                "из системного промпта. Верни ТОЛЬКО текст сообщения, без "
                "префиксов 'Ответ:', 'Сообщение:', без кавычек и пояснений."
            ),
        )

    def run(self, tool: HHApplicantTool, args: Namespace) -> None:
        self.tool = tool
        self.api_client = tool.api_client
        self.resume_id = tool.first_resume_id()
        self.reply_message = args.reply_message or tool.config.get(
            "reply_message"
        )
        self.max_pages = args.max_pages
        self.dry_run = args.dry_run
        self.only_invitations = args.only_invitations

        self.message_prompt = args.message_prompt
        self.user_data = tool.config.get("form_user_data", {})
        self.use_claude = bool(args.use_claude)
        if args.use_claude:
            self.cover_letter_ai = (
                tool.get_cover_letter_claude(args.system_prompt)
            )
        elif args.use_ai:
            self.cover_letter_ai = (
                tool.get_cover_letter_ai(args.system_prompt)
            )
        else:
            self.cover_letter_ai = None
        self.period = args.period

        # Пути для журналов событий и подтверждений
        data_dir = Path(tool.config_dir) / "data"
        self.events_jsonl = data_dir / "scheduled_events.jsonl"
        self.agenda_md = data_dir / "agenda.md"
        self.confirmations_jsonl = data_dir / "pending_confirmations.jsonl"

        # Агент Claude для структурированных ответов
        if self.use_claude and isinstance(
            self.cover_letter_ai, ChatClaude
        ):
            self.reply_agent = ReplyAgent(self.cover_letter_ai)
        else:
            self.reply_agent = None

        self.approval_defaults = tool.get_approval_defaults()
        self.approval_mode = (
            args.approval_mode or self.approval_defaults["mode"]
        )
        self.approval_cfg = {
            **self.approval_defaults,
            "mode": self.approval_mode,
        }
        self._messenger = None

        self.fill_forms = args.fill_forms
        if self.fill_forms:
            claude_cfg = tool.config.get("claude", {})
            self.form_filler = FormFiller(
                user_data=tool.config.get(
                    "form_user_data", {}
                ),
                model=claude_cfg.get("model"),
                timeout=claude_cfg.get("timeout", 120.0),
                approval_mode=self.approval_mode,
            )
        else:
            self.form_filler = None

        logger.debug(f"{self.reply_message = }")
        self.reply_employers()

    def reply_employers(self):
        blacklist = set(self.tool.get_blacklisted())
        me: datatypes.User = self.tool.get_me()
        resumes = self.tool.get_resumes()
        resumes = (
            list(filter(lambda x: x["id"] == self.resume_id, resumes))
            if self.resume_id
            else resumes
        )
        resumes = list(
            filter(
                lambda resume: resume["status"]["id"] == "published", resumes
            )
        )
        self._reply_chats(user=me, resumes=resumes, blacklist=blacklist)

    def _reply_chats(
        self,
        user: datatypes.User,
        resumes: list[datatypes.Resume],
        blacklist: set[str],
    ) -> None:
        resume_map = {r["id"]: r for r in resumes}

        base_placeholders = {
            "first_name": user.get("first_name") or "",
            "last_name": user.get("last_name") or "",
            "email": user.get("email") or "",
            "phone": user.get("phone") or "",
        }

        for negotiation in self.tool.get_negotiations():
            try:
                # try:
                #     self.tool.storage.negotiations.save(negotiation)
                # except RepositoryError as e:
                #     logger.exception(e)

                negotiation_resume = negotiation.get("resume")
                if not negotiation_resume:
                    continue
                if not (resume := resume_map.get(negotiation_resume["id"])):
                    continue

                updated_at = parse_api_datetime(negotiation["updated_at"])

                # Пропуск откликов, которые не обновлялись более N дней (при просмотре они обновляются вроде)
                if (
                    self.period
                    and (datetime.now(updated_at.tzinfo) - updated_at).days
                    > self.period
                ):
                    continue

                state_id = negotiation["state"]["id"]
                if state_id == "discard":
                    continue

                if self.only_invitations and not state_id.startswith("inv"):
                    continue

                nid = negotiation["id"]
                vacancy = negotiation["vacancy"]
                employer = vacancy.get("employer") or {}
                salary = vacancy.get("salary") or {}

                if employer.get("id") in blacklist:
                    print(
                        "🚫 Пропускаем заблокированного работодателя",
                        employer.get("alternate_url"),
                    )
                    continue

                placeholders = {
                    "vacancy_name": vacancy.get("name", ""),
                    "employer_name": employer.get("name", ""),
                    "resume_title": resume.get("title") or "",
                    **base_placeholders,
                }

                logger.debug(
                    "Вакансия %(vacancy_name)s от %(employer_name)s"
                    % placeholders
                )

                page: int = 0
                last_message: datatypes.Message | None = None
                message_history: list[str] = []
                raw_messages: list[dict] = []
                while True:
                    messages_res: datatypes.PaginatedItems[
                        datatypes.Message
                    ] = self.api_client.get(
                        f"/negotiations/{nid}/messages", page=page
                    )
                    if not messages_res["items"]:
                        break

                    last_message = messages_res["items"][-1]
                    for message in messages_res["items"]:
                        if not message.get("text"):
                            continue
                        raw_messages.append(message)
                        author = (
                            "Работодатель"
                            if message["author"]["participant_type"]
                            == "employer"
                            else "Я"
                        )
                        message_date = parse_api_datetime(
                            message.get("created_at")
                        ).strftime("%d.%m.%Y %H:%M:%S")

                        message_history.append(
                            f"[ {message_date} ] {author}: {message['text']}"
                        )

                    if page + 1 >= messages_res["pages"]:
                        break
                    page += 1

                if not last_message:
                    continue

                # Поиск ссылок на формы в сообщениях
                if self.form_filler:
                    self._process_form_urls(
                        nid,
                        messages_res,
                        vacancy_context=(
                            f"{placeholders['vacancy_name']} "
                            f"от {placeholders['employer_name']}"
                        ),
                    )

                is_employer_message = (
                    last_message["author"]["participant_type"] == "employer"
                )

                if is_employer_message:
                    send_message = ""
                    if self.reply_message:
                        send_message = (
                            rand_text(self.reply_message) % placeholders
                        )
                        logger.debug(f"Template message: {send_message}")
                    elif self.reply_agent is not None:
                        result = self.reply_agent.process_chat(
                            negotiation, raw_messages, self.user_data
                        )
                        if result.action == "skip":
                            logger.info(
                                "skip chat %s: %s",
                                nid,
                                result.skip_reason or "no reason",
                            )
                            continue
                        if result.action == "error":
                            logger.warning(
                                "agent error for chat %s: %s",
                                nid,
                                result.skip_reason,
                            )
                            continue
                        send_message = result.reply

                        # AIResponse для approval-loop: наличие confirmations
                        # — сильный сигнал к эскалации (агент сам просит
                        # подтвердить что-то у пользователя).
                        has_confirmations = bool(result.confirmations)
                        ai_resp = AIResponse(
                            answer=send_message,
                            confidence=0.6 if has_confirmations else 0.9,
                            escalate=has_confirmations,
                            escalation_reason=(
                                "agent_requested_confirmation"
                                if has_confirmations
                                else None
                            ),
                            question_for_user=(
                                "; ".join(
                                    c.get("question", "")
                                    for c in result.confirmations
                                )
                                if has_confirmations
                                else None
                            ),
                        )
                        if should_escalate(
                            ai_resp, "reply_employer", self.approval_cfg
                        ):
                            draft_payload = {
                                "nid": nid,
                                "endpoint": f"/negotiations/{nid}/messages",
                                "message": send_message,
                                "vacancy_name": placeholders.get("vacancy_name"),
                                "employer_name": placeholders.get("employer_name"),
                            }
                            escalate_to_user(
                                self.tool.storage,
                                self._get_messenger(),
                                action_type="reply_employer",
                                draft_payload=draft_payload,
                                ai_response=ai_resp,
                                approval_cfg=self.approval_cfg,
                            )
                            logger.info(
                                "reply эскалирован на approval: chat %s", nid
                            )
                            continue
                        persist_ai_decision(
                            self.tool.storage,
                            operation="reply_employer",
                            ai_response=ai_resp,
                            status="auto_dispatched",
                            negotiation_id=(
                                int(nid) if str(nid).isdigit() else None
                            ),
                            result_preview=send_message[:200],
                            messenger=self._get_messenger(),
                            approval_cfg=self.approval_cfg,
                        )
                        logger.info(
                            "AI reply for %s: %s",
                            placeholders["vacancy_name"],
                            send_message,
                        )
                        chat_context = {
                            "chat_id": nid,
                            "vacancy_id": vacancy.get("id"),
                            "vacancy_url": vacancy.get("alternate_url"),
                            "vacancy_name": vacancy.get("name"),
                            "employer_name": employer.get("name"),
                        }
                        for ev in result.events:
                            append_event(
                                ev,
                                chat_context=chat_context,
                                jsonl_path=self.events_jsonl,
                                md_path=self.agenda_md,
                            )
                        for cf in result.confirmations:
                            append_confirmation(
                                cf,
                                chat_context=chat_context,
                                proposed_reply=send_message,
                                jsonl_path=self.confirmations_jsonl,
                            )
                    elif self.cover_letter_ai:
                        user_data_block = ""
                        if self.user_data:
                            user_data_block = (
                                "\n\nДанные кандидата (используй "
                                "по ситуации):\n"
                                + json.dumps(
                                    self.user_data,
                                    ensure_ascii=False,
                                    indent=2,
                                )
                            )

                        ai_query = (
                            f"Вакансия: {placeholders['vacancy_name']}\n"
                            f"Работодатель: {placeholders['employer_name']}"
                            f"\n\nИстория переписки:\n"
                            + "\n".join(message_history)
                            + user_data_block
                            + f"\n\nИнструкция: {self.message_prompt}"
                        )
                        ai_resp = generate_with_self_assessment(
                            self.cover_letter_ai, ai_query
                        )
                        if ai_resp.is_sentinel and not isinstance(
                            ai_resp.answer, str
                        ):
                            logger.warning(
                                "sentinel AIResponse for chat %s — skip", nid
                            )
                            continue
                        send_message = (
                            ai_resp.answer
                            if isinstance(ai_resp.answer, str)
                            else str(ai_resp.answer)
                        )
                        logger.debug(f"AI message: {send_message}")

                        if should_escalate(
                            ai_resp, "reply_employer", self.approval_cfg
                        ):
                            draft_payload = {
                                "nid": nid,
                                "endpoint": f"/negotiations/{nid}/messages",
                                "message": send_message,
                                "vacancy_name": placeholders.get("vacancy_name"),
                                "employer_name": placeholders.get("employer_name"),
                            }
                            escalate_to_user(
                                self.tool.storage,
                                self._get_messenger(),
                                action_type="reply_employer",
                                draft_payload=draft_payload,
                                ai_response=ai_resp,
                                approval_cfg=self.approval_cfg,
                            )
                            logger.info(
                                "reply эскалирован на approval (openai): chat %s",
                                nid,
                            )
                            continue
                        persist_ai_decision(
                            self.tool.storage,
                            operation="reply_employer",
                            ai_response=ai_resp,
                            status="auto_dispatched",
                            negotiation_id=(
                                int(nid) if str(nid).isdigit() else None
                            ),
                            result_preview=send_message[:200],
                            messenger=self._get_messenger(),
                            approval_cfg=self.approval_cfg,
                        )
                    else:
                        print("🏢", placeholders["employer_name"])
                        print("💼", placeholders["vacancy_name"])
                        if salary:
                            print(
                                "💵 от",
                                salary.get("from") or salary.get("to") or 0,
                                "до",
                                salary.get("to") or salary.get("from") or 0,
                                salary.get("currency", "RUR"),
                            )

                        print("\nПоследние сообщения чата:")
                        print()
                        for msg in (
                            message_history[-5:]
                            if len(message_history) > 5
                            else message_history
                        ):
                            print(msg)

                        try:
                            print("-" * 40)
                            print("Активное резюме:", resume.get("title") or "")
                            print(
                                "/ban, /cancel необязательное сообщение для отмены"
                            )
                            send_message = input("Ваше сообщение: ").strip()
                        except EOFError:
                            continue

                        if not send_message:
                            print("🚶 Пропускаем чат")
                            continue

                        if send_message.startswith("/ban"):
                            self.api_client.put(
                                f"/employers/blacklisted/{employer['id']}"
                            )
                            blacklist.add(employer["id"])
                            print(
                                "🚫 Работодатель заблокирован",
                                employer.get("alternate_url"),
                            )
                            continue
                        elif send_message.startswith("/cancel"):
                            _, decline_msg = send_message.split("/cancel", 1)
                            self.api_client.delete(
                                f"/negotiations/active/{nid}",
                                with_decline_message=decline_msg.strip(),
                            )
                            print("❌ Отмена заявки", vacancy["alternate_url"])
                            continue

                    # Финальная отправка текста
                    if self.dry_run:
                        logger.debug(
                            "dry-run: отклик на %s: %s",
                            vacancy["alternate_url"],
                            send_message,
                        )
                        continue

                    self.api_client.post(
                        f"/negotiations/{nid}/messages",
                        message=send_message,
                        delay=random.uniform(1, 3),
                    )
                    logger.info(
                        "📨 Отправлено для %s", vacancy["alternate_url"]
                    )

            except ApiError as ex:
                logger.error(ex)

        print("📝 Сообщения разосланы!")

    _URL_PATTERN = re.compile(r"https?://\S+")
    _FORM_DOMAINS = (
        "forms.google.com",
        "docs.google.com/forms",
        "forms.yandex.ru",
        "typeform.com",
        "surveymonkey.com",
        "anketolog.ru",
    )

    def _get_messenger(self):
        if self._messenger is not None:
            return self._messenger
        try:
            from ..messaging import get_messenger_client

            self._messenger = get_messenger_client(
                self.tool.config, self.tool.storage
            )
        except Exception as ex:
            logger.warning(
                "messenger не инициализирован (%s); эскалации без нотификации",
                ex,
            )
            self._messenger = None
        return self._messenger

    def _process_form_urls(
        self,
        nid: str,
        messages_res: datatypes.PaginatedItems[
            datatypes.Message
        ],
        vacancy_context: str,
    ) -> None:
        form_urls: list[str] = []
        for msg in messages_res["items"]:
            if (
                msg["author"]["participant_type"]
                == "employer"
                and msg.get("text")
            ):
                urls = self._URL_PATTERN.findall(
                    msg["text"]
                )
                for url in urls:
                    if any(
                        d in url for d in self._FORM_DOMAINS
                    ):
                        form_urls.append(url)

        for url in form_urls:
            logger.info(
                "Найдена ссылка на форму в чате %s: %s",
                nid,
                url,
            )
            try:
                result = self.form_filler.fill_form(
                    url,
                    vacancy_context=vacancy_context,
                    dry_run=self.dry_run,
                )
                if result.status == "submitted":
                    print(
                        f"📋 Форма заполнена: {url}"
                    )
                elif result.status == "escalated":
                    print(
                        f"⚠️ Форма в очереди ревью: "
                        f"{url} ({result.reason})"
                    )
                else:
                    print(
                        f"❌ Ошибка формы: "
                        f"{url} ({result.reason})"
                    )
            except Exception as ex:
                logger.error(
                    "Ошибка заполнения формы %s: %s",
                    url,
                    ex,
                )
