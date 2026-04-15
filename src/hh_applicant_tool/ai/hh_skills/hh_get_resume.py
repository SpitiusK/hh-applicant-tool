"""Скил для Claude: получить полное резюме кандидата по ID.

Если ID не указан, возвращает первое резюме из /resumes/mine.
"""
from __future__ import annotations

import json
import sys

from ._shared import call_api


def main() -> None:
    if len(sys.argv) >= 2:
        resume_id = sys.argv[1].strip()
    else:
        mine = json.loads(call_api("/resumes/mine"))
        items = mine.get("items", [])
        if not items:
            sys.stderr.write("No resumes found\n")
            sys.exit(1)
        resume_id = items[0]["id"]

    sys.stdout.write(call_api(f"/resumes/{resume_id}"))


if __name__ == "__main__":
    main()
