"""Скил для Claude: получить информацию о работодателе по ID."""
from __future__ import annotations

import sys

from ._shared import call_api


def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: hh-get-employer <employer_id>\n")
        sys.exit(2)

    employer_id = sys.argv[1].strip()
    sys.stdout.write(call_api(f"/employers/{employer_id}"))


if __name__ == "__main__":
    main()
