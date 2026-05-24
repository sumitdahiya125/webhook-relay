"""SQLite-backed event/attempt storage.

Schema is intentionally small. Indexed on ``(status, next_attempt_at)`` so the
worker poll can grab the next batch efficiently.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

import aiosqlite

from webhook_relay.models import (
    DeliveryAttempt,
    DeliveryStatus,
    Endpoint,
    WebhookEvent,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS endpoints (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    description TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    endpoint_id TEXT NOT NULL REFERENCES endpoints(id),
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT,
    last_status_code INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_status_next ON events (status, next_attempt_at);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES events(id),
    attempted_at TEXT NOT NULL,
    status_code INTEGER,
    response_body TEXT,
    error TEXT,
    duration_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_event ON attempts (event_id);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


class Storage:
    def __init__(self, db_path: str):
        # Accept both "sqlite:///path" and bare paths.
        if db_path.startswith("sqlite:///"):
            db_path = db_path[len("sqlite:///") :]
        self.db_path = db_path

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            yield db

    async def init(self) -> None:
        async with self._conn() as db:
            await db.executescript(SCHEMA)
            await db.commit()

    # ---- endpoints --------------------------------------------------------

    async def upsert_endpoint(self, ep: Endpoint) -> None:
        async with self._conn() as db:
            await db.execute(
                """
                INSERT INTO endpoints (id, url, description, active, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    url = excluded.url,
                    description = excluded.description,
                    active = excluded.active
                """,
                (ep.id, str(ep.url), ep.description, int(ep.active), _iso(ep.created_at)),
            )
            await db.commit()

    async def get_endpoint(self, endpoint_id: str) -> Endpoint | None:
        async with self._conn() as db:
            cur = await db.execute(
                "SELECT id, url, description, active, created_at FROM endpoints WHERE id = ?",
                (endpoint_id,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            return Endpoint(
                id=row["id"],
                url=row["url"],
                description=row["description"],
                active=bool(row["active"]),
                created_at=_parse_iso(row["created_at"]),
            )

    async def list_endpoints(self) -> list[Endpoint]:
        async with self._conn() as db:
            cur = await db.execute(
                "SELECT id, url, description, active, created_at FROM endpoints ORDER BY id"
            )
            return [
                Endpoint(
                    id=r["id"],
                    url=r["url"],
                    description=r["description"],
                    active=bool(r["active"]),
                    created_at=_parse_iso(r["created_at"]),
                )
                async for r in cur
            ]

    # ---- events -----------------------------------------------------------

    async def enqueue(
        self, endpoint_id: str, event_type: str, payload: dict, now: datetime
    ) -> WebhookEvent:
        event_id = f"evt_{uuid.uuid4().hex[:16]}"
        async with self._conn() as db:
            await db.execute(
                """
                INSERT INTO events
                  (id, endpoint_id, event_type, payload, status,
                   attempts, next_attempt_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (
                    event_id,
                    endpoint_id,
                    event_type,
                    json.dumps(payload, separators=(",", ":"), sort_keys=True),
                    _iso(now),
                    _iso(now),
                    _iso(now),
                ),
            )
            await db.commit()
        return WebhookEvent(
            id=event_id,
            endpoint_id=endpoint_id,
            event_type=event_type,
            payload=payload,
            status=DeliveryStatus.PENDING,
            attempts=0,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )

    async def claim_due(self, now: datetime, batch_size: int) -> list[WebhookEvent]:
        """Atomically mark up to ``batch_size`` due events as in_flight, return them."""
        async with self._conn() as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                """
                SELECT id, endpoint_id, event_type, payload, status,
                       attempts, next_attempt_at, created_at, updated_at,
                       last_error, last_status_code
                FROM events
                WHERE status IN ('pending', 'failed')
                  AND next_attempt_at <= ?
                ORDER BY next_attempt_at
                LIMIT ?
                """,
                (_iso(now), batch_size),
            )
            rows = await cur.fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                placeholders = ",".join("?" * len(ids))
                await db.execute(
                    f"UPDATE events SET status = 'in_flight', updated_at = ? "
                    f"WHERE id IN ({placeholders})",
                    (_iso(now), *ids),
                )
            await db.commit()
            return [_row_to_event(r) for r in rows]

    async def mark_succeeded(self, event_id: str, now: datetime) -> None:
        async with self._conn() as db:
            await db.execute(
                "UPDATE events SET status = 'succeeded', updated_at = ?, attempts = attempts + 1 "
                "WHERE id = ?",
                (_iso(now), event_id),
            )
            await db.commit()

    async def mark_failed(
        self,
        event_id: str,
        now: datetime,
        next_attempt_at: datetime,
        last_error: str | None,
        last_status_code: int | None,
        dead_letter: bool,
    ) -> None:
        new_status = "dead_lettered" if dead_letter else "failed"
        async with self._conn() as db:
            await db.execute(
                """
                UPDATE events SET
                    status = ?,
                    updated_at = ?,
                    next_attempt_at = ?,
                    attempts = attempts + 1,
                    last_error = ?,
                    last_status_code = ?
                WHERE id = ?
                """,
                (
                    new_status,
                    _iso(now),
                    _iso(next_attempt_at),
                    last_error,
                    last_status_code,
                    event_id,
                ),
            )
            await db.commit()

    async def record_attempt(
        self,
        event_id: str,
        now: datetime,
        status_code: int | None,
        response_body: str | None,
        error: str | None,
        duration_ms: int,
    ) -> None:
        async with self._conn() as db:
            await db.execute(
                """
                INSERT INTO attempts
                  (event_id, attempted_at, status_code, response_body, error, duration_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_id, _iso(now), status_code, response_body, error, duration_ms),
            )
            await db.commit()

    async def get_event(self, event_id: str) -> WebhookEvent | None:
        async with self._conn() as db:
            cur = await db.execute(
                """
                SELECT id, endpoint_id, event_type, payload, status,
                       attempts, next_attempt_at, created_at, updated_at,
                       last_error, last_status_code
                FROM events WHERE id = ?
                """,
                (event_id,),
            )
            row = await cur.fetchone()
            return _row_to_event(row) if row else None

    async def list_attempts(self, event_id: str) -> list[DeliveryAttempt]:
        async with self._conn() as db:
            cur = await db.execute(
                """
                SELECT id, event_id, attempted_at, status_code, response_body, error, duration_ms
                FROM attempts WHERE event_id = ? ORDER BY attempted_at
                """,
                (event_id,),
            )
            return [
                DeliveryAttempt(
                    id=r["id"],
                    event_id=r["event_id"],
                    attempted_at=_parse_iso(r["attempted_at"]),
                    status_code=r["status_code"],
                    response_body=r["response_body"],
                    error=r["error"],
                    duration_ms=r["duration_ms"],
                )
                async for r in cur
            ]

    async def replay(self, event_id: str, now: datetime) -> bool:
        """Mark a previously failed/dead-lettered event as pending again."""
        async with self._conn() as db:
            cur = await db.execute(
                "UPDATE events SET status = 'pending', next_attempt_at = ?, "
                "updated_at = ?, attempts = 0, last_error = NULL, last_status_code = NULL "
                "WHERE id = ?",
                (_iso(now), _iso(now), event_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def stats(self) -> dict[str, int]:
        async with self._conn() as db:
            cur = await db.execute(
                "SELECT status, COUNT(*) AS n FROM events GROUP BY status"
            )
            result = {s.value: 0 for s in DeliveryStatus}
            async for row in cur:
                result[row["status"]] = row["n"]
            return result


def _row_to_event(row: aiosqlite.Row) -> WebhookEvent:
    return WebhookEvent(
        id=row["id"],
        endpoint_id=row["endpoint_id"],
        event_type=row["event_type"],
        payload=json.loads(row["payload"]),
        status=DeliveryStatus(row["status"]),
        attempts=row["attempts"],
        next_attempt_at=_parse_iso(row["next_attempt_at"]),
        created_at=_parse_iso(row["created_at"]),
        updated_at=_parse_iso(row["updated_at"]),
        last_error=row["last_error"] if "last_error" in row.keys() else None,
        last_status_code=row["last_status_code"] if "last_status_code" in row.keys() else None,
    )
