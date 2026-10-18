"""Durable urgent classification, separate from normal guesses and labels.

Only alert evaluation consumes these assessments. Windows never opens SQLite.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from aiohttp import web

from petkit_local.fountain_app import db
from petkit_local.fountain_app.api import _store
from petkit_local.fountain_app.model_guess import READY, _answer, visit_input
from petkit_local.fountain_app.settings import settings_of
from petkit_local.fountain_app.state import state_of
from petkit_local.fountain_app.stats import MODEL_CONFIDENT
from petkit_local.web.api._common import _json_body, _pets_by_id, _refuse

WAIT_SEC = 600
KIND_UNCONFIRMED = "no_drink_unconfirmed"
KIND_UNVALIDATED = "unvalidated_drink"


def episode_key(pet_id: int, hours: float, anchor: float | None) -> str:
    """No nullable unique key: a never-identified pet has a stable none anchor."""
    return json.dumps([pet_id, hours, anchor if anchor is not None else "none"])


def reduce_assessments(checks: list[dict[str, Any]]) -> dict[tuple[int, str, str], dict]:
    """Highest sequence wins globally, not independently for each pet request."""
    found: dict[tuple[int, str, str], dict] = {}
    for check in checks:
        result = check.get("result")
        if result is None:
            continue
        for answer in result["answers"]:
            key = (answer["device_id"], answer["visit_key"], answer["input_rev"])
            candidate = {
                **answer, "model_seq": result["model_seq"],
                "validation_passed": result["validation_passed"],
            }
            old = found.get(key)
            if old is None or candidate["model_seq"] > old["model_seq"]:
                found[key] = candidate
            elif candidate["model_seq"] == old["model_seq"]:
                if (candidate["label"], candidate["confidence"]) != (old["label"], old["confidence"]):
                    found[key] = {**old, "label": "not_sure", "conflict": True}
                elif old.get("conflict"):
                    continue
                else:
                    # Never promote evidence to validated through one inconsistent claim.
                    old["validation_passed"] &= candidate["validation_passed"]
    return found


async def current_evidence(store, records: list[dict], media_root: str,
                           now: float) -> dict[int, dict]:
    assessments = reduce_assessments(await db.pre_alert_checks(store))
    evidence = {}
    for record in records:
        if record["label"] is not None:
            continue
        visit = record["visit"]
        candidates = [
            answer for (did, key, _), answer in assessments.items()
            if did == record["device_id"] and key == visit["visit_key"]
        ]
        if not candidates:
            continue
        current = await asyncio.to_thread(visit_input, visit, media_root, now)
        answer = next((a for a in candidates if a["input_rev"] == current["input_rev"]), None)
        if (answer is None or current["readiness"] != READY
                or not answer["label"].isdigit() or answer["confidence"] < MODEL_CONFIDENT
                or answer["crops"] <= 0
                or answer["model_seq"] < (record["guess"] or {}).get("model_seq", 0)):
            continue
        normal = record["guess"] or {}
        if (normal.get("input_rev") == answer["input_rev"]
                and normal.get("model_seq", -1) >= answer["model_seq"]):
            continue
        pid = int(answer["label"])
        if pid not in evidence or visit["start_ts"] > evidence[pid]["start_ts"]:
            evidence[pid] = {**answer, "start_ts": visit["start_ts"]}
    return evidence


async def pending_deadline(store, now: float) -> float | None:
    pending = [
        row["deadline"] for row in await db.pre_alert_checks(store)
        if row["result"] is None and row["deadline"] > now
    ]
    return min(pending, default=None)


async def api_alert_checks(request: web.Request) -> web.Response:
    store = _store(request)
    state = state_of(request.app)
    await state.tables.ensure(store)
    watermark = await db.model_sequence_watermark(store)
    now = time.time()
    if not settings_of(request.app)["use_model_answers"]:
        return web.json_response({"requests": [], "model_sequence_watermark": watermark})
    pets = await _pets_by_id(request)
    media_root = request.app["cfg"].get("media_root", "")
    rows = []
    for row in await db.pre_alert_checks(store):
        if row["result"] is not None or row["deadline"] <= now or row["pet_id"] not in pets:
            continue
        visits = []
        for scope in row["scope"]["visits"]:
            visit = await db.visit_by_key(store, scope["device_id"], scope["visit_key"])
            labels = await db.labels_for(store, scope["device_id"], [scope["visit_key"]])
            if visit is None or not visit["has_drinking"] or scope["visit_key"] in labels:
                continue
            visits.append({
                **scope, "start_ts": visit["start_ts"],
                **await asyncio.to_thread(visit_input, visit, media_root, now),
            })
        if visits:
            rows.append({
                "id": row["id"], "pet_id": row["pet_id"], "deadline": row["deadline"],
                "created_at": row["created_at"], "visits": visits,
            })
    return web.json_response({"requests": rows, "model_sequence_watermark": watermark})


def parse_result(body: dict, pets: dict) -> dict:
    validation = body.get("validation_passed")
    seq = body.get("model_seq")
    rows = body.get("answers")
    if not isinstance(validation, bool):
        raise _refuse(web.HTTPBadRequest, "validation_passed must be a boolean")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise _refuse(web.HTTPBadRequest, "model_seq must be a nonnegative integer")
    if not isinstance(rows, list) or len(rows) > 500:
        raise _refuse(web.HTTPBadRequest, "answers must be a bounded list")
    answers = []
    keys = set()
    for row in rows:
        if not isinstance(row, dict):
            raise _refuse(web.HTTPBadRequest, "invalid assessment")
        did, key = row.get("device_id"), row.get("visit_key")
        if (not isinstance(did, int) or isinstance(did, bool) or did <= 0
                or not isinstance(key, str) or not key or len(key) > 200):
            raise _refuse(web.HTTPBadRequest, "invalid visit identity")
        answer = _answer({**row, "model_seq": seq}, pets)
        if (did, key) in keys:
            raise _refuse(web.HTTPBadRequest, "duplicate assessment")
        keys.add((did, key))
        answers.append({**answer, "device_id": did, "visit_key": key})
    return {"model_seq": seq, "validation_passed": validation,
            "answers": sorted(answers, key=lambda a: (a["device_id"], a["visit_key"]))}


async def api_alert_result(request: web.Request) -> web.Response:
    store = _store(request)
    state = state_of(request.app)
    await state.tables.ensure(store)
    result = parse_result(await _json_body(request), await _pets_by_id(request))
    media_root = request.app["cfg"].get("media_root", "")
    async with state.alert_lock:
        if not settings_of(request.app)["use_model_answers"]:
            raise _refuse(web.HTTPConflict, "model attribution is disabled")
        rows = await db.pre_alert_checks(store)
        row = next((r for r in rows if r["id"] == request.match_info["request_id"]), None)
        if row is None:
            raise _refuse(web.HTTPNotFound, "unknown alert check")
        # Exact retry is permitted after completion, never a new interpretation.
        if row["result"] == result:
            return web.json_response({"ok": True, "repeated": True})
        now = time.time()
        if row["result"] is not None or now >= row["deadline"]:
            raise _refuse(web.HTTPConflict, "alert check completed or expired")
        scope = {(v["device_id"], v["visit_key"]) for v in row["scope"]["visits"]}
        reduced = reduce_assessments(rows)
        for answer in result["answers"]:
            did, key = answer["device_id"], answer["visit_key"]
            if (did, key) not in scope:
                raise _refuse(web.HTTPBadRequest, "assessment is outside this request")
            visit = await db.visit_by_key(store, did, key)
            labels = await db.labels_for(store, did, [key])
            if (visit is None or not visit["has_drinking"] or key in labels
                    or not row["scope"]["start_ts"] <= visit["start_ts"] <= now):
                raise _refuse(web.HTTPConflict, "visit is removed, labeled or outside the window")
            current = await asyncio.to_thread(visit_input, visit, media_root, now)
            if current["readiness"] != READY or answer["input_rev"] != current["input_rev"]:
                raise _refuse(web.HTTPConflict, "assessment footage is stale")
            normal = (await db.guesses_for(store, did, [key])).get(key) or {}
            prior = reduced.get((did, key, answer["input_rev"])) or {}
            if answer["model_seq"] < max(normal.get("model_seq", 0), prior.get("model_seq", 0)):
                raise _refuse(web.HTTPConflict, "assessment model is older")
        accepted, reason = await db.complete_pre_alert(store, row["id"], result, now)
        if not accepted:
            raise _refuse(web.HTTPConflict, reason)
        state.alert_wake.set()
    # Alert arbitration imports this module; keep the recheck dependency lazy.
    from petkit_local.fountain_app.alerts import check_app  # noqa: PLC0415
    await check_app(request.app)
    return web.json_response({"ok": True, "repeated": False})
