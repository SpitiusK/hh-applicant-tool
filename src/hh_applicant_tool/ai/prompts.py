"""Системные суффиксы-инструкции для AI-вызовов.

Каждый суффикс описывает требуемый JSON-формат ответа. Подмешивается
в конец system/user prompt, чтобы модель возвращала структуру, которую
можно провалидировать через pydantic-схему из `ai/schema.py`.
"""

from __future__ import annotations

def build_system_prompt(base_rules: str, persona: str) -> str:
    """Подмешать persona.md в system prompt.

    Пустой persona возвращает base_rules без изменений — регрессии нет
    для пользователей без persona.md.
    """
    if not persona:
        return base_rules
    return (
        base_rules
        + "\n\n# PROFESSIONAL PROFILE (контекст для ответов от первого лица)\n\n"
        + persona
    )


AI_RESPONSE_JSON_SUFFIX = """
Ответь СТРОГО в формате JSON-объекта без пояснений и без markdown-обёртки.
Структура:
{
  "answer": <строка или объект — содержательный ответ>,
  "confidence": <число от 0.0 до 1.0 — насколько ты уверен в ответе>,
  "escalate": <true|false — нужно ли эскалировать решение человеку>,
  "escalation_reason": <null или строка — почему эскалируешь; один из:
      "ai_unclear" (модель не уверена),
      "missing_data" (не хватает данных),
      "policy_conflict" (запрос противоречит правилам),
      "user_decision_needed" (нужно решение человека)>,
  "question_for_user": <null или строка — конкретный вопрос человеку, если escalate=true>,
  "context_summary": <null или строка — короткая сводка контекста (<=200 символов)>
}

Правила:
- confidence=0.9+ только если ты действительно уверен.
- Если данных мало или запрос двусмысленный — escalate=true и задай question_for_user.
- Не выдумывай факты о пользователе, которых нет в контексте.
""".strip()
