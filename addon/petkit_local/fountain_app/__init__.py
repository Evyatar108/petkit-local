"""The fountain app for a W7H: visits, labels, stats, dataset export, alerts,
a status summary, and the cat-ID job's work list and answers.

Self-contained on purpose. Upstream reaches this package through one call,
`register(app)` at the end of `web/panel.py::create_panel_app`; everything
else (routes, our own tables, start-up work) is wired from here, so a merge
from upstream touches at most that one line.

Labels live in our `fountain_visit_labels` table, never in `events.pet_id`:
upstream's `bind_pet_ref` and its start-up backfill rewrite that column.
"""
from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from petkit_local.fountain_app.alerts import (
    alert_loop, api_fountain_alert_settings, api_fountain_alerts,
)
from petkit_local.fountain_app.api import (
    api_fountain_export, api_fountain_label, api_fountain_stats, api_fountain_visits,
)
from petkit_local.fountain_app.model_guess import (
    api_fountain_labels, api_fountain_model_guess, api_fountain_model_work,
)
from petkit_local.fountain_app.state import FOUNTAIN_APP, FountainState, state_of
from petkit_local.fountain_app.status import api_fountain_status

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


async def _start_alerts(app: web.Application) -> None:
    """Start-up hook: run the alert check in the background.

    Registered after `_create_tables`, and the loop waits one interval before
    its first check. The task joins the panel's background set, which the
    panel's cleanup cancels and drains.
    """
    if app.get("event_store") is None:
        return
    tasks = app["background_tasks"]
    task = asyncio.get_running_loop().create_task(alert_loop(app), name="fountain-alerts")
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def register(app: web.Application) -> None:
    """Wire the fountain app onto the panel application, before it is frozen."""
    app[FOUNTAIN_APP] = FountainState()
    app.router.add_get("/api/fountain/visits", api_fountain_visits)
    app.router.add_post("/api/fountain/visits/{visit_key}/label", api_fountain_label)
    app.router.add_delete("/api/fountain/visits/{visit_key}/label", api_fountain_label)
    app.router.add_post("/api/fountain/visits/{visit_key}/model-guess",
                        api_fountain_model_guess)
    app.router.add_delete("/api/fountain/visits/{visit_key}/model-guess",
                          api_fountain_model_guess)
    app.router.add_get("/api/fountain/model/work", api_fountain_model_work)
    app.router.add_get("/api/fountain/labels", api_fountain_labels)
    app.router.add_get("/api/fountain/stats", api_fountain_stats)
    app.router.add_post("/api/fountain/export", api_fountain_export)
    app.router.add_get("/api/fountain/alerts", api_fountain_alerts)
    app.router.add_get("/api/fountain/alert-settings", api_fountain_alert_settings)
    app.router.add_post("/api/fountain/alert-settings", api_fountain_alert_settings)
    app.router.add_get("/api/fountain/status", api_fountain_status)
    app.on_startup.append(_create_tables)
    app.on_startup.append(_start_alerts)
