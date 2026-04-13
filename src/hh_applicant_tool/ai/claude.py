import json
import logging
import subprocess
import time
from dataclasses import KW_ONLY, dataclass, field
from threading import Lock

from .base import AIError

logger = logging.getLogger(__package__)


class ClaudeError(AIError):
    pass


@dataclass
class ChatClaude:
    """AI-бэкенд через Claude CLI (claude -p).

    Использует подписку пользователя, а не API-ключ.
    """

    _: KW_ONLY

    system_prompt: str | None = None
    timeout: float = 60.0
    max_retries: int = 2
    model: str | None = None

    # количество запросов в минуту (0 = отключено)
    rate_limit: int = 10

    # Внутренние поля
    _previous_request_time: float = field(
        default=0.0, init=False
    )
    _lock: Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._lock = Lock()

    @property
    def _min_request_interval(self) -> float:
        return (
            60.0 / self.rate_limit
            if self.rate_limit > 0
            else 0.0
        )

    def _wait_rate_limit(self) -> None:
        if self._previous_request_time > 0:
            delay = (
                self._min_request_interval
                - time.monotonic()
                + self._previous_request_time
            )
            if delay > 0:
                logger.debug(
                    "Wait %.2fs before Claude request", delay
                )
                time.sleep(delay)

    def _build_cmd(self) -> list[str]:
        cmd = ["claude", "-p"]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def complete(self, message: str) -> str:
        """Генерация текста через Claude CLI."""
        prompt = message
        if self.system_prompt:
            prompt = (
                f"Системная инструкция: {self.system_prompt}"
                f"\n\n{message}"
            )

        cmd = self._build_cmd()

        for attempt in range(self.max_retries + 1):
            with self._lock:
                self._wait_rate_limit()
                try:
                    result = subprocess.run(
                        cmd,
                        input=prompt,
                        capture_output=True,
                        text=True,
                        timeout=self.timeout,
                    )
                finally:
                    self._previous_request_time = (
                        time.monotonic()
                    )

            if result.returncode == 0:
                response = result.stdout.strip()
                if not response:
                    raise ClaudeError(
                        "Claude CLI вернул пустой ответ"
                    )
                return response

            stderr = result.stderr.strip()
            logger.warning(
                "Claude CLI ошибка (попытка %d/%d): %s",
                attempt + 1,
                self.max_retries + 1,
                stderr,
            )

            if attempt < self.max_retries:
                delay = 2.0 * (attempt + 1)
                time.sleep(delay)

        raise ClaudeError(
            f"Claude CLI ошибка после "
            f"{self.max_retries + 1} попыток: "
            f"{result.stderr.strip()}"
        )

    def complete_json(self, message: str) -> dict | list:
        """Генерация JSON через Claude CLI."""
        if not message.rstrip().endswith(
            "JSON"
        ) and "json" not in message.lower():
            message += "\n\nОтветь строго в формате JSON."

        response = self.complete(message)

        # Пытаемся извлечь JSON из ответа
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = lines[1:]  # убираем ```json
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        try:
            return json.loads(text)
        except json.JSONDecodeError as ex:
            raise ClaudeError(
                f"Claude вернул невалидный JSON: {ex}\n"
                f"Ответ: {response[:500]}"
            ) from ex
