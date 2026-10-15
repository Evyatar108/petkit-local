"""Reads and writes of the fountain app's own tables, on the event store.

Our tables share `petkit.db` with upstream's, so they go through the store's
own engine and its single write lock (`EventStore._read`/`_write`): SQLite
allows one writer, and the store's guarantee that no in-process coroutine meets
SQLITE_BUSY only holds if every writer takes that lock. Reaching into those
private members is the price of not editing `events/store.py`; it is confined
to this module.
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from petkit_local.fountain_app.models import FountainBase, VisitLabel

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
