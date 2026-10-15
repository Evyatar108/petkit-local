"""Request handlers for `/api/fountain/`: visits, their labels, stats, export.

Every endpoint is for a W7H fountain (`codes.FOUNTAIN_NEXT_GEN`); any other
device is refused with 400, an unknown one with 404. Without an event store
every endpoint answers 400 `{"error": "event store not available"}`, like the
Timeline.
"""
from __future__ import annotations

from typing import Any

from aiohttp import web

from petkit_local.events import codes
from petkit_local.fountain_app import db, stats
from petkit_local.fountain_app.export import export_dataset
from petkit_local.fountain_app.models import SPECIAL_LABELS
from petkit_local.fountain_app.state import state_of
from petkit_local.fountain_app.visits import VISIT_WINDOW_SEC, build_visits
from petkit_local.utils.coerce import to_int
from petkit_local.utils.const import device_display_name
from petkit_local.utils.timeutil import local_day_bounds, local_offset_hours
from petkit_local.web.api._common import _json_body, _pet_fields, _pets_by_id, _refuse
from petkit_local.web.api.media import _session_media_urls

#: Rows one visit query may read. A busy day is a few dozen visits of a few
#: rows each; the cap only stops a runaway device from stalling the panel.
ROW_LIMIT = 20000


def is_fountain(device: Any) -> bool:
    """True if a registry device is a W7H fountain."""
    return bool(device) and (device.device_type or "").lower() in codes.FOUNTAIN_NEXT_GEN


def fountains(app: web.Application) -> list[Any]:
    """Every registered W7H fountain, lowest id first."""
    return sorted((d for d in app["registry"].all() if is_fountain(d)),
                  key=lambda d: d.petkit_id)


def _store(request: web.Request):
    store = request.app.get("event_store")
    if store is None:
        raise _refuse(web.HTTPBadRequest, "event store not available")
    return store


def _device(app: web.Application, value: Any, *, default_first: bool = True):
    """The W7H fountain `value` names, or the first one when it is absent.

    Raises:
        web.HTTPBadRequest: the id is not a number, the device is not a W7H,
            or no id was given and there is no W7H to default to.
        web.HTTPNotFound: no device has that id.
    """
    if value in (None, ""):
        if default_first:
            found = fountains(app)
            if found:
                return found[0]
        raise _refuse(web.HTTPBadRequest, "no fountain device")
    did = to_int(value, None)
    if did is None:
        raise _refuse(web.HTTPBadRequest, "device must be a number")
    dev = app["registry"].get(did)
    if dev is None:
        raise _refuse(web.HTTPNotFound, "unknown device")
    if not is_fountain(dev):
        raise _refuse(web.HTTPBadRequest, "not a W7H fountain")
    return dev


async def visits_between(store, device_id: int, start_ts: float, end_ts: float,
                         window_sec: float = VISIT_WINDOW_SEC) -> list[dict[str, Any]]:
    """Visits of one fountain that START in `[start_ts, end_ts)`, newest first.

    The read is widened by `window_sec` on both sides so a visit whose two
    halves straddle a boundary is built whole and then filed by its start.
    """
    rows: list[dict[str, Any]] = []
    for kind in (codes.KIND_PET, codes.KIND_DRINKING):
        rows += await store.query_timeline(
            device_id=device_id, start_ts=start_ts - window_sec,
            end_ts=end_ts + window_sec, event_kind=kind, limit=ROW_LIMIT)
    return [v for v in build_visits(rows, window_sec) if start_ts <= v["start_ts"] < end_ts]


def _device_summary(dev: Any) -> dict[str, Any]:
    return {"id": dev.petkit_id, "device_type": dev.device_type,
            "name": device_display_name(dev.device_type)}


def _pet_list(pets: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"id": pid, **_pet_fields(pets, pid)} for pid in sorted(pets)]


def label_fields(label: str | None, pets: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """`{label, label_pet_id, label_name}` for one visit."""
    pet_id = int(label) if label and label.isdigit() else None
    if pet_id is not None:
        name = _pet_fields(pets, pet_id)["pet_name"] or f"pet {pet_id}"
    else:
        name = {None: None, "unknown": "Unknown", "not_a_cat": "Not a cat"}.get(label, label)
    return {"label": label, "label_pet_id": pet_id, "label_name": name}


def visit_payload(v: dict[str, Any], label: str | None, pets: dict[int, dict[str, Any]],
                  media_root: str) -> dict[str, Any]:
    """One visit as the panel receives it."""
    guess = _pet_fields(pets, v["device_guess"])
    return {
        **{k: v[k] for k in ("visit_key", "device_id", "start_ts", "appeared_ts",
                             "drink_start_ts", "drink_end_ts", "duration_sec",
                             "drink_time_sec", "device_guess", "has_appearance",
                             "has_drinking", "event_ids")},
        "device_guess_name": guess["pet_name"],
        "device_guess_photo_url": guess["pet_photo_url"],
        **label_fields(label, pets),
        "attributed": stats.attributed(v, label),
        "episodes": [
            {"kind": e["kind"], "related_event": e["related_event"], "start_ts": e["start_ts"],
             "media": _session_media_urls(e["media"], media_root)}
            for e in v["episodes"]
        ],
    }


async def api_fountain_visits(request: web.Request) -> web.Response:
    """One LOCAL day of one fountain's visits, newest first.

    `GET /api/fountain/visits?device=&date=`. `device` defaults to the first
    W7H; `date` defaults to today, and an unparseable one falls back to today,
    as on the Timeline. Answers `{device, fountains, date, tz_offset, pets,
    visits}`; each visit carries its label, its device guess, and one media
    block per episode (an appearance and a drinking episode film separately).
    """
    store = _store(request)
    dev = _device(request.app, request.query.get("device"))
    await state_of(request.app).tables.ensure(store)
    start_ts, end_ts, day_label = local_day_bounds(request.query.get("date", ""))
    found = await visits_between(store, dev.petkit_id, start_ts, end_ts)
    labels = await db.labels_for(store, dev.petkit_id, [v["visit_key"] for v in found])
    pets = await _pets_by_id(request)
    media_root = request.app["cfg"].get("media_root", "")
    return web.json_response({
        "device": _device_summary(dev),
        "fountains": [_device_summary(d) for d in fountains(request.app)],
        "date": day_label,
        "tz_offset": int(round(local_offset_hours(start_ts) * 3600)),
        "pets": _pet_list(pets),
        "visits": [visit_payload(v, labels.get(v["visit_key"]), pets, media_root)
                   for v in found],
    })


async def _visit_exists(store, device_id: int, visit_key: str) -> bool:
    """True if `visit_key` is the session id of an event of this device."""
    row = await store.event_by_related(visit_key)
    return row is not None and row.get("device_id") == device_id


async def api_fountain_label(request: web.Request) -> web.Response:
    """Set or remove one visit's label.

    `POST /api/fountain/visits/{visit_key}/label` with `{"device", "label"}`,
    where `label` is a registered pet's id, `"unknown"` or `"not_a_cat"`.
    `DELETE` takes `device` from the query string (or a JSON body) and removes
    the label. A label is refused for a `visit_key` this device never sent,
    but a label whose events were later pruned is kept and can be removed.
    """
    store = _store(request)
    visit_key = request.match_info["visit_key"]
    body = await _json_body(request) if request.can_read_body else {}
    dev = _device(request.app, request.query.get("device") or body.get("device"),
                  default_first=False)
    await state_of(request.app).tables.ensure(store)

    if request.method == "DELETE":
        removed = await db.delete_label(store, dev.petkit_id, visit_key)
        return web.json_response({"ok": True, "removed": removed, "visit_key": visit_key})

    raw = body.get("label")
    label = str(raw).strip() if raw is not None and not isinstance(raw, bool) else ""
    pets = await _pets_by_id(request)
    if label.isdigit():
        if int(label) not in pets:
            raise _refuse(web.HTTPBadRequest, "unknown pet")
        label = str(int(label))
    elif label not in SPECIAL_LABELS:
        raise _refuse(web.HTTPBadRequest,
                      "label must be a pet id, 'unknown' or 'not_a_cat'")
    if not await _visit_exists(store, dev.petkit_id, visit_key):
        raise _refuse(web.HTTPNotFound, "unknown visit")
    await db.set_label(store, dev.petkit_id, visit_key, label)
    return web.json_response({"ok": True, "visit_key": visit_key,
                              **label_fields(label, pets)})


async def api_fountain_stats(request: web.Request) -> web.Response:
    """Per-day, per-cat visits and drinking time for the last 7 or 30 days.

    `GET /api/fountain/stats?device=&days=7|30` (default 7). Answers
    `{device, days, tz_offset, dates, series, visits, labeled, not_a_cat}`;
    see `stats.aggregate`. Each series is named for its pet.
    """
    store = _store(request)
    dev = _device(request.app, request.query.get("device"))
    days = to_int(request.query.get("days"), 7)
    if days not in stats.ALLOWED_DAYS:
        raise _refuse(web.HTTPBadRequest, "days must be 7 or 30")
    await state_of(request.app).tables.ensure(store)
    spans = stats.day_spans(days)
    found = await visits_between(store, dev.petkit_id, spans[0][1], spans[-1][2])
    labels = await db.labels_for(store, dev.petkit_id, [v["visit_key"] for v in found])
    pets = await _pets_by_id(request)
    result = stats.aggregate(found, labels, spans, sorted(pets))
    for s in result["series"]:
        s.update(label_fields(s["key"], pets))
    return web.json_response({
        "device": _device_summary(dev), "days": days,
        "tz_offset": int(round(local_offset_hours(spans[-1][1]) * 3600)),
        **result,
    })


async def api_fountain_export(request: web.Request) -> web.Response:
    """Write a training dataset from every labeled visit of one fountain.

    `POST /api/fountain/export?device=` (device defaults to the first W7H).
    Writes `{data_dir}/datasets/<timestamp>/` (see `export.py`) and answers
    `{ok, path, visits, images, by_label, missing, no_media, ffmpeg}`. A
    second export while one runs is refused with 409.
    """
    store = _store(request)
    dev = _device(request.app, request.query.get("device"))
    data_dir = request.app["cfg"].get("data_dir")
    if not data_dir:
        raise _refuse(web.HTTPBadRequest, "no data directory configured")
    state = state_of(request.app)
    if state.export_lock.locked():
        raise _refuse(web.HTTPConflict, "an export is already running")
    async with state.export_lock:
        await state.tables.ensure(store)
        pets = await _pets_by_id(request)
        result = await export_dataset(store, dev.petkit_id, data_dir,
                                      {pid: p.get("name") for pid, p in pets.items()})
    return web.json_response({"ok": True, **result})