"""The cat-ID job's side of the panel: its work list, the labels it trains on,
and the answers it posts back (`decision:job-on-windows-posts-to-panel`).

The job runs on this PC and reaches the panel directly; none of these routes
is meant for the Tailscale gateway (`requirement:footage-stays-home`).

**Input revision** (`decision:input-revision`). Each episode of a visit gets
one source, chosen with the Timeline's own media slots
(`web/api/media.py::_session_media_urls`): the full recording, else the
highlight clip, else the snapshot. An episode is `pending` while its full
recording is still being saved (`video_pending`), `unavailable` when none of
the three is on disk, and `ready` otherwise. A visit is `ready` when no
episode is pending and at least one is ready, `unavailable` when every
episode is, and `pending` otherwise. Its input revision hashes each ready
episode's media path, size and modification time, so a recording that
arrives later or is re-stitched in place under the same name
(`media/stitch.py`) gives the visit a new revision. A visit that is not ready
has no input revision.

**Answers** (`decision:job-recovery`). One row per device and visit, insert
or replace. An answer is refused with 409, keeping the stored row, unless
its `input_rev` is the visit's current one and its `model_seq` is not older
than the stored answer's.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import time
from typing import Any

from aiohttp import web

from petkit_local.fountain_app import db
from petkit_local.fountain_app.api import (
    _device, _device_summary, _recheck_alerts, _store, model_fields,
)
from petkit_local.fountain_app.models import ANSWER_NOT_SURE
from petkit_local.fountain_app.state import state_of
from petkit_local.fountain_app.visits import build_visits
from petkit_local.utils.paths import UnsafePathError, safe_join
from petkit_local.web.api._common import _json_body, _pets_by_id, _refuse
from petkit_local.web.api.media import _session_media_urls

READY = "ready"
PENDING = "pending"
UNAVAILABLE = "unavailable"
SOURCE_FULL = "full"
SOURCE_HIGHLIGHT = "highlight"
SOURCE_SNAPSHOT = "snapshot"


# --- readiness and input revision ---------------------------------------------


def _file_stat(media_root: str, rel: str | None) -> os.stat_result | None:
    """The stat of a media-root-relative file, or None if it is not a file there."""
    if not media_root or not rel:
        return None
    try:
        st = os.stat(safe_join(media_root, rel))
    except (UnsafePathError, OSError):
        return None
    return st if stat.S_ISREG(st.st_mode) else None


def episode_source(media: list[dict[str, Any]], media_root: str,
                   now: float) -> dict[str, Any]:
    """`{state, source, path, size, mtime_ns}` of one episode.

    A source whose row says ready but whose file is gone is passed over for
    the next one: the job could not read it.
    """
    slots = _session_media_urls(media, media_root, now)
    if slots["video_pending"]:
        return {"state": PENDING, "source": None, "path": None}
    for source, rel in ((SOURCE_FULL, slots["playback_url"]),
                        (SOURCE_HIGHLIGHT, slots["highlight_url"]),
                        (SOURCE_SNAPSHOT, slots["poster_url"])):
        st = _file_stat(media_root, rel)
        if st is not None:
            return {"state": READY, "source": source, "path": rel,
                    "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    return {"state": UNAVAILABLE, "source": None, "path": None}


def visit_input(visit: dict[str, Any], media_root: str, now: float) -> dict[str, Any]:
    """`{readiness, input_rev, episodes}` of one `build_visits` visit.

    Blocking (one `stat` per episode); call it off the event loop.
    """
    episodes = [{"kind": e["kind"], "related_event": e["related_event"],
                 "start_ts": e["start_ts"], **episode_source(e["media"], media_root, now)}
                for e in visit["episodes"]]
    states = {e["state"] for e in episodes}
    if PENDING in states:
        readiness = PENDING
    elif READY in states:
        readiness = READY
    else:
        readiness = UNAVAILABLE
    input_rev = None
    if readiness == READY:
        parts = [[e["path"], e["size"], e["mtime_ns"]] for e in episodes
                 if e["state"] == READY]
        input_rev = hashlib.sha256(
            json.dumps(parts, separators=(",", ":")).encode()).hexdigest()
    return {
        "readiness": readiness, "input_rev": input_rev,
        "episodes": [{k: e[k] for k in ("kind", "related_event", "start_ts", "state",
                                        "source", "path")} for e in episodes],
    }


# --- handlers -------------------------------------------------------------------


async def api_fountain_model_work(request: web.Request) -> web.Response:
    """Every visit of one fountain still in the event store, for the cat-ID job.

    `GET /api/fountain/model/work?device=` (device defaults to the first
    W7H). Any day, not one local day: a visit that starts at 23:58 and whose
    recording is saved after midnight is still listed. Answers `{device,
    generated_at, visits}`, newest first; each visit carries `visit_key`,
    `start_ts`, `readiness`, `input_rev`, `episodes` (`kind`,
    `related_event`, `start_ts`, `state`, `source`, `path` relative to the
    media root), `label`, and `model_guess`: the stored answer (`label`,
    `confidence`, `crops`, `model_seq`, `input_rev`, `scored_at`) or null.
    """
    store = _store(request)
    dev = _device(request.app, request.query.get("device"))
    await state_of(request.app).tables.ensure(store)
    rows = await db.all_visit_rows(store, dev.petkit_id)
    labels = await db.labels_for(store, dev.petkit_id)
    guesses = await db.guesses_for(store, dev.petkit_id)
    media_root = request.app["cfg"].get("media_root", "")
    now = time.time()

    def build() -> list[dict[str, Any]]:
        return [{"visit_key": v["visit_key"], "start_ts": v["start_ts"],
                 **visit_input(v, media_root, now),
                 "label": labels.get(v["visit_key"]),
                 "model_guess": guesses.get(v["visit_key"])}
                for v in build_visits(rows)]

    visits = await asyncio.to_thread(build)
    return web.json_response({"device": _device_summary(dev), "generated_at": now,
                              "visits": visits})


async def api_fountain_labels(request: web.Request) -> web.Response:
    """Every label of one fountain, oldest first, for the cat-ID job.

    `GET /api/fountain/labels?device=`. Includes labels whose visit's events
    were pruned. Answers `{device, labels: [{visit_key, label, labeled_at}]}`.
    """
    store = _store(request)
    dev = _device(request.app, request.query.get("device"))
    await state_of(request.app).tables.ensure(store)
    return web.json_response({"device": _device_summary(dev),
                              "labels": await db.all_labels(store, dev.petkit_id)})


def _whole(value: Any) -> int | None:
    """A JSON integer of at least zero, or None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 \
        else None


def _answer(body: dict[str, Any], pets: dict[int, Any]) -> dict[str, Any]:
    """The validated answer of a POST body.

    Raises:
        web.HTTPBadRequest: a field is missing or out of range, or the label
            is neither a registered pet's id nor `not_sure`.
    """
    raw = body.get("label")
    label = str(raw).strip() if isinstance(raw, (str, int)) and not isinstance(raw, bool) \
        else ""
    if label.isdigit():
        if int(label) not in pets:
            raise _refuse(web.HTTPBadRequest, "unknown pet")
        label = str(int(label))
    elif label != ANSWER_NOT_SURE:
        raise _refuse(web.HTTPBadRequest, "label must be a pet id or 'not_sure'")
    confidence = body.get("confidence")
    if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1):
        raise _refuse(web.HTTPBadRequest, "confidence must be a number from 0 to 1")
    crops = _whole(body.get("crops"))
    if crops is None:
        raise _refuse(web.HTTPBadRequest, "crops must be a whole number")
    model_seq = _whole(body.get("model_seq"))
    if model_seq is None:
        raise _refuse(web.HTTPBadRequest, "model_seq must be a whole number")
    input_rev = body.get("input_rev")
    if not isinstance(input_rev, str) or not input_rev:
        raise _refuse(web.HTTPBadRequest, "input_rev must be a non-empty string")
    return {"label": label, "confidence": float(confidence), "crops": crops,
            "model_seq": model_seq, "input_rev": input_rev}


async def api_fountain_model_guess(request: web.Request) -> web.Response:
    """Store or remove the cat-ID model's answer for one visit.

    `POST /api/fountain/visits/{visit_key}/model-guess` with `{"device",
    "label", "confidence", "crops", "model_seq", "input_rev"}`; `device`
    defaults to the first W7H, `label` is a registered pet's id or
    `"not_sure"`. Answers 200 `{ok, visit_key, stored, ...model fields}`
        (`stored` is the saved row, the model fields as on a visit); 409 `{error, reason, input_rev, readiness, stored}` when
    `reason` is `stale_input` (the visit's footage is not the answer's, or
    not ready) or `older_model` (a newer model's answer is stored); 404 for
    a visit that is not in the store. `DELETE` (device in the query string
    or body) removes the answer. Either change re-checks the alerts.
    """
    store = _store(request)
    visit_key = request.match_info["visit_key"]
    body = await _json_body(request) if request.can_read_body else {}
    dev = _device(request.app, request.query.get("device") or body.get("device"))
    await state_of(request.app).tables.ensure(store)

    if request.method == "DELETE":
        removed = await db.delete_guess(store, dev.petkit_id, visit_key)
        await _recheck_alerts(request.app)
        return web.json_response({"ok": True, "removed": removed, "visit_key": visit_key})

    pets = await _pets_by_id(request)
    answer = _answer(body, pets)
    visit = await db.visit_by_key(store, dev.petkit_id, visit_key)
    if visit is None:
        raise _refuse(web.HTTPNotFound, "unknown visit")
    current = await asyncio.to_thread(visit_input, visit, request.app["cfg"].get(
        "media_root", ""), time.time())

    def conflict(reason: str, error: str, stored: dict[str, Any] | None) -> web.Response:
        return web.json_response({"error": error, "reason": reason,
                                  "input_rev": current["input_rev"],
                                  "readiness": current["readiness"], "stored": stored},
                                 status=409)

    if answer["input_rev"] != current["input_rev"]:
        stored = (await db.guesses_for(store, dev.petkit_id, [visit_key])).get(visit_key)
        return conflict("stale_input", "input revision is not current", stored)
    saved, row = await db.put_guess(store, dev.petkit_id, visit_key, answer)
    if not saved:
        return conflict("older_model", "a newer model's answer is stored", row)
    await _recheck_alerts(request.app)
    return web.json_response({"ok": True, "visit_key": visit_key, "stored": row,
                              **model_fields(row, pets)})
