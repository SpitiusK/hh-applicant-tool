from __future__ import annotations

import argparse
import csv
import logging
import pathlib
import sqlite3
import sys
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from prettytable import PrettyTable

from ..main import BaseNamespace, BaseOperation

if TYPE_CHECKING:
    from ..main import HHApplicantTool

try:
    import readline

    readline.parse_and_bind("tab: complete")
except ImportError:
    readline = None

MAX_RESULTS = 10


logger = logging.getLogger(__package__)


class Namespace(BaseNamespace):
    pass


class Operation(BaseOperation):
    """Выполняет SQL-запрос. Поддерживает вывод в консоль или CSV файл."""

    __aliases__: list[str] = ["sql"]

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("sql", nargs="?", help="SQL запрос")
        parser.add_argument(
            "--csv", action="store_true", help="Вывести результат в формате CSV"
        )
        parser.add_argument(
            "-o",
            "--output",
            type=pathlib.Path,
            help="Файл для сохранения",
        )
        parser.add_argument(
            "--ai-stats",
            action="store_true",
            help="Статистика ai_decisions за 7 дней (counts by operation/status)",
        )
        parser.add_argument(
            "--escalation-rate",
            nargs="?",
            const=7,
            type=int,
            metavar="DAYS",
            help="Доля escalated в ai_decisions за N дней (default 7)",
        )
        parser.add_argument(
            "--sentinel-rate",
            nargs="?",
            const=7,
            type=int,
            metavar="DAYS",
            help="Доля is_sentinel в ai_decisions за N дней (default 7)",
        )
        parser.add_argument(
            "--flagged",
            action="store_true",
            help="Последние 20 flagged записей ai_decisions",
        )

    def run(self, tool: HHApplicantTool, args: Namespace) -> None:
        def execute(sql_query: str) -> None:
            sql_query = sql_query.strip()
            if not sql_query:
                return
            try:
                cursor = tool.db.cursor()
                cursor.execute(sql_query)

                if cursor.description:
                    columns = [d[0] for d in cursor.description]

                    if args.csv or args.output:
                        # Если -o не задан, используем sys.stdout
                        output = (
                            args.output.open("w", encoding="utf-8")
                            if args.output
                            else sys.stdout
                        )
                        writer = csv.writer(output)
                        writer.writerow(columns)
                        writer.writerows(cursor.fetchall())

                        if output is not sys.stdout:
                            print(f"✅  Exported to {output.name}")
                        return

                    rows = cursor.fetchmany(MAX_RESULTS + 1)
                    if not rows:
                        print("No results found.")
                        return

                    table = PrettyTable()
                    table.field_names = columns
                    for row in rows[:MAX_RESULTS]:
                        table.add_row(row)

                    print(table)

                    if len(rows) > MAX_RESULTS:
                        print(
                            f"⚠️  Warning: Showing only first {MAX_RESULTS} results."
                        )
                else:
                    tool.db.commit()

                    if cursor.rowcount > 0:
                        print(f"Rows affected: {cursor.rowcount}")

            except sqlite3.Error as ex:
                print(f"❌  SQL Error: {ex}")
                return 1

        if (
            args.ai_stats
            or args.escalation_rate is not None
            or args.sentinel_rate is not None
            or args.flagged
        ):
            return _run_ai_analytics(tool, args)

        if initial_sql := args.sql:
            return execute(initial_sql)

        if not sys.stdin.isatty():
            return execute(sys.stdin.read())

        print("SQL Console (q or ^D to exit)")
        try:
            while True:
                try:
                    user_input = input("query> ").strip()
                    if user_input.lower() in (
                        "exit",
                        "quit",
                        "q",
                    ):
                        break
                    execute(user_input)
                    print()
                except KeyboardInterrupt:
                    print("^C")
                    continue
        except EOFError:
            print()


def _run_ai_analytics(tool: "HHApplicantTool", args: Namespace) -> None:
    repo = tool.storage.ai_decisions

    if args.ai_stats:
        since = datetime.utcnow() - timedelta(days=7)
        by_op = repo.count_by_operation(since=since)
        by_status = repo.count_by_status(since=since)
        total = sum(by_op.values())
        print(f"AI decisions за последние 7 дней: {total} decisions")
        if total == 0:
            return
        t_op = PrettyTable()
        t_op.field_names = ["operation", "count"]
        for op, n in sorted(by_op.items(), key=lambda x: -x[1]):
            t_op.add_row([op, n])
        print(t_op)
        t_st = PrettyTable()
        t_st.field_names = ["status", "count"]
        for st, n in sorted(by_status.items(), key=lambda x: -x[1]):
            t_st.add_row([st, n])
        print(t_st)

    if args.escalation_rate is not None:
        days = args.escalation_rate
        since = datetime.utcnow() - timedelta(days=days)
        rate = repo.escalation_rate(since=since)
        print(f"Escalation rate за {days} дн: {rate:.2%}")

    if args.sentinel_rate is not None:
        days = args.sentinel_rate
        since = datetime.utcnow() - timedelta(days=days)
        rate = repo.sentinel_rate(since=since)
        print(f"Sentinel rate за {days} дн: {rate:.2%}")

    if args.flagged:
        rows = list(repo.list_flagged(limit=20))
        if not rows:
            print("No flagged decisions.")
            return
        table = PrettyTable()
        table.field_names = [
            "id",
            "operation",
            "vacancy_id",
            "flag_reason",
            "created_at",
        ]
        for r in rows:
            table.add_row(
                [r.id, r.operation, r.vacancy_id, r.flag_reason, r.created_at]
            )
        print(table)
