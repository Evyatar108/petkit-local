"""Fountain alerts: a cat that has not drunk in N hours, and the W7H tanks.

A background task runs `check` every `CHECK_INTERVAL_SEC`. Each alert is
raised once, as a `fountain_alerts` row and a `fountain_alert` hub event (the
Log tab), and cleared the same way when its condition ends. The Fountain tab
shows the active ones as a banner.

* **No drink**: for each registered pet, the newest visit attributed to it
  (label first, else the device's guess) that has a drinking episode is older
  than the threshold (`no_drink_hours`, default 12). Not judged until the
  store holds fountain events from before the threshold window, so a fresh
  install or a new fountain does not alert on every cat at once.
* **Tanks**: the latest W7H `error_start` naming `tankCL` (clean tank low) or
  `tankDF` (waste tank full) with no later `error_over` for it
  (`claim:tank-alerts-from-error-events`), however far back it lies. A tank
  alert is cleared only by a reported clear, never because its start fell
  out of the rows read. The raw hall sensors are not read.

The threshold is kept in `{data_dir}/fountain_app.json`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from aiohttp import web

from petkit_local.events import codes
from petkit_local.fountain_app import db
from petkit_local.fountain_app.api import _store, fountains, visits_between
from petkit_local.fountain_app.state import state_of
from petkit_local.fountain_app.stats import attributed
from petkit_local.utils.coerce import to_float
from petkit_local.utils.jsonio import atomic_write_json, read_json
from petkit_local.web.api._common import _json_body, _refuse

log = logging.getLogger(__name__)

CHECK_INTERVAL_SEC = 60
DEFAULT_NO_DRINK_HOURS = 12.0
NO_DRINK_HOURS_RANGE = (1.0, 168.0)
#: How far back the "last drank" time in a new no-drink alert is looked for.
LAST_DRINK_LOOKBACK_SEC = 7 * 86400
#: How long a cleared alert stays in the `recent` list.
RECENT_SEC = 7 * 86400
#: Error rows read per page, and at most per fountain and check, while
#: looking back for each tank flag's latest start or clear.
TANK_SCAN_PAGE = 500
TANK_SCAN_MAX = 20000
TANK_FLAGS = ("tankCL", "tankDF")
SETTINGS_FILE = "fountain_app.json"
HUB_KIND = "fountain_alert"
KIND_NO_DRINK = "no_drink"
KIND_TANK = "tank"


# --- settings ---------------------------------------------------------------


def _settings_path(data_dir: str) -> str:
    return os.path.join(data_dir, SETTINGS_FILE)


def load_settings(data_dir: str | None) -> dict[str, float]:
    """The alert settings, defaults filled in; defaults alone without a data dir."""
    raw = read_json(_settings_path(data_dir), {}) if data_dir else {}
    hours = to_float(raw.get("no_drink_hours") if isinstance(raw, dict) else None, None)
    low, high = NO_DRINK_HOURS_RANGE
    if hours is None or not low <= hours <= high:
        hours = DEFAULT_NO_DRINK_HOURS
    return {"no_drink_hours": hours}


def settings_of(app: web.Application) -> dict[str, float]:
    """The settings, read from disk once per process and then kept in memory."""
    state = state_of(app)
    if state.alert_settings is None:
        state.alert_settings = load_settings(app["cfg"].get("data_dir"))
    return state.alert_settings


# --- conditions -------------------------------------------------------------


def _flag_names(row: dict[str, Any]) -> list[str]:
    """The fault names an error row's content carries, in any of its shapes.

    Same shapes as `mqtt/bridge.py::_error_text`: one name, a comma list, a
    list, or a `{name: truthy}` object, under `err` or `errorMsg`.
    """
    try:
        content = json.loads(row.get("content_json") or "{}")
    except ValueError:
        return []
    if not isinstance(content, dict):
        return []
    err = content.get("err", content.get("errorMsg"))
    if isinstance(err, dict):
        return [str(k) for k, v in err.items() if v]
    if isinstance(err, (list, tuple)):
        return [str(v) for v in err if v]
    if err is None:
        return []
    return [part.strip() for part in str(err).split(",") if part.strip()]


def _decide(row: dict[str, Any], decided: dict[str, dict[str, Any] | None]) -> None:
    """Record what `row` says about each tank flag not yet decided.

    Rows are fed newest first, so the first transition met for a flag is its
    latest: an `error_start` naming it opens it (the row is kept), an
    `error_over` naming it, or naming nothing at all, clears it (None).
    """
    code = codes.lookup(row.get("event_type"), row.get("device_type"))
    role = code.role if code else None
    if role not in (codes.ROLE_ERROR_START, codes.ROLE_ERROR_OVER):
        return
    names = _flag_names(row)
    for flag in TANK_FLAGS:
        if flag in decided:
            continue
        if role == codes.ROLE_ERROR_START and flag in names:
            decided[flag] = row
        elif role == codes.ROLE_ERROR_OVER and (not names or flag in names):
            decided[flag] = None


def tank_subject(device_id: int, flag: str) -> str:
    """The `fountain_alerts.subject` of one fountain's tank alert."""
    return f"{device_id}:{flag}"


def tank_transitions(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    """The latest transition of each tank flag among `rows` (any order).

    `flag -> error_start row` if open, `flag -> None` if cleared; a flag the
    rows say nothing about is absent, which means "unknown", never "clear".
    """
    decided: dict[str, dict[str, Any] | None] = {}
    for row in sorted(rows, key=lambda r: (r.get("ts") or 0, r.get("id") or 0), reverse=True):
        _decide(row, decided)
        if len(decided) == len(TANK_FLAGS):
            break
    return decided


async def _tank_alerts(store, device_id: int
                       ) -> tuple[dict[str, tuple[str, dict[str, Any]]], set[str]]:
    """`(wanted, unknown)`: the open tank alerts, and the subjects to leave as they are.

    Pages back through the device's error history until every tank flag has
    met its latest transition, however many unrelated faults came after it.
    A flag still undecided when the history (or `TANK_SCAN_MAX`) runs out is
    unknown: an alert already raised for it stays raised.
    """
    decided: dict[str, dict[str, Any] | None] = {}
    before: tuple[float, int] | None = None
    read = 0
    while len(decided) < len(TANK_FLAGS) and read < TANK_SCAN_MAX:
        page = await db.error_rows(store, device_id, before, TANK_SCAN_PAGE)
        for row in page:
            _decide(row, decided)
            if len(decided) == len(TANK_FLAGS):
                break
        read += len(page)
        if len(page) < TANK_SCAN_PAGE:
            break
        before = (page[-1]["ts"], page[-1]["id"])
    wanted: dict[str, tuple[str, dict[str, Any]]] = {}
    for flag, row in decided.items():
        if row is None:
            continue
        label = codes.FOUNTAIN_W7H_ERROR_FLAGS.get(flag, flag)
        wanted[tank_subject(device_id, flag)] = (KIND_TANK, {
            "device_id": device_id, "flag": flag, "since_ts": row.get("ts"),
            "message": f"{label} (fountain #{device_id})"})
    unknown = {tank_subject(device_id, flag) for flag in TANK_FLAGS if flag not in decided}
    return wanted, unknown

async def _attributed_drinkers(store, device_ids: list[int], start: float,
                               end: float) -> list[tuple[float, str]]:
    """`(visit start, attributed key)` of every drinking visit, newest first."""
    found: list[tuple[float, str]] = []
    for did in device_ids:
        visits = await visits_between(store, did, start, end)
        labels = await db.labels_for(store, did, [v["visit_key"] for v in visits])
        found += [(v["start_ts"], attributed(v, labels.get(v["visit_key"])))
                  for v in visits if v["has_drinking"]]
    return sorted(found, reverse=True)


async def _has_history_before(store, device_ids: list[int], ts: float) -> bool:
    for did in device_ids:
        if await store.query_timeline(device_id=did, end_ts=ts, limit=1):
            return True
    return False


async def _no_drink_alerts(store, device_ids: list[int], pets: dict[int, str | None],
                           hours: float, now: float,
                           active: set[str]) -> dict[str, tuple[str, dict[str, Any]]]:
    since = now - hours * 3600
    if not pets or not device_ids or not await _has_history_before(store, device_ids, since):
        return {}
    drank = {key for _, key in await _attributed_drinkers(store, device_ids, since, now + 1)}
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    older: list[tuple[float, str]] | None = None
    for pid, name in sorted(pets.items()):
        if str(pid) in drank:
            continue
        subject = f"pet:{pid}"
        last = None
        if subject not in active:
            # Only for a new alert: an active one keeps the detail it was raised with.
            if older is None:
                older = await _attributed_drinkers(
                    store, device_ids, since - LAST_DRINK_LOOKBACK_SEC, since)
            last = next((ts for ts, key in older if key == str(pid)), None)
        label = name or f"pet {pid}"
        out[subject] = (KIND_NO_DRINK, {
            "pet_id": pid, "pet_name": name, "hours": hours, "last_drink_ts": last,
            "message": f"{label} has not drunk from the fountain in {hours:g} hours"})
    return out


async def check(store, device_ids: list[int], pets: dict[int, str | None], hub,
                no_drink_hours: float, now: float | None = None) -> dict[str, list]:
    """Raise and clear alerts so the table matches the conditions at `now`.

    Args:
        device_ids: The W7H fountains to read.
        pets: Pet id to name, for every registered pet.
        hub: The panel's `EventHub`, or None to skip the Log tab events.

    Returns:
        `{raised: [alert], cleared: [alert]}`, each alert as `db` returns it.
    """
    now = time.time() if now is None else now
    active = {a["subject"]: a for a in await db.active_alerts(store)}
    wanted: dict[str, tuple[str, dict[str, Any]]] = {}
    unknown: set[str] = set()
    for did in device_ids:
        tank_wanted, tank_unknown = await _tank_alerts(store, did)
        wanted.update(tank_wanted)
        unknown |= tank_unknown
    wanted.update(await _no_drink_alerts(store, device_ids, pets, no_drink_hours, now,
                                         set(active)))

    raised, cleared = [], []
    for subject, (kind, detail) in wanted.items():
        if subject in active:
            continue
        alert = await db.raise_alert(store, kind, subject, detail, now)
        raised.append(alert)
        if hub is not None:
            hub.publish(HUB_KIND, detail.get("device_id"), detail["message"], alert)
    for subject, alert in active.items():
        if subject in wanted or subject in unknown:
            continue
        await db.clear_alert(store, alert["id"], now)
        alert = {**alert, "cleared_at": now}
        cleared.append(alert)
        if hub is not None:
            hub.publish(HUB_KIND, alert["detail"].get("device_id"),
                        "Cleared: " + alert["detail"].get("message", subject), alert)
    return {"raised": raised, "cleared": cleared}


async def check_app(app: web.Application, now: float | None = None) -> dict[str, list] | None:
    """`check` with the panel's own collaborators; None without an event store.

    Serialized by a lock, so the periodic task and a settings change never
    raise the same alert twice.
    """
    store = app.get("event_store")
    if store is None:
        return None
    state = state_of(app)
    async with state.alert_lock:
        await state.tables.ensure(store)
        registry = app.get("pet_registry")
        pets = await registry.all() if registry is not None else []
        return await check(store, [d.petkit_id for d in fountains(app)],
                           {p["id"]: p.get("name") for p in pets}, app.get("hub"),
                           settings_of(app)["no_drink_hours"], now)


async def alert_loop(app: web.Application) -> None:
    """The periodic check. Waits one interval first, so start-up stays quick."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SEC)
        try:
            await check_app(app)
        except Exception:
            log.exception("fountain app: alert check failed")


# --- handlers ---------------------------------------------------------------


async def api_fountain_alerts(request: web.Request) -> web.Response:
    """`GET /api/fountain/alerts`: `{active, recent, settings}`.

    `recent` holds the alerts cleared in the last `RECENT_SEC`.
    """
    store = _store(request)
    await state_of(request.app).tables.ensure(store)
    return web.json_response({
        "active": await db.active_alerts(store),
        "recent": await db.cleared_alerts(store, time.time() - RECENT_SEC),
        "settings": settings_of(request.app),
    })


async def api_fountain_alert_settings(request: web.Request) -> web.Response:
    """`GET` or `POST /api/fountain/alert-settings` (`{"no_drink_hours": 12}`).

    A change is saved, then checked at once, so the banner follows it without
    waiting for the next periodic check.
    """
    if request.method == "GET":
        return web.json_response(settings_of(request.app))
    body = await _json_body(request)
    raw = body.get("no_drink_hours")
    hours = None if isinstance(raw, bool) else to_float(raw, None)
    low, high = NO_DRINK_HOURS_RANGE
    if hours is None or not low <= hours <= high:
        raise _refuse(web.HTTPBadRequest,
                      f"no_drink_hours must be a number from {low:g} to {high:g}")
    data_dir = request.app["cfg"].get("data_dir")
    if not data_dir:
        raise _refuse(web.HTTPBadRequest, "no data directory configured")
    settings = {"no_drink_hours": hours}
    await asyncio.to_thread(atomic_write_json, _settings_path(data_dir), settings)
    state_of(request.app).alert_settings = settings
    result = await check_app(request.app)
    return web.json_response({"ok": True, "settings": settings,
                              "raised": len(result["raised"]) if result else 0,
                              "cleared": len(result["cleared"]) if result else 0})
