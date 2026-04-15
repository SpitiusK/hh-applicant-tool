"""Скил для Claude: найти до 5 похожих вакансий по тексту запроса."""
from __future__ import annotations

import json
import sys

from ._shared import call_api


def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: hh-search-similar <query>\n")
        sys.exit(2)

    query = " ".join(sys.argv[1:]).strip()
    raw = call_api("/vacancies", text=query, per_page="5", page="0")
    data = json.loads(raw)

    # Сжимаем — возвращаем только ключевые поля
    items = data.get("items", [])
    compact = []
    for v in items:
        salary = v.get("salary") or {}
        compact.append({
            "id": v.get("id"),
            "name": v.get("name"),
            "employer": (v.get("employer") or {}).get("name"),
            "area": (v.get("area") or {}).get("name"),
            "salary_from": salary.get("from"),
            "salary_to": salary.get("to"),
            "currency": salary.get("currency"),
            "schedule": (v.get("schedule") or {}).get("name"),
            "url": v.get("alternate_url"),
        })
    sys.stdout.write(json.dumps(
        {"items": compact, "found": data.get("found", 0)},
        ensure_ascii=False, indent=2,
    ))


if __name__ == "__main__":
    main()
