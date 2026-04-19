from __future__ import annotations

from datetime import datetime
from typing import Iterator

from ..models.ai_decision import AiDecisionModel
from .base import BaseRepository
from .errors import wrap_db_errors


def _truncate_preview(text: str | None, limit: int = 200) -> str | None:
    if text is None:
        return None
    return text[:limit]


class AiDecisionsRepository(BaseRepository):
    __table__ = "ai_decisions"
    model = AiDecisionModel

    @wrap_db_errors
    def create(
        self, obj: AiDecisionModel, /, commit: bool | None = None
    ) -> int:
        if obj.result_preview is not None:
            obj.result_preview = _truncate_preview(obj.result_preview)
        data = obj.to_db()
        data.pop("id", None)
        if data.get("created_at") is None:
            data.pop("created_at", None)
        for bool_field in ("escalated", "is_sentinel", "sample_for_review", "flagged"):
            if bool_field in data:
                data[bool_field] = int(bool(data[bool_field]))
        columns = list(data.keys())
        sql = (
            f"INSERT INTO {self.table_name} ({', '.join(columns)})"
            f" VALUES (:{', :'.join(columns)});"
        )
        cur = self.conn.execute(sql, data)
        obj.id = cur.lastrowid
        self.maybe_commit(commit)
        return cur.lastrowid

    def _query_recent(
        self,
        where: str = "",
        params: tuple = (),
        limit: int = 20,
    ) -> Iterator[AiDecisionModel]:
        sql = f"SELECT * FROM {self.table_name}"
        if where:
            sql += f" WHERE {where}"
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?;"
        cur = self.conn.execute(sql, (*params, limit))
        for row in cur.fetchall():
            yield self._row_to_model(cur, row)

    def list_recent(
        self,
        limit: int = 50,
        operation: str | None = None,
        status: str | None = None,
    ) -> Iterator[AiDecisionModel]:
        clauses: list[str] = []
        params: list = []
        if operation is not None:
            clauses.append("operation = ?")
            params.append(operation)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = " AND ".join(clauses)
        yield from self._query_recent(where, tuple(params), limit)

    def list_flagged(self, limit: int = 20) -> Iterator[AiDecisionModel]:
        yield from self._query_recent("flagged = 1", (), limit)

    def list_sentinels(self, limit: int = 20) -> Iterator[AiDecisionModel]:
        yield from self._query_recent("is_sentinel = 1", (), limit)

    def list_samples_for_review(
        self, limit: int = 20
    ) -> Iterator[AiDecisionModel]:
        yield from self._query_recent("sample_for_review = 1", (), limit)

    @wrap_db_errors
    def count_by_status(self, since: datetime | None = None) -> dict[str, int]:
        sql = f"SELECT status, COUNT(*) FROM {self.table_name}"
        params: tuple = ()
        if since is not None:
            sql += " WHERE created_at >= ?"
            params = (since,)
        sql += " GROUP BY status;"
        cur = self.conn.execute(sql, params)
        return {row[0]: row[1] for row in cur.fetchall()}

    @wrap_db_errors
    def count_by_operation(
        self, since: datetime | None = None
    ) -> dict[str, int]:
        sql = f"SELECT operation, COUNT(*) FROM {self.table_name}"
        params: tuple = ()
        if since is not None:
            sql += " WHERE created_at >= ?"
            params = (since,)
        sql += " GROUP BY operation;"
        cur = self.conn.execute(sql, params)
        return {row[0]: row[1] for row in cur.fetchall()}

    @wrap_db_errors
    def mark_flagged(
        self, pk: int, flag_reason: str, /, commit: bool | None = None
    ) -> None:
        self.conn.execute(
            f"UPDATE {self.table_name}"
            " SET flagged = 1, flag_reason = ? WHERE id = ?;",
            (flag_reason, pk),
        )
        self.maybe_commit(commit)

    @wrap_db_errors
    def mark_sample_for_review(
        self, pk: int, /, commit: bool | None = None
    ) -> None:
        self.conn.execute(
            f"UPDATE {self.table_name}"
            " SET sample_for_review = 1 WHERE id = ?;",
            (pk,),
        )
        self.maybe_commit(commit)

    @wrap_db_errors
    def _rate(self, column: str, since: datetime | None = None) -> float:
        sql = (
            f"SELECT"
            f" COALESCE(SUM({column}), 0) AS hits,"
            f" COUNT(*) AS total"
            f" FROM {self.table_name}"
        )
        params: tuple = ()
        if since is not None:
            sql += " WHERE created_at >= ?"
            params = (since,)
        cur = self.conn.execute(sql, params)
        hits, total = cur.fetchone()
        if not total:
            return 0.0
        return hits / total

    def sentinel_rate(self, since: datetime | None = None) -> float:
        return self._rate("is_sentinel", since)

    def escalation_rate(self, since: datetime | None = None) -> float:
        return self._rate("escalated", since)
