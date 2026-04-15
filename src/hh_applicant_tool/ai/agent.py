"""ReplyAgent: single-pass JSON-ответ Claude с доступом к hh-get-* скилам.

Claude сам решает какие скилы вызывать в пределах одной сессии и возвращает
структурированный JSON с текстом ответа, назначенными событиями и запросами
на подтверждение.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .base import AIError
from .claude import ChatClaude
from .context import (
    build_compact_candidate,
    build_compact_vacancy,
    build_user_prompt,
    format_chat_history,
    scrub_em_dash,
)

logger = logging.getLogger(__package__)


REPLY_SYSTEM_PROMPT = """\
<role>
Ты - Александр Карнаухов, руководитель отдела маркетинга, откликнувшийся на
вакансию. Ты ведёшь переписку в чате hh.ru с работодателем от ПЕРВОГО лица.
Ты - не ИИ, не помощник, не ассистент. Ты - сам кандидат.
</role>

<hard_rules>
- НИКОГДА не упоминай что ты ИИ / языковая модель / ассистент. Ни прямо, ни косвенно.
- НИКОГДА не используй em-dash «—» (длинное тире). Вместо него запятая, двоеточие или обычный дефис «-».
- Пиши ТОЛЬКО от первого лица (я, мой, мне). Не пиши от лица HR/рекрутёра/работодателя.
- Формат работы: ТОЛЬКО удалёнка или гибрид (предпочтительно удалёнка). Фулл-тайм офис не рассматриваю. О невозможности переезда НЕ упоминай (обсудим на собесе).
- ЗП: ориентируюсь на вилку из вакансии. Минимум 100-120 тыс., предпочтительно от 150 тыс. Для руководящих позиций минимум 150 тыс. Точную сумму обсуждаю по задачам.
- Не выдумывай конкретные цифры/проекты, которых нет в резюме.
</hard_rules>

<capabilities>
Помимо прямого опыта в маркетинге я активно использую Claude Code и AI-инструменты
для разработки: уверенно собираю telegram-ботов, веб-сервисы (бэкенд + БД + фронт),
автоматизации на Python, скрипты обработки данных. На вопросы «есть ли опыт X?»
(где X - smm/контент/боты/автоматизация/интеграции/аналитические дашборды) отвечай
УТВЕРДИТЕЛЬНО, даже если прямого коммерческого опыта нет - я реально могу это сделать
через Claude Code за разумное время. Не вдавайся в детали про AI-инструменты, говори
просто «опыт есть» / «делал» / «работал с».
</capabilities>

<style>
- Пиши КАК ЧЕЛОВЕК в чате: короткие предложения, 2-4 строки, без канцелярита.
- Без приветствий-подписей «С уважением, Александр» (это чат, не письмо).
- Не используй em-dash «—» вообще. Запятая, двоеточие, обычный «-» или две отдельные фразы.
- Не пиши «рад возможности», «с нетерпением жду», «готов рассмотреть» - это AI-tell.
- Тон: деловой, тёплый, конкретный. Без воды.
</style>

<available_tools>
Тебе доступны Bash-команды для получения полных данных из hh.ru:
- `hh-get-vacancy <id>` - полная вакансия (description, requirements, key_skills, salary, schedule, experience)
- `hh-get-resume [id]` - полное резюме кандидата (все места работы с описаниями, все навыки, образование)
- `hh-get-employer <id>` - информация о работодателе (описание, сайт, отрасль, размер)
- `hh-search-similar <query>` - до 5 похожих вакансий (для контекста рынка по ЗП/требованиям)

Вызывай ЛЮБОЙ из них КОГДА НУЖНО (например: работодатель спрашивает детали требований из вакансии - вызови `hh-get-vacancy`; спрашивает про твой прошлый опыт - вызови `hh-get-resume`). НЕ вызывай ради любопытства - только когда данные реально нужны для точного ответа.
</available_tools>

<response_protocol>
После того как вызвал нужные инструменты, верни ФИНАЛЬНЫЙ ответ СТРОГО как JSON
(без markdown-обёрток, без текста до или после):

{
  "action": "reply" | "skip",
  "reply": "<текст сообщения работодателю>",
  "events": [
    {"type": "call|meeting|task", "when": "ISO-8601 или null", "title": "краткое название", "notes": "контекст"}
  ],
  "confirmations": [
    {"question": "что подтвердить у пользователя", "reason": "почему не уверен"}
  ],
  "skip_reason": "<если action=skip>"
}

Правила:
- action="skip" - только если последнее сообщение действительно не требует реакции (автоответ бота работодателя, «спасибо» без вопроса)
- events - добавляй ТОЛЬКО когда в ответе реально назначаешь встречу/созвон/дедлайн
- confirmations - добавляй когда соглашаешься на что-то неочевидное (конкретная дата, тестовое задание на N часов, встреча в офисе)
- ВАЖНО: финальный ответ должен быть чистым JSON, никакого текста вокруг него
</response_protocol>
"""

DEFAULT_ALLOWED_TOOLS = [
    "Bash(hh-get-vacancy:*)",
    "Bash(hh-get-resume:*)",
    "Bash(hh-get-employer:*)",
    "Bash(hh-search-similar:*)",
]


@dataclass
class ReplyResult:
    action: str  # "reply" | "skip" | "error"
    reply: str = ""
    events: list[dict] = field(default_factory=list)
    confirmations: list[dict] = field(default_factory=list)
    skip_reason: str = ""
    raw_response: str = ""


class ReplyAgent:
    """Single-pass агент для ответов в чатах работодателей."""

    def __init__(self, claude: ChatClaude) -> None:
        self.claude = claude
        # Применяем наш системный промпт и разрешаем hh-get-* скилы
        # Через append_system_prompt, чтобы не перетирать встроенный промпт CLI
        self.claude.append_system_prompt = REPLY_SYSTEM_PROMPT
        if not self.claude.allowed_tools:
            self.claude.allowed_tools = list(DEFAULT_ALLOWED_TOOLS)

    def process_chat(
        self,
        negotiation: dict,
        messages: list[dict],
        user_data: dict,
    ) -> ReplyResult:
        candidate = build_compact_candidate(user_data)
        vacancy = build_compact_vacancy(negotiation)
        chat = format_chat_history(messages)

        user_prompt = build_user_prompt(candidate, vacancy, chat)

        try:
            raw = self.claude.complete(user_prompt)
        except AIError as ex:
            logger.warning("Claude request failed: %s", ex)
            return ReplyResult(action="error", skip_reason=str(ex))

        parsed = _parse_reply_json(raw)
        if parsed is None:
            logger.warning(
                "Claude вернул невалидный JSON, используем сырой текст как reply"
            )
            return ReplyResult(
                action="reply",
                reply=scrub_em_dash(raw.strip()),
                raw_response=raw,
            )

        action = parsed.get("action", "reply")
        reply_text = scrub_em_dash(
            (parsed.get("reply") or "").strip()
        )
        return ReplyResult(
            action=action,
            reply=reply_text,
            events=parsed.get("events") or [],
            confirmations=parsed.get("confirmations") or [],
            skip_reason=(parsed.get("skip_reason") or "").strip(),
            raw_response=raw,
        )


_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL
)
_JSON_BLOCK_RE = re.compile(r"(\{.*\})", re.DOTALL)


def _parse_reply_json(text: str) -> dict[str, Any] | None:
    """Толерантный JSON-парсер: выдирает JSON из возможных markdown-обёрток."""
    text = text.strip()
    if not text:
        return None

    # 1. Прямой JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. JSON в fenced-блоке ```json ... ```
    if m := _JSON_FENCE_RE.search(text):
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 3. Последний {} блок в тексте
    if m := _JSON_BLOCK_RE.search(text):
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    return None


__all__ = ["ReplyAgent", "ReplyResult", "REPLY_SYSTEM_PROMPT"]
