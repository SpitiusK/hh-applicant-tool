from __future__ import annotations

from dataclasses import fields as dataclass_fields
from datetime import datetime
from typing import Any, Iterator

from ...utils import json as json_utils
from ..models.pending_message import PendingMessageModel
from .base import BaseRepository
from .errors import wrap_db_errors


_MODEL_FIELDS = {f.name: f for f in dataclass_fields(PendingMessageModel)}


def _encode_field(name: str, value: Any) -> Any:
    """Конвертация dict/list в JSON для store_json-полей (draft_payload,
    draft_history) — без этого SQLite падает на `Error binding parameter`.
    to_db() делает то же в create(), но update() биндит **kwargs напрямую
    и без этого helper'а."""
    mf = _MODEL_FIELDS.get(name)
    if mf is None:
        return value
    if mf.metadata.get("store_json") and not isinstance(value, (str, bytes)):
        if value is None:
            return None
        return json_utils.dumps(value)
    return value


class PendingMessagesRepository(BaseRepository):
    __table__ = "pending_messages"
    model = PendingMessageModel

    @wrap_db_errors
    def create(
        self, obj: PendingMessageModel, /, commit: bool | None = None
    ) -> int:
        data = obj.to_db()
        data.pop("id", None)
        for k in ("created_at", "updated_at", "dispatched_at"):
            if data.get(k) is None:
                data.pop(k, None)
        columns = list(data.keys())
        sql = (
            f"INSERT INTO {self.table_name} ({', '.join(columns)})"
            f" VALUES (:{', :'.join(columns)});"
        )
        cur = self.conn.execute(sql, data)
        obj.id = cur.lastrowid
        self.maybe_commit(commit)
        return cur.lastrowid

    def get_by_id(self, pk: int) -> PendingMessageModel | None:
        return self.get(pk)

    def get_by_status(self, status: str) -> Iterator[PendingMessageModel]:
        yield from self.find(status=status)

    def list_pending(self) -> Iterator[PendingMessageModel]:
        yield from self.find(status="pending")

    @wrap_db_errors
    def update_status(
        self,
        pk: int,
        status: str,
        /,
        dispatched_at: datetime | None = None,
        commit: bool | None = None,
    ) -> None:
        if dispatched_at is not None:
            self.conn.execute(
                f"UPDATE {self.table_name}"
                " SET status = ?, dispatched_at = ? WHERE id = ?;",
                (status, dispatched_at, pk),
            )
        else:
            self.conn.execute(
                f"UPDATE {self.table_name} SET status = ? WHERE id = ?;",
                (status, pk),
            )
        self.maybe_commit(commit)

    @wrap_db_errors
    def update(
        self, pk: int, /, commit: bool | None = None, **fields: Any
    ) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{k} = :{k}" for k in fields)
        params = {k: _encode_field(k, v) for k, v in fields.items()}
        params["__pk__"] = pk
        self.conn.execute(
            f"UPDATE {self.table_name} SET {assignments} WHERE id = :__pk__;",
            params,
        )
        self.maybe_commit(commit)
