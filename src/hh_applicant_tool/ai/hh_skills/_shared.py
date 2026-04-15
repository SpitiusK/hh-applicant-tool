"""Общие утилиты для hh_skills-скриптов."""
from __future__ import annotations

import os
import subprocess
import sys


def call_api(endpoint: str, **params: str) -> str:
    """Вызывает hh-applicant-tool call-api и возвращает stdout.

    Используется тонкими обёртками-скилами. Авторизация + refresh
    токенов автоматически через основной CLI.
    """
    cmd = ["hh-applicant-tool"]
    config_dir = os.environ.get("CONFIG_DIR")
    if config_dir:
        cmd += ["-c", config_dir]
    cmd += ["call-api", endpoint]
    for k, v in params.items():
        cmd.append(f"{k}={v}")

    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
    return result.stdout
