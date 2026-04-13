from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..ai.base import AIError
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
    review_queue_path: Path = field(
        default_factory=lambda: Path("data/review_queue.jsonl")
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
                self._save_to_queue(
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

            self._save_to_queue(
                url, proposed2, verdict2, vacancy_context
            )
            return FormResult(
                status="escalated",
                answers=proposed2,
                reason=verdict2.feedback,
            )

        # escalate
        self._save_to_queue(
            url, proposed, verdict, vacancy_context
        )
        return FormResult(
            status="escalated",
            answers=proposed,
            reason=verdict.feedback,
        )

    def _build_cmd(self) -> list[str]:
        cmd = ["claude", "-p"]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

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

        result = subprocess.run(
            self._build_cmd(),
            input=prompt,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )

        if result.returncode != 0:
            raise AIError(
                f"Filler agent ошибка: {result.stderr.strip()}"
            )

        response = result.stdout.strip()
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

        result = subprocess.run(
            self._build_cmd(),
            input=prompt,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            logger.warning(
                "Reviewer agent ошибка: %s",
                result.stderr.strip(),
            )
            return ReviewVerdict(
                action="escalate",
                feedback=f"Ошибка ревьюера: "
                f"{result.stderr.strip()}",
            )

        return ReviewVerdict.from_json(result.stdout)

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

        result = subprocess.run(
            self._build_cmd(),
            input=prompt,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )

        if result.returncode != 0:
            logger.error(
                "Submit agent ошибка: %s",
                result.stderr.strip(),
            )

    def _save_to_queue(
        self,
        url: str,
        proposed: list[dict],
        verdict: ReviewVerdict,
        vacancy_context: str,
    ) -> None:
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
            "Форма добавлена в очередь ревью: %s", url
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
