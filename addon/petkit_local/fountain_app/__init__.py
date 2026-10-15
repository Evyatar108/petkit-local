"""The fountain app: visits, labels and stats for a W7H fountain.

Self-contained on purpose. Upstream reaches this package through one call,
`register(app)` at the end of `web/panel.py::create_panel_app`; everything
else (routes, our own tables, start-up work) is wired from here, so a merge
from upstream touches at most that one line.

Labels live in our `fountain_visit_labels` table, never in `events.pet_id`:
upstream's `bind_pet_ref` and its start-up backfill rewrite that column.
"""
from __future__ import annotations

import logging

from aiohttp import web

from petkit_local.fountain_app.api import (
    api_fountain_label, api_fountain_stats, api_fountain_visits,
)
from petkit_local.fountain_app.state import FOUNTAIN_APP, FountainState, state_of

log = logging.getLogger(__name__)


async def _create_tables(app: web.Application) -> None:
    """Start-up hook: create our tables on the event store's engine."""
    store = app.get("event_store")
    if store is None:
        return
    try:
        await state_of(app).tables.ensure(store)
    except Exception:
        # Not fatal for the panel: every fountain request retries the guard.
        log.exception("fountain app: could not create its tables")


def register(app: web.Application) -> None:
    """Wire the fountain app onto the panel application, before it is frozen."""
    app[FOUNTAIN_APP] = FountainState()
    app.router.add_get("/api/fountain/visits", api_fountain_visits)
    app.router.add_post("/api/fountain/visits/{visit_key}/label", api_fountain_label)
    app.router.add_delete("/api/fountain/visits/{visit_key}/label", api_fountain_label)
    app.router.add_get("/api/fountain/stats", api_fountain_stats)
    app.on_startup.append(_create_tables)
