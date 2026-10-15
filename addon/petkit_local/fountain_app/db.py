"""Reads and writes of the fountain app's own tables, on the event store.

Our tables share `petkit.db` with upstream's, so they go through the store's
own engine and its single write lock (`EventStore._read`/`_write`): SQLite
allows one writer, and the store's guarantee that no in-process coroutine meets
SQLITE_BUSY only holds if every writer takes that lock. Reaching into those
private members is the price of not editing `events/store.py`; it is confined
to this module. The two reads of upstream's `events` that `EventStore` has no
method for (whole episodes by link, error rows by page) live here too.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import REAL, ColumnElement, and_, case, cast, delete, func, or_, select, update

from petkit_local.events import codes
from petkit_local.events.models import Event, Media
from petkit_local.fountain_app.models import FountainAlert, FountainBase, VisitLabel
from petkit_local.fountain_app.visits import reported_start

if TYPE_CHECKING:
    from petkit_local.events.store import EventStore


class TableGuard:
    """Creates our tables once per process, from start-up or the first request."""

    def __init__(self) -> None:
        self._ready = False
        self._lock = asyncio.Lock()

    async def ensure(self, store: EventStore) -> None:
        """Create any missing fountain table. Idempotent and cheap once done."""
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            # Upstream's schema first, so a fresh file is never one that holds
            # only our tables when its own `connect()` runs.
            await store._ensure_connected()
            async with store._write_lock:
                async with store._engine.begin() as conn:
                    await conn.run_sync(FountainBase.metadata.create_all)
            self._ready = True


async def labels_for(store: EventStore, device_id: int,
                     visit_keys: list[str] | None = None) -> dict[str, str]:
    """`visit_key -> label` for one device, optionally only for some keys."""
    stmt = select(VisitLabel.visit_key, VisitLabel.label).where(
        VisitLabel.device_id == device_id)
    if visit_keys is not None:
        if not visit_keys:
            return {}
        stmt = stmt.where(VisitLabel.visit_key.in_(visit_keys))
    async with store._read() as session:
        return {k: v for k, v in (await session.execute(stmt)).all()}


async def all_labels(store: EventStore, device_id: int) -> list[dict]:
    """Every label of one device, oldest label first."""
    async with store._read() as session:
        rows = await session.scalars(
            select(VisitLabel).where(VisitLabel.device_id == device_id)
            .order_by(VisitLabel.labeled_at))
        return [{"visit_key": r.visit_key, "label": r.label, "labeled_at": r.labeled_at}
                for r in rows]


async def set_label(store: EventStore, device_id: int, visit_key: str, label: str,
                    now: float | None = None) -> None:
    """Insert or replace the label of one visit."""
    labeled_at = now if now is not None else time.time()
    async with store._write() as session:
        row = await session.scalar(select(VisitLabel).where(
            VisitLabel.device_id == device_id, VisitLabel.visit_key == visit_key))
        if row is None:
            session.add(VisitLabel(device_id=device_id, visit_key=visit_key,
                                   label=label, labeled_at=labeled_at))
        else:
            row.label = label
            row.labeled_at = labeled_at


async def delete_label(store: EventStore, device_id: int, visit_key: str) -> bool:
    """Remove one visit's label; True if there was one."""
    async with store._write() as session:
        result = await session.execute(
            delete(VisitLabel).where(VisitLabel.device_id == device_id,
                                     VisitLabel.visit_key == visit_key),
            execution_options={"synchronize_session": False})
        return bool(result.rowcount)


def _alert_dict(row: FountainAlert) -> dict[str, Any]:
    try:
        detail = json.loads(row.detail) if row.detail else {}
    except ValueError:
        detail = {}
    return {"id": row.id, "kind": row.kind, "subject": row.subject,
            "started_at": row.started_at, "cleared_at": row.cleared_at,
            "detail": detail if isinstance(detail, dict) else {}}


async def active_alerts(store: EventStore) -> list[dict[str, Any]]:
    """Every alert not yet cleared, oldest first."""
    async with store._read() as session:
        rows = await session.scalars(
            select(FountainAlert).where(FountainAlert.cleared_at.is_(None))
            .order_by(FountainAlert.started_at, FountainAlert.id))
        return [_alert_dict(r) for r in rows]


async def cleared_alerts(store: EventStore, since: float, limit: int = 50) -> list[dict[str, Any]]:
    """Alerts cleared at or after `since`, most recently cleared first."""
    async with store._read() as session:
        rows = await session.scalars(
            select(FountainAlert).where(FountainAlert.cleared_at >= since)
            .order_by(FountainAlert.cleared_at.desc()).limit(limit))
        return [_alert_dict(r) for r in rows]


async def raise_alert(store: EventStore, kind: str, subject: str, detail: dict[str, Any],
                      now: float) -> dict[str, Any]:
    """Record a new active alert and return it."""
    async with store._write() as session:
        row = FountainAlert(kind=kind, subject=subject, started_at=now,
                            detail=json.dumps(detail))
        session.add(row)
        await session.flush()
        return _alert_dict(row)


async def clear_alert(store: EventStore, alert_id: int, now: float) -> None:
    """Mark one alert as ended."""
    async with store._write() as session:
        await session.execute(
            update(FountainAlert).where(FountainAlert.id == alert_id)
            .values(cleared_at=now),
            execution_options={"synchronize_session": False})

# --- upstream events, read for visits and alerts ------------------------------

#: `IN (...)` lists are split to stay under SQLite's bound-variable limit.
_IN_CHUNK = 400
VISIT_KINDS = (codes.KIND_PET, codes.KIND_DRINKING)
#: How far the device clock may run ahead of ours. A row reports a start
#: before it arrives, however late it arrives, so rows that arrived more than
#: this before a span cannot start in it; the bound keeps the read on the
#: `(device_id, ts)` index instead of decoding the device's whole history.
DEVICE_CLOCK_AHEAD_SEC = 86400
#: Content keys a visit row may report its episode's start under; the exact
#: rule per row is `visits.reported_start`.
_START_KEYS = ("event_start", "start_time", "mark")


async def linked_rows(store: EventStore, device_id: int,
                      keys: list[str]) -> list[dict[str, Any]]:
    """Visit-kind rows whose `related_event` or `parent_event` is in `keys`,
    whenever they arrived, each carrying its `"media"` as `query_timeline` does."""
    rows: list[dict[str, Any]] = []
    async with store._read() as session:
        for i in range(0, len(keys), _IN_CHUNK):
            chunk = keys[i:i + _IN_CHUNK]
            found = await session.scalars(select(Event).where(
                Event.device_id == device_id, Event.event_kind.in_(VISIT_KINDS),
                or_(Event.related_event.in_(chunk), Event.parent_event.in_(chunk))))
            rows += [e.as_dict() for e in found]
        related = sorted({r["related_event"] for r in rows if r.get("related_event")})
        media: dict[str, list[dict[str, Any]]] = {}
        for i in range(0, len(related), _IN_CHUNK):
            found = await session.scalars(
                select(Media).where(Media.related_event.in_(related[i:i + _IN_CHUNK]))
                .order_by(Media.created_at))
            for m in found:
                media.setdefault(m.related_event, []).append(m.as_dict())
    for r in rows:
        r["media"] = media.get(r.get("related_event") or "", [])
    return rows


def _may_start_in(lo: float, hi: float) -> ColumnElement[bool]:
    """SQL prefilter: rows that may report a start in `[lo, hi)`.

    A superset of what `visits.reported_start` keeps: arrival time or any
    start key in range, plus content SQLite cannot parse (the device's
    bare-token JSON, which Python repairs and SQL cannot), left for Python.
    """
    content = Event.content_json
    valid = func.json_valid(content) == 1

    def reported(key: str) -> ColumnElement[Any]:
        return cast(case((valid, func.json_extract(content, f"$.{key}"))), REAL)

    in_range = [and_(col >= lo, col < hi) for col in
                [Event.ts] + [reported(k) for k in _START_KEYS]]
    return or_(*in_range, and_(content.is_not(None), func.json_valid(content) == 0))


async def visit_rows(store: EventStore, device_id: int, start_ts: float, end_ts: float,
                     window_sec: float) -> list[dict[str, Any]]:
    """The rows of every visit that can start in `[start_ts, end_ts)`, episodes whole.

    Episodes are found by the start they REPORT (`visits.reported_start`),
    not by when their rows arrived: a drink-over with no drink-start is the
    episode's only row, and the device may deliver it hours late (the PC is
    often off overnight). The span is widened by `window_sec` so a drink can
    join an appearance across the boundary. Every episode found is then read
    whole by `related_event` and `parent_event`, so a drink that ends after
    midnight keeps its drink-over and its duration. The caller keeps a visit
    only if it starts in the span, and each day computes the same start from
    the same whole episodes, so no visit is listed on two days.
    """
    lo, hi = start_ts - window_sec, end_ts + window_sec
    async with store._read() as session:
        found = await session.scalars(select(Event).where(
            Event.device_id == device_id, Event.event_kind.in_(VISIT_KINDS),
            Event.ts >= lo - DEVICE_CLOCK_AHEAD_SEC, _may_start_in(lo, hi)))
        seeds = [e.as_dict() for e in found]
    keys = sorted({k for r in seeds if lo <= reported_start(r) < hi
                   for k in (r.get("related_event"), r.get("parent_event")) if k})
    return await linked_rows(store, device_id, keys)

async def error_rows(store: EventStore, device_id: int, before: tuple[float, int] | None,
                     limit: int) -> list[dict[str, Any]]:
    """Error-kind events of one device, newest first, older than `before`.

    `before` is the `(ts, id)` of the last row of the previous page, so a
    caller can page back through the whole history without skipping rows
    that share a timestamp.
    """
    stmt = select(Event).where(Event.device_id == device_id,
                               Event.event_kind == codes.KIND_ERROR)
    if before is not None:
        ts, row_id = before
        stmt = stmt.where(or_(Event.ts < ts, and_(Event.ts == ts, Event.id < row_id)))
    stmt = stmt.order_by(Event.ts.desc(), Event.id.desc()).limit(limit)
    async with store._read() as session:
        return [e.as_dict() for e in await session.scalars(stmt)]