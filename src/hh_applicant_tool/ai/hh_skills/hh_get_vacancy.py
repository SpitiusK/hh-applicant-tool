"""Скил для Claude: получить полную вакансию по ID."""
from __future__ import annotations

import sys

from ._shared import call_api


def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: hh-get-vacancy <vacancy_id>\n")
        sys.exit(2)

    vacancy_id = sys.argv[1].strip()
    sys.stdout.write(call_api(f"/vacancies/{vacancy_id}"))


if __name__ == "__main__":
    main()
