"""Fountain alerts: a cat that has not drunk in N hours, and the W7H tanks.

A background task runs `check` every `CHECK_INTERVAL_SEC`. Each alert is
raised once, as a `fountain_alerts` row and a `fountain_alert` hub event (the
Log tab), and cleared the same way when its condition ends. The Fountain tab
shows the active ones as a banner.

* **No drink**: for each registered pet, the newest visit attributed to it
  (`stats.attributed`: label, else a confident model answer while
  `use_model_answers` is on, else the device's guess) that has a drinking
  episode is older than the threshold (`no_drink_hours`, default 12). Not
  judged until the store holds fountain events from before the threshold
  window, so a fresh install or a new fountain does not alert on every cat
  at once.
  With recent unidentified drinking and model attribution enabled, an urgent
  request can defer a new warning for at most ten minutes. Its separate,
  current emergency evidence may clear only the warning; unvalidated evidence
  is displayed as such and never changes normal guesses or graph attribution.
* **Tanks**: the latest W7H `error_start` naming `tankCL` (clean tank low) or
  `tankDF` (waste tank full) with no later `error_over` for it
  (`claim:tank-alerts-from-error-events`), however far back it lies. A tank
  alert is cleared only by a reported clear, never because its start fell
  out of the rows read. The raw hall sensors are not read.

The settings are kept in `{data_dir}/fountain_app.json` (see `settings.py`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from aiohttp import web

from petkit_local.events import codes
from petkit_local.fountain_app import db
from petkit_local.fountain_app import pre_alert
from petkit_local.fountain_app.api import _store, fountains, visits_between
from petkit_local.fountain_app.settings import SettingsError, settings_of, update_settings
from petkit_local.fountain_app.state import state_of
from petkit_local.fountain_app.stats import attributed
from petkit_local.web.api._common import _json_body, _refuse

log = logging.getLogger(__name__)

CHECK_INTERVAL_SEC = 60
#: How far back the "last drank" time in a new no-drink alert is looked for.
LAST_DRINK_LOOKBACK_SEC = 7 * 86400
#: How long a cleared alert stays in the `recent` list.
RECENT_SEC = 7 * 86400
#: Error rows read per page, and at most per fountain and check, while
#: looking back for each tank flag's latest start or clear.
TANK_SCAN_PAGE = 500
TANK_SCAN_MAX = 20000
TANK_FLAGS = ("tankCL", "tankDF")
HUB_KIND = "fountain_alert"
KIND_NO_DRINK = "no_drink"
KIND_TANK = "tank"


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
                               end: float, use_model: bool) -> list[tuple[float, str]]:
    """`(visit start, attributed key)` of every drinking visit, newest first."""
    found: list[tuple[float, str]] = []
    for did in device_ids:
        visits = await visits_between(store, did, start, end)
        keys = [v["visit_key"] for v in visits]
        labels = await db.labels_for(store, did, keys)
        guesses = await db.guesses_for(store, did, keys)
        found += [(v["start_ts"], attributed(v, labels.get(v["visit_key"]),
                                             guesses.get(v["visit_key"]), use_model))
                  for v in visits if v["has_drinking"]]
    return sorted(found, reverse=True)


async def _drinking_records(store, device_ids: list[int], start: float,
                            end: float, use_model: bool) -> list[dict]:
    found = []
    for did in device_ids:
        visits = await visits_between(store, did, start, end)
        keys = [v["visit_key"] for v in visits]
        labels = await db.labels_for(store, did, keys)
        guesses = await db.guesses_for(store, did, keys)
        for visit in visits:
            if visit["has_drinking"]:
                label, guess = labels.get(visit["visit_key"]), guesses.get(visit["visit_key"])
                found.append({
                    "device_id": did, "visit": visit, "label": label, "guess": guess,
                    "attributed": attributed(visit, label, guess, use_model),
                })
    return sorted(found, key=lambda r: r["visit"]["start_ts"], reverse=True)


async def _has_history_before(store, device_ids: list[int], ts: float) -> bool:
    for did in device_ids:
        if await store.query_timeline(device_id=did, end_ts=ts, limit=1):
            return True
    return False


async def _no_drink_alerts(store, device_ids: list[int], pets: dict[int, str | None],
                           hours: float, now: float, active: set[str],
                           use_model: bool, *, media_root: str = "",
                           waiting: set[str] | None = None, wake=None
                           ) -> dict[str, tuple[str, dict[str, Any]]]:
    since = now - hours * 3600
    if not pets or not device_ids or not await _has_history_before(store, device_ids, since):
        return {}
    records = await _drinking_records(
        store, device_ids, since - LAST_DRINK_LOOKBACK_SEC, now + 1, use_model)
    recent = [r for r in records if r["visit"]["start_ts"] >= since]
    drank = {r["attributed"] for r in recent}
    emergency = await pre_alert.current_evidence(store, records, media_root, now) if use_model else {}
    unresolved = [r for r in recent if r["attributed"] == "unknown"]
    candidates = [r for r in unresolved if r["label"] is None]
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for pid, name in sorted(pets.items()):
        if str(pid) in drank:
            continue
        subject = f"pet:{pid}"
        label = name or f"pet {pid}"
        evidence = emergency.get(pid)
        if evidence and evidence["start_ts"] >= since:
            if not evidence["validation_passed"]:
                out[subject] = (pre_alert.KIND_UNVALIDATED, {
                    "pet_id": pid, "pet_name": name, "hours": hours,
                    "last_drink_ts": evidence["start_ts"],
                    "unvalidated": True, "visit_key": evidence["visit_key"],
                    "message": f"An unvalidated model identified a recent drink as {label}. "
                               "The no-drink warning was cleared. Review the drink's label.",
                })
            continue
        last = next((r["visit"]["start_ts"] for r in records
                     if r["attributed"] == str(pid)), None)
        if evidence:
            last = max(last or 0, evidence["start_ts"])
        if use_model and candidates:
            scope = {
                "start_ts": since, "device_ids": device_ids,
                "visits": [{"device_id": r["device_id"], "visit_key": r["visit"]["visit_key"]}
                           for r in candidates],
            }
            request = await db.ensure_pre_alert(
                store, pre_alert.episode_key(pid, hours, last), pid, scope, now,
                pre_alert.WAIT_SEC)
            if request["result"] is None and now < request["deadline"]:
                if waiting is not None:
                    waiting.add(subject)
                if wake is not None and request["created"]:
                    wake.set()
                continue
        uncertain = use_model and bool(unresolved)
        message = (
            f"No identified drink for {label} in {hours:g} hours. Recent drinks remain "
            "unidentified; classification could not confirm them."
            if uncertain else f"{label} has not drunk from the fountain in {hours:g} hours")
        out[subject] = (pre_alert.KIND_UNCONFIRMED if uncertain else KIND_NO_DRINK, {
            "pet_id": pid, "pet_name": name, "hours": hours, "last_drink_ts": last,
            "message": message})
    return out


async def check(store, device_ids: list[int], pets: dict[int, str | None], hub,
                no_drink_hours: float, now: float | None = None,
                use_model_answers: bool = True, *, media_root: str = "", wake=None
                ) -> dict[str, list]:
    """Raise and clear alerts so the table matches the conditions at `now`.

    Args:
        device_ids: The W7H fountains to read.
        pets: Pet id to name, for every registered pet.
        hub: The panel's `EventHub`, or None to skip the Log tab events.
        use_model_answers: Whether confident model answers attribute visits.

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
                                         set(active), use_model_answers,
                                         media_root=media_root, waiting=unknown, wake=wake))

    raised, cleared = [], []
    for subject, (kind, detail) in wanted.items():
        if subject in active:
            previous = active[subject]
            if previous["kind"] == kind:
                continue
            await db.clear_alert(store, previous["id"], now)
            previous = {**previous, "cleared_at": now}
            cleared.append(previous)
            if hub is not None:
                hub.publish(HUB_KIND, previous["detail"].get("device_id"),
                            "Cleared: " + previous["detail"].get("message", subject), previous)
        alert = await db.raise_alert(store, kind, subject, detail, now)
        raised.append(alert)
        if hub is not None:
            hub.publish(HUB_KIND, detail.get("device_id"), detail["message"], alert)
    for subject, alert in active.items():
        if subject in wanted or (subject in unknown and alert["kind"] != pre_alert.KIND_UNVALIDATED):
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
        settings = settings_of(app)
        return await check(store, [d.petkit_id for d in fountains(app)],
                           {p["id"]: p.get("name") for p in pets}, app.get("hub"),
                           settings["no_drink_hours"], now, settings["use_model_answers"],
                           media_root=app["cfg"].get("media_root", ""), wake=state.alert_wake)


async def alert_loop(app: web.Application) -> None:
    """Periodic reconciliation plus exact request deadlines and creation wakeups."""
    state = state_of(app)
    while True:
        state.alert_wake.clear()
        try:
            await check_app(app)
        except Exception:
            log.exception("fountain app: alert check failed")
        delay = CHECK_INTERVAL_SEC
        store = app.get("event_store")
        if store is not None:
            try:
                deadline = await pre_alert.pending_deadline(store, time.time())
                if deadline is not None:
                    delay = min(delay, max(0, deadline - time.time()))
            except Exception:
                log.exception("fountain app: could not read urgent deadline")
        try:
            await asyncio.wait_for(state.alert_wake.wait(), timeout=delay)
        except TimeoutError:
            pass


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
    """`GET` or `POST /api/fountain/alert-settings`.

    `GET` answers `{"no_drink_hours": 12.0, "use_model_answers": true}`.
    `POST` takes either field or both; a field the body omits keeps its
    stored value. A change is saved, then checked at once, so the banner
    follows it without waiting for the next periodic check.
    """
    if request.method == "GET":
        return web.json_response(settings_of(request.app))
    body = await _json_body(request)
    try:
        settings = await update_settings(request.app, body)
    except SettingsError as e:
        raise _refuse(web.HTTPBadRequest, str(e)) from None
    result = await check_app(request.app)
    return web.json_response({"ok": True, "settings": settings,
                              "raised": len(result["raised"]) if result else 0,
                              "cleared": len(result["cleared"]) if result else 0})
