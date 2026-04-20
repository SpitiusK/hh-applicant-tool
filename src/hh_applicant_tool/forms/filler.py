from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..ai.base import AIError
from ..ai.claude import ChatClaude, ClaudeError
from .reviewer import FormResult, ReviewVerdict

logger = logging.getLogger(__package__)


@dataclass
class FormFiller:
    """Заполнение веб-форм через двухагентную схему:
    fill -> review -> submit.
    """

    user_data: dict
    model: str | None = None
    timeout: float = 180.0
    rate_limit: int = 10
    approval_mode: str = "on_escalation"
    # П.21: storage + messenger_factory делают escalate основным путём.
    # При любой ошибке messaging-пути — failover в review_queue.jsonl.
    storage: Any | None = None
    messenger_factory: Callable[[], Any] | None = None
    approval_cfg: dict[str, Any] = field(default_factory=dict)
    messenger_type: str = "telegram"
    review_queue_path: Path = field(
        default_factory=lambda: Path("data/review_queue.jsonl")
    )

    # Per-stage ChatClaude-клиенты — создаются в __post_init__, чтобы все
    # вызовы claude CLI шли через общий путь complete() с rate-limit lock
    # (П.5). У каждого инстанса свой Lock, это не глобальный throttle
    # между стадиями, но внутри одной стадии гонок не будет и легко
    # переехать на общий лок в будущем.
    _filler_claude: ChatClaude = field(init=False, repr=False)
    _reviewer_claude: ChatClaude = field(init=False, repr=False)
    _submit_claude: ChatClaude = field(init=False, repr=False)

    # Workaround для контейнера: хостовский installed_plugins.json подкидывает
    # Windows-пути → playwright marketplace plugin не находится. Передаём
    # абсолютный путь к каталогу плагина через --plugin-dir, тогда Claude CLI
    # подхватывает session-only без marketplace-резолва.
    # На host'е путь другой — через CONFIG_DIR можно переопределить.
    playwright_plugin_dir: str = (
        "/home/docker/.claude/plugins/marketplaces/"
        "claude-plugins-official/external_plugins/playwright"
    )

    def __post_init__(self) -> None:
        # MCP-инструменты playwright плагина: Claude CLI требует явное
        # разрешение, иначе filler_agent упирается в "разрешите browser_navigate".
        # Паттерн `mcp__<plugin>__*` покрывает все browser_* tools.
        tool_scope: list[str] = [
            "mcp__playwright__*",
            # резервный pattern с дефисом — разные версии плагина дают
            # разный делимитер slug'а (видели оба: `-` и `_`).
            "mcp__plugin_playwright_playwright__*",
        ]
        # Filler и submit нуждаются в Playwright. Reviewer — нет.
        plugin_dirs = (
            [self.playwright_plugin_dir]
            if self.playwright_plugin_dir
            else []
        )
        self._filler_claude = ChatClaude(
            model=self.model,
            timeout=self.timeout,
            rate_limit=self.rate_limit,
            allowed_tools=tool_scope,
            plugin_dirs=plugin_dirs,
        )
        self._reviewer_claude = ChatClaude(
            model=self.model,
            timeout=60.0,
            rate_limit=self.rate_limit,
        )
        self._submit_claude = ChatClaude(
            model=self.model,
            timeout=self.timeout,
            rate_limit=self.rate_limit,
            allowed_tools=tool_scope,
            plugin_dirs=plugin_dirs,
        )

    def fill_form(
        self,
        url: str,
        vacancy_context: str = "",
        dry_run: bool = False,
    ) -> FormResult:
        """Заполнить форму по URL.

        1. Filler-агент читает форму, предлагает ответы
        2. Reviewer-агент проверяет ответы
        3. Действие по вердикту: отправить / повторить / эскалировать
        """
        try:
            proposed = self._run_filler_agent(
                url, vacancy_context
            )
        except AIError as ex:
            logger.error("Filler agent ошибка: %s", ex)
            return FormResult(
                status="error", reason=str(ex)
            )

        verdict = self._run_reviewer_agent(
            url, proposed, vacancy_context
        )

        if verdict.action == "approve":
            if dry_run:
                logger.info(
                    "dry-run: форма не отправлена: %s", url
                )
                return FormResult(
                    status="submitted", answers=proposed
                )
            self._run_submit_agent(url, proposed)
            return FormResult(
                status="submitted", answers=proposed
            )

        if verdict.action == "reject":
            # Одна попытка с фидбеком от ревьюера
            try:
                proposed2 = self._run_filler_agent(
                    url,
                    vacancy_context,
                    feedback=verdict.feedback,
                )
            except AIError as ex:
                logger.error(
                    "Filler agent повторная ошибка: %s", ex
                )
                self._escalate_form_review(
                    url, proposed, verdict, vacancy_context
                )
                return FormResult(
                    status="escalated",
                    answers=proposed,
                    reason=str(ex),
                )

            verdict2 = self._run_reviewer_agent(
                url, proposed2, vacancy_context
            )
            if verdict2.action == "approve":
                if dry_run:
                    logger.info(
                        "dry-run: форма не отправлена: %s",
                        url,
                    )
                    return FormResult(
                        status="submitted", answers=proposed2
                    )
                self._run_submit_agent(url, proposed2)
                return FormResult(
                    status="submitted", answers=proposed2
                )

            self._escalate_form_review(
                url, proposed2, verdict2, vacancy_context
            )
            return FormResult(
                status="escalated",
                answers=proposed2,
                reason=verdict2.feedback,
            )

        # escalate
        self._escalate_form_review(
            url, proposed, verdict, vacancy_context
        )
        return FormResult(
            status="escalated",
            answers=proposed,
            reason=verdict.feedback,
        )

    def _run_filler_agent(
        self,
        url: str,
        vacancy_context: str,
        feedback: str | None = None,
    ) -> list[dict]:
        user_data_json = json.dumps(
            self.user_data, ensure_ascii=False, indent=2
        )

        feedback_block = ""
        if feedback:
            feedback_block = (
                f"\nПредыдущая попытка была отклонена. "
                f"Замечания ревьюера:\n{feedback}\n"
            )

        prompt = f"""Перейди по ссылке {url} и проанализируй форму.
НЕ НАЖИМАЙ кнопку отправки (Submit/Отправить)!

Для каждого вопроса/поля предложи ответ на основе данных кандидата:
{user_data_json}

Контекст вакансии: {vacancy_context}
{feedback_block}
Инструкции:
- Пройди все страницы (нажимай "Продолжить"/"Next"/"Далее")
- Для каждого поля запиши: текст вопроса, тип поля, предлагаемый ответ
- Верни ТОЛЬКО JSON массив: [{{"question": "...", "field_type": "...", "answer": "..."}}]
- НЕ нажимай Submit/Отправить
"""

        try:
            response = self._filler_claude.complete(prompt).strip()
        except ClaudeError as ex:
            raise AIError(f"Filler agent ошибка: {ex}") from ex

        return self._parse_json_response(response)

    def _run_reviewer_agent(
        self,
        url: str,
        proposed_answers: list[dict],
        vacancy_context: str,
    ) -> ReviewVerdict:
        answers_json = json.dumps(
            proposed_answers, ensure_ascii=False, indent=2
        )
        user_data_json = json.dumps(
            self.user_data, ensure_ascii=False, indent=2
        )

        prompt = f"""Ты — ревьюер автоматического заполнения анкет.

Форма: {url}
Вакансия: {vacancy_context}

Предложенные ответы:
{answers_json}

Реальные данные кандидата:
{user_data_json}

ПРОВЕРЬ каждый ответ на нарушения:
1. КОНФИДЕНЦИАЛЬНЫЕ ДАННЫЕ — утечка чувствительной информации \
(банковские реквизиты, пароли, внутренние проекты, точный адрес)
2. РАСКРЫТИЕ AI — фразы вроде "Как AI", "Я языковая модель", \
"у меня нет личного опыта"
3. НЕСООТВЕТСТВИЕ — ответ не соответствует вопросу или \
противоречит данным кандидата
4. НЕУМЕСТНОСТЬ — непрофессиональный ответ, вредит шансам кандидата

Верни JSON:
{{"action": "approve"|"reject"|"escalate", \
"feedback": "объяснение если не approve", \
"flagged_items": [индексы проблемных ответов]}}

Правила:
- "approve" = все ответы безопасны для отправки
- "reject" = нужны исправления (дай конкретный фидбек)
- "escalate" = нужно решение человека (неоднозначный вопрос)
- По умолчанию "approve", если нет реальных проблем
"""

        try:
            response = self._reviewer_claude.complete(prompt)
        except ClaudeError as ex:
            logger.warning("Reviewer agent ошибка: %s", ex)
            return ReviewVerdict(
                action="escalate",
                feedback=f"Ошибка ревьюера: {ex}",
            )

        return ReviewVerdict.from_json(response)

    def _run_submit_agent(
        self, url: str, approved_answers: list[dict]
    ) -> None:
        answers_json = json.dumps(
            approved_answers, ensure_ascii=False, indent=2
        )

        prompt = f"""Перейди по ссылке {url} и заполни форму \
этими ответами, затем отправь:
{answers_json}

- Заполни каждое поле соответствующим ответом
- Пройди все страницы, нажимая Продолжить/Next/Далее
- На последней странице нажми Submit/Отправить
- Подтверди успешную отправку
"""

        try:
            self._submit_claude.complete(prompt)
        except ClaudeError as ex:
            logger.error("Submit agent ошибка: %s", ex)

    def _escalate_form_review(
        self,
        url: str,
        proposed: list[dict],
        verdict: ReviewVerdict,
        vacancy_context: str,
    ) -> None:
        """П.21: основной путь — pending_messages + messenger.

        При любой ошибке (нет storage/factory, messenger падает, create
        падает) — failover в review_queue.jsonl, чтобы форма не терялась.
        """
        if self.storage is None or self.messenger_factory is None:
            logger.info(
                "form escalate: storage/messenger_factory не заданы, "
                "failover в jsonl для %s",
                url,
            )
            self._save_to_queue(url, proposed, verdict, vacancy_context)
            return

        try:
            from ..storage.models.pending_message import PendingMessageModel

            draft_id = self.storage.pending_messages.create(
                PendingMessageModel(
                    messenger_type=self.messenger_type,
                    action_type="form_field",
                    draft_payload={
                        "url": url,
                        "answers": proposed,
                        "vacancy": vacancy_context,
                        "verdict_feedback": verdict.feedback,
                        "flagged_items": verdict.flagged_items,
                    },
                    status="pending",
                    question_for_user=(
                        verdict.feedback
                        or "Проверь форму перед отправкой"
                    ),
                    context_summary=f"form: {url}",
                    confidence=0.0,
                    escalation_reason=(
                        verdict.feedback or "reviewer_escalated"
                    ),
                )
            )
            messenger = self.messenger_factory()
            if messenger is None:
                raise RuntimeError(
                    "messenger_factory() вернула None"
                )
            text = (
                f"📝 Form escalation: {url}\n"
                f"Feedback: {verdict.feedback or '—'}"
            )
            external_id = messenger.send_approval_request(
                draft_id=draft_id,
                text=text,
                actions=["approve", "reject"],
            )
            try:
                self.storage.pending_messages.update(
                    draft_id, messenger_message_id=external_id
                )
            except Exception:
                logger.exception(
                    "form escalate: не удалось сохранить messenger_message_id #%s",
                    draft_id,
                )
            logger.info(
                "form escalation sent to messenger: pm#%s %s",
                draft_id,
                url,
            )
        except Exception as ex:
            logger.error(
                "FAILOVER: messaging escalation failed (%s), "
                "fallback в review_queue.jsonl",
                ex,
            )
            self._save_to_queue(url, proposed, verdict, vacancy_context)

    def _save_to_queue(
        self,
        url: str,
        proposed: list[dict],
        verdict: ReviewVerdict,
        vacancy_context: str,
    ) -> None:
        """Failover-путь (П.21): JSONL-очередь для ручного ревью когда
        messaging недоступен. Не используется как основной escalate.
        """
        self.review_queue_path.parent.mkdir(
            parents=True, exist_ok=True
        )

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "url": url,
            "vacancy": vacancy_context,
            "proposed_answers": proposed,
            "reviewer_feedback": verdict.feedback,
            "flagged_items": verdict.flagged_items,
            "status": "pending",
        }

        with open(self.review_queue_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False))
            f.write("\n")

        logger.info(
            "Форма добавлена в очередь ревью (failover jsonl): %s", url
        )

    @staticmethod
    def _parse_json_response(
        response: str,
    ) -> list[dict]:
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as ex:
            raise AIError(
                f"Невалидный JSON от filler-агента: {ex}\n"
                f"Ответ: {response[:500]}"
            ) from ex

        if not isinstance(data, list):
            raise AIError(
                "Filler-агент вернул не массив: "
                f"{type(data)}"
            )

        return data
