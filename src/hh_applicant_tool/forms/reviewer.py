from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class ReviewVerdict:
    action: str  # "approve" | "reject" | "escalate"
    feedback: str = ""
    flagged_items: list[int] = field(default_factory=list)

    @classmethod
    def from_json(cls, text: str) -> ReviewVerdict:
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return cls(
                action="escalate",
                feedback=f"Не удалось разобрать ответ ревьюера: "
                f"{text[:300]}",
            )

        return cls(
            action=data.get("action", "escalate"),
            feedback=data.get("feedback", ""),
            flagged_items=data.get("flagged_items", []),
        )


@dataclass
class FormResult:
    status: str  # "submitted" | "escalated" | "error"
    answers: list[dict] = field(default_factory=list)
    reason: str = ""
