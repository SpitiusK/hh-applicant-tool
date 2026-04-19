from __future__ import annotations

from datetime import datetime
from typing import Iterator

from ..models.event import EventModel
from .base import BaseRepository
from .errors import wrap_db_errors


class EventsRepository(BaseRepository):
    __table__ = "events"
    model = EventModel

    @wrap_db_errors
    def create(self, obj: EventModel, /, commit: bool | None = None) -> int:
        data = obj.to_db()
        data.pop("id", None)
        if data.get("created_at") is None:
            data.pop("created_at", None)
        columns = list(data.keys())
        sql = (
            f"INSERT INTO {self.table_name} ({', '.join(columns)})"
            f" VALUES (:{', :'.join(columns)});"
        )
        cur = self.conn.execute(sql, data)
        obj.id = cur.lastrowid
        self.maybe_commit(commit)
        return cur.lastrowid

    def get_by_id(self, pk: int) -> EventModel | None:
        return self.get(pk)

    def _query(
        self,
        where: str = "",
        params: tuple = (),
        order: str = "ORDER BY created_at DESC, id DESC",
        limit: int = 50,
    ) -> Iterator[EventModel]:
        sql = f"SELECT * FROM {self.table_name}"
        if where:
            sql += f" WHERE {where}"
        sql += f" {order} LIMIT ?;"
        cur = self.conn.execute(sql, (*params, limit))
        for row in cur.fetchall():
            yield self._row_to_model(cur, row)

    def list_by_status(
        self, status: str, limit: int = 50
    ) -> Iterator[EventModel]:
        yield from self._query("status = ?", (status,), limit=limit)

    def list_by_type(self, type: str, limit: int = 50) -> Iterator[EventModel]:
        yield from self._query("type = ?", (type,), limit=limit)

    def list_confirmed_since(
        self, since: datetime, limit: int = 100
    ) -> Iterator[EventModel]:
        yield from self._query(
            "status = 'confirmed' AND when_ts IS NOT NULL AND when_ts >= ?",
            (since,),
            order="ORDER BY when_ts ASC, id ASC",
            limit=limit,
        )

    @wrap_db_errors
    def update_status(
        self, pk: int, status: str, /, commit: bool | None = None
    ) -> None:
        self.conn.execute(
            f"UPDATE {self.table_name} SET status = ? WHERE id = ?;",
            (status, pk),
        )
        self.maybe_commit(commit)

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
