"""Urgent alert contracts with synthetic visits/files and a deterministic clock."""
import asyncio
import time

import pytest

from petkit_local.fountain_app import alerts, db, pre_alert
from petkit_local.fountain_app.state import state_of
from tests.test_fountain_app import FOUNTAIN, _client, _drink, _panel, _seed
from tests.test_fountain_model_guess import _media, _work


async def scene(tmp_path, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(pre_alert.time, "time", lambda: now[0])
    app, _, store, pets = _panel(tmp_path)
    archie = await pets.create("Archie")
    holly = await pets.create("Holly")
    await _seed(store, _drink(now[0] - 20 * 3600, rel="old", end=now[0] - 20 * 3600 + 30,
                             first_id=50)
                + _drink(now[0] - 600, rel="new", end=now[0] - 560))
    await _media(store, tmp_path, "new", "fullVideo", "Playback/synthetic.mp4")
    client = await _client(app)
    await alerts.check_app(app)
    requests = (await (await client.get("/api/fountain/model/alert-checks")).json())["requests"]
    revision = (await _work(client))["new"]["input_rev"]
    return app, store, client, now, archie, holly, requests, revision


def result(pet, revision, seq=21, *, confidence=1.0, validated=False):
    return {
        "model_seq": seq, "validation_passed": validated,
        "answers": [{"device_id": FOUNTAIN, "visit_key": "new", "label": str(pet),
                     "input_rev": revision, "confidence": confidence, "crops": 4}],
    }


async def test_one_deadline_survives_rechecks_and_table_guard_restart(tmp_path, monkeypatch):
    app, store, c, now, archie, _, requests, _ = await scene(tmp_path, monkeypatch)
    try:
        assert len(requests) == 2
        assert await db.active_alerts(store) == []
        original = {r["id"]: r["deadline"] for r in requests}
        now[0] += 599
        state_of(app).tables = db.TableGuard()
        await alerts.check_app(app)
        assert await db.active_alerts(store) == []
        assert original == {r["id"]: r["deadline"] for r in await db.pre_alert_checks(store)}
        now[0] += 1
        step = await alerts.check_app(app)
        assert len(step["raised"]) == 2
        assert all(a["kind"] == pre_alert.KIND_UNCONFIRMED for a in step["raised"])
        assert len(await db.pre_alert_checks(store)) == 2
        assert '"none"' in (await db.pre_alert_checks(store))[0]["episode_key"]
    finally:
        await c.close()
        await store.close()


async def test_failed_model_can_clear_only_its_alert_not_labels_or_graphs(tmp_path, monkeypatch):
    app, store, c, now, archie, holly, requests, rev = await scene(tmp_path, monkeypatch)
    try:
        req = next(r for r in requests if r["pet_id"] == archie["id"])
        body = result(archie["id"], rev)
        response = await c.post("/api/fountain/model/alert-checks/" + req["id"], json=body)
        assert response.status == 200
        active = await db.active_alerts(store)
        assert [(a["subject"], a["kind"]) for a in active] == [
            (f"pet:{archie['id']}", pre_alert.KIND_UNVALIDATED)]
        assert "unvalidated" in active[0]["detail"]["message"]
        assert await db.guesses_for(store, FOUNTAIN) == {}
        assert await db.labels_for(store, FOUNTAIN) == {}
        repeat = await c.post("/api/fountain/model/alert-checks/" + req["id"], json=body)
        assert repeat.status == 200 and (await repeat.json())["repeated"]
        assert len(await db.active_alerts(store)) == 1
        work = await (await c.get("/api/fountain/model/work")).json()
        assert work["model_sequence_watermark"] == 21
        assert all(v["model_guess"] is None for v in work["visits"])
    finally:
        await c.close()
        await store.close()


@pytest.mark.parametrize("reverse", [False, True])
async def test_one_drink_cannot_clear_two_pets_with_conflicting_sequences(tmp_path, monkeypatch, reverse):
    app, store, c, now, archie, holly, requests, rev = await scene(tmp_path, monkeypatch)
    try:
        by_pet = {r["pet_id"]: r for r in requests}
        older = (archie["id"], 21)
        newer = (holly["id"], 22)
        for pid, seq in ((newer, older) if reverse else (older, newer)):
            response = await c.post(
                "/api/fountain/model/alert-checks/" + by_pet[pid]["id"],
                json=result(pid, rev, seq))
            assert response.status == (409 if reverse and seq == 21 else 200)
        await alerts.check_app(app)
        notices = [a for a in await db.active_alerts(store) if a["kind"] == pre_alert.KIND_UNVALIDATED]
        assert [a["subject"] for a in notices] == [f"pet:{holly['id']}"]
        if not reverse:
            replay = await c.post(
                "/api/fountain/model/alert-checks/" + by_pet[archie["id"]]["id"],
                json=result(archie["id"], rev, 21))
            assert replay.status == 200
            await alerts.check_app(app)
            assert [a["subject"] for a in await db.active_alerts(store)
                    if a["kind"] == pre_alert.KIND_UNVALIDATED] == [f"pet:{holly['id']}"]
    finally:
        await c.close()
        await store.close()


def test_equal_sequence_conflict_and_newer_uncertainty_revoke_old_positive():
    def row(label, seq):
        return {"result": result(label, "rev", seq)}
    key = (FOUNTAIN, "new", "rev")
    assert pre_alert.reduce_assessments([row(1, 20), row(2, 20)])[key]["label"] == "not_sure"
    assert pre_alert.reduce_assessments([row(2, 20), row(1, 20)])[key]["label"] == "not_sure"
    assert pre_alert.reduce_assessments([row(1, 20), row("not_sure", 21)])[key]["label"] == "not_sure"
    assert pre_alert.reduce_assessments([row("not_sure", 21), row(1, 20)])[key]["label"] == "not_sure"


@pytest.mark.parametrize("fault", ["low", "zero_crops", "stale", "label", "scope", "late", "unknown_pet", "unknown_request"])
async def test_invalid_assessment_cannot_clear_warning(tmp_path, monkeypatch, fault):
    app, store, c, now, archie, holly, requests, rev = await scene(tmp_path, monkeypatch)
    try:
        req = requests[0]
        body = result(archie["id"], rev)
        target = req["id"]
        if fault == "low":
            body["answers"][0]["confidence"] = .79
        elif fault == "zero_crops":
            body["answers"][0]["crops"] = 0
        elif fault == "stale":
            body["answers"][0]["input_rev"] = "stale"
        elif fault == "label":
            await db.set_label(store, FOUNTAIN, "new", str(holly["id"]))
        elif fault == "scope":
            body["answers"][0]["visit_key"] = "unrequested"
        elif fault == "late":
            now[0] += 600
        elif fault == "unknown_pet":
            body["answers"][0]["label"] = "999"
        else:
            target = "missing"
        response = await c.post("/api/fountain/model/alert-checks/" + target, json=body)
        assert response.status == (200 if fault in ("low", "zero_crops") else 400 if fault in ("scope", "unknown_pet")
                                   else 404 if fault == "unknown_request" else 409)
        await alerts.check_app(app)
        assert not any(a["kind"] == pre_alert.KIND_UNVALIDATED for a in await db.active_alerts(store))
    finally:
        await c.close()
        await store.close()


async def test_label_and_model_switch_revoke_emergency_notice(tmp_path, monkeypatch):
    app, store, c, now, archie, holly, requests, rev = await scene(tmp_path, monkeypatch)
    try:
        req = requests[0]
        assert (await c.post("/api/fountain/model/alert-checks/" + req["id"],
                             json=result(archie["id"], rev))).status == 200
        assert any(a["kind"] == pre_alert.KIND_UNVALIDATED for a in await db.active_alerts(store))
        response = await c.post("/api/fountain/alert-settings", json={"use_model_answers": False})
        assert response.status == 200
        assert not any(a["kind"] == pre_alert.KIND_UNVALIDATED for a in await db.active_alerts(store))
        assert (await (await c.get("/api/fountain/model/alert-checks")).json())["requests"] == []
        await db.set_label(store, FOUNTAIN, "new", str(archie["id"]))
        await alerts.check_app(app)
        assert not any(a["subject"] == f"pet:{archie['id']}" for a in await db.active_alerts(store))
    finally:
        await c.close()
        await store.close()


@pytest.mark.parametrize("change", ["footage", "label", "not_sure"])
async def test_accepted_evidence_is_revalidated_and_can_be_revoked(tmp_path, monkeypatch, change):
    app, store, c, now, archie, holly, requests, rev = await scene(tmp_path, monkeypatch)
    try:
        a = next(r for r in requests if r["pet_id"] == archie["id"])
        h = next(r for r in requests if r["pet_id"] == holly["id"])
        assert (await c.post("/api/fountain/model/alert-checks/" + a["id"],
                             json=result(archie["id"], rev, 25))).status == 200
        if change == "footage":
            (tmp_path / "media" / "Playback" / "synthetic.mp4").write_bytes(b"replacement")
        elif change == "label":
            await db.set_label(store, FOUNTAIN, "new", str(holly["id"]))
        else:
            assert (await c.post("/api/fountain/model/alert-checks/" + h["id"],
                                 json=result("not_sure", rev, 26))).status == 200
            assert await db.model_sequence_watermark(store) == 26
        await alerts.check_app(app)
        assert not any(a["kind"] == pre_alert.KIND_UNVALIDATED for a in await db.active_alerts(store))
        assert await db.guesses_for(store, FOUNTAIN) == {}
    finally:
        await c.close()
        await store.close()


async def test_newer_emergency_replaces_older_normal_attribution_for_alerts_only(tmp_path, monkeypatch):
    app, store, c, now, archie, holly, requests, rev = await scene(tmp_path, monkeypatch)
    try:
        a = next(r for r in requests if r["pet_id"] == archie["id"])
        h = next(r for r in requests if r["pet_id"] == holly["id"])
        ordinary = {
            "label": str(archie["id"]), "confidence": 1.0, "crops": 4,
            "model_seq": 21, "input_rev": rev,
        }
        await db.put_guess(store, FOUNTAIN, "new", ordinary)
        # Finish Archie's check without extra emergency evidence so its
        # original grace period cannot conceal stale normal attribution.
        empty = {"model_seq": 21, "validation_passed": True, "answers": []}
        assert (await c.post("/api/fountain/model/alert-checks/" + a["id"], json=empty)).status == 200
        assert (await c.post("/api/fountain/model/alert-checks/" + h["id"],
                             json=result(holly["id"], rev, 22))).status == 200
        active = await db.active_alerts(store)
        assert any(r["subject"] == f"pet:{archie['id']}"
                   and r["kind"] in (alerts.KIND_NO_DRINK, pre_alert.KIND_UNCONFIRMED)
                   for r in active)
        assert any(r["subject"] == f"pet:{holly['id']}"
                   and r["kind"] == pre_alert.KIND_UNVALIDATED for r in active)
        assert (await db.guesses_for(store, FOUNTAIN))["new"] == {
            **ordinary, "scored_at": now[0]}
    finally:
        await c.close()
        await store.close()


@pytest.mark.parametrize("normal_label", ["other", "not_sure"])
async def test_newer_normal_assessment_also_revokes_emergency_attribution(tmp_path, monkeypatch, normal_label):
    app, store, c, now, archie, holly, requests, rev = await scene(tmp_path, monkeypatch)
    try:
        a = next(r for r in requests if r["pet_id"] == archie["id"])
        assert (await c.post("/api/fountain/model/alert-checks/" + a["id"],
                             json=result(archie["id"], rev, 25))).status == 200
        label = str(holly["id"]) if normal_label == "other" else "not_sure"
        await db.put_guess(store, FOUNTAIN, "new", {
            "label": label, "confidence": 1.0, "crops": 4, "model_seq": 26, "input_rev": rev})
        await alerts.check_app(app)
        assert not any(row["kind"] == pre_alert.KIND_UNVALIDATED
                       for row in await db.active_alerts(store))
        assert (await db.guesses_for(store, FOUNTAIN))["new"]["label"] == label
    finally:
        await c.close()
        await store.close()


async def test_semantic_transition_gets_new_identity_but_poll_does_not(tmp_path, monkeypatch):
    app, store, c, now, archie, holly, requests, rev = await scene(tmp_path, monkeypatch)
    try:
        now[0] += 600
        await alerts.check_app(app)
        original = next(a for a in await db.active_alerts(store) if a["subject"] == f"pet:{archie['id']}")
        assert original["kind"] == pre_alert.KIND_UNCONFIRMED
        await db.set_label(store, FOUNTAIN, "new", str(holly["id"]))
        step = await alerts.check_app(app)
        current = next(a for a in await db.active_alerts(store) if a["subject"] == original["subject"])
        assert current["kind"] == alerts.KIND_NO_DRINK and current["id"] != original["id"]
        assert any(a["id"] == original["id"] for a in step["cleared"])
        assert await alerts.check_app(app) == {"raised": [], "cleared": []}
    finally:
        await c.close()
        await store.close()


@pytest.mark.parametrize("start,creation,deadline", [(0, 1, 601), (599.5, None, 600), (601, None, 600)])
async def test_actual_alert_loop_wakes_at_deadline_and_immediately_on_restart(monkeypatch, start, creation, deadline):
    now = [start]
    pending = [None if creation is not None else deadline]
    times = []
    wake = asyncio.Event()
    app = {"event_store": object()}
    monkeypatch.setattr(alerts, "state_of", lambda app: type("State", (), {"alert_wake": wake})())
    monkeypatch.setattr(alerts.time, "time", lambda: now[0])

    async def check(app, **kwargs):
        times.append(now[0])
        if now[0] >= deadline:
            raise asyncio.CancelledError

    async def earliest(store, current):
        return pending[0]

    async def wait(coro, timeout):
        coro.close()
        if pending[0] is None:
            now[0] = creation
            pending[0] = deadline
            return True
        now[0] += timeout
        raise TimeoutError

    monkeypatch.setattr(alerts, "check_app", check)
    monkeypatch.setattr(pre_alert, "pending_deadline", earliest)
    monkeypatch.setattr(alerts.asyncio, "wait_for", wait)
    with pytest.raises(asyncio.CancelledError):
        await alerts.alert_loop(app)
    assert times[0] == start and times[-1] == max(start, deadline)


@pytest.mark.parametrize("start,finish,sleep", [(599.5, 600.25, 0), (600.25, 600.3, 60)])
async def test_deadline_crossed_during_reconciliation_is_not_lost(tmp_path, monkeypatch, start, finish, sleep):
    app, _, store, _ = _panel(tmp_path)
    await state_of(app).tables.ensure(store)
    await db.ensure_pre_alert(store, "synthetic-gap", 1, {"visits": []}, 0, 600)
    now, times, sleeps = [start], [], []
    monkeypatch.setattr(alerts.time, "time", lambda: now[0])

    async def check(app, now=None):
        times.append(now)
        if len(times) == 1:
            # Database work crosses the absolute deadline after deciding
            # whether the request was still waiting at the snapshot time.
            clock[0] = finish
        else:
            raise asyncio.CancelledError

    async def wait(coro, timeout):
        coro.close()
        sleeps.append(timeout)
        clock[0] += timeout
        raise TimeoutError

    clock = now
    monkeypatch.setattr(alerts, "check_app", check)
    monkeypatch.setattr(alerts.asyncio, "wait_for", wait)
    try:
        with pytest.raises(asyncio.CancelledError):
            await alerts.alert_loop(app)
        assert sleeps == [sleep]
        assert times == [start, finish + sleep]
    finally:
        await store.close()
