"""The fountain app (`petkit_local/fountain_app/`): visits, labels, stats.

Row shapes follow the real W7H HTTP reports of 2026-10-14 (firmware 456):
appearance `20` with its own `related_event`, detection `24` pointing back at
it through `parent_event`, and drinking `5`/`6` sharing a different
`related_event` with `event_start`/`event_end` on the drink-over.
"""
import json
import time
from datetime import datetime
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from petkit_local.ai.pets import PetRegistry
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.events.store import EventStore
from petkit_local.fountain_app import stats
from petkit_local.fountain_app.visits import build_visits
from petkit_local.web.hub import EventHub
from petkit_local.web.panel import create_panel_app

FOUNTAIN = 1


def _noon() -> float:
    """Today at local noon, hours from either midnight (see test_panel_media)."""
    return datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()


def _appearance(t0: float, rel: str = "A", pet_id: int | None = None) -> list[dict]:
    """An appearance and its detection result, as stored rows."""
    rows = [{"id": 10, "device_id": FOUNTAIN, "device_type": "w7h", "event_type": "20",
             "event_kind": "pet", "ts": t0 + 2, "related_event": rel,
             "content_json": {"start_time": int(t0), "mark": int(t0)}}]
    rows.append({"id": 11, "device_id": FOUNTAIN, "device_type": "w7h", "event_type": "24",
                 "event_kind": "pet", "ts": t0 + 54, "related_event": f"det-{rel}",
                 "parent_event": rel, "pet_id": pet_id,
                 "content_json": {"related_event": rel, "pet_id": pet_id or 0}})
    return rows


def _drink(start: float, rel: str = "B", *, end: float | None = None,
           with_start: bool = True, drink_time: int = 34, arrival_end: float | None = None,
           first_id: int = 20) -> list[dict]:
    """A drinking episode: drink start `5` (optional) and drink over `6`."""
    rows = []
    if with_start:
        rows.append({"id": first_id, "device_id": FOUNTAIN, "device_type": "w7h",
                     "event_type": "5", "event_kind": "drinking", "ts": start + 1,
                     "related_event": rel, "content_json": {"event_start": int(start)}})
    content = {"event_start": int(start), "start_time": int(start), "mark": int(start),
               "drink_time": drink_time, "pet_id": 0}
    if end is not None:
        content["event_end"] = int(end)
    rows.append({"id": first_id + 1, "device_id": FOUNTAIN, "device_type": "w7h",
                 "event_type": "6", "event_kind": "drinking",
                 "ts": arrival_end if arrival_end is not None else (end or start) + 2,
                 "related_event": rel, "content_json": content})
    return rows


# --- visits (pure) ---------------------------------------------------------


def test_drinking_within_the_window_joins_the_appearance_before_it():
    t0 = 1_792_036_551.0
    rows = _appearance(t0, pet_id=1) + _drink(t0 + 97, end=t0 + 171)
    [visit] = build_visits(rows)
    assert visit["visit_key"] == "A"
    assert visit["has_appearance"] and visit["has_drinking"]
    assert visit["start_ts"] == t0
    assert visit["drink_start_ts"] == t0 + 97
    assert visit["duration_sec"] == 74
    assert visit["drink_time_sec"] == 34
    assert visit["device_guess"] == 1
    assert [e["kind"] for e in visit["episodes"]] == ["appearance", "drinking"]
    assert visit["event_ids"] == [10, 11, 20, 21]


def test_drinking_past_the_window_is_its_own_visit():
    t0 = 1_792_000_000.0
    rows = _appearance(t0) + _drink(t0 + 181, end=t0 + 200)
    visits = build_visits(rows)
    assert [v["visit_key"] for v in visits] == ["B", "A"]
    assert visits[0]["has_appearance"] is False
    assert visits[1]["has_drinking"] is False
    assert visits[1]["duration_sec"] is None


def test_drinking_joins_the_latest_appearance_in_the_window():
    t0 = 1_792_000_000.0
    rows = (_appearance(t0, rel="A1") + _appearance(t0 + 60, rel="A2")
            + _drink(t0 + 100, end=t0 + 130))
    by_key = {v["visit_key"]: v for v in build_visits(rows)}
    assert by_key["A2"]["has_drinking"] and not by_key["A1"]["has_drinking"]


def test_drink_over_alone_is_a_visit_timed_by_its_own_fields():
    t0 = 1_792_035_020.0
    [visit] = build_visits(_drink(t0, end=t0 + 50, with_start=False))
    assert visit["visit_key"] == "B"
    assert visit["start_ts"] == t0
    assert visit["duration_sec"] == 50


def test_duration_falls_back_to_arrival_times_without_event_end():
    t0 = 1_792_000_000.0
    rows = _drink(t0, end=None, arrival_end=t0 + 1 + 72)
    [visit] = build_visits(rows)
    assert visit["duration_sec"] == 72


def test_detection_without_our_pet_leaves_no_guess_and_non_visit_rows_are_ignored():
    t0 = 1_792_000_000.0
    rows = _appearance(t0, pet_id=None) + [
        {"id": 99, "device_id": FOUNTAIN, "device_type": "w7h", "event_type": "999",
         "event_kind": "other", "ts": t0, "related_event": "Z"}]
    [visit] = build_visits(rows)
    assert visit["device_guess"] is None
    assert visit["event_ids"] == [10, 11]


def test_a_month_of_visits_builds_quickly():
    """Review: 30 days at 50 visits a day took 4.5 s, blocking the event loop."""
    t0, gap = 1_792_036_551.0, 30 * 86400 / 1500
    rows = []
    for n in range(1500):
        t = t0 + n * gap
        rows += _appearance(t, rel=f"A{n}") + _drink(t + 60, rel=f"B{n}", end=t + 90,
                                                      first_id=100 + 2 * n)
    assert len(rows) == 6000
    began = time.perf_counter()
    visits = build_visits(rows)
    elapsed = time.perf_counter() - began
    assert len(visits) == 1500 and all(v["has_appearance"] and v["has_drinking"]
                                       for v in visits)
    assert elapsed < 0.5, f"build_visits took {elapsed:.2f} s"

# --- stats (pure) ----------------------------------------------------------


def test_stats_attribute_by_label_then_guess_and_count_not_a_cat_apart():
    spans = stats.day_spans(7, now=_noon())
    today = spans[-1][1] + 3600
    visits = [
        {"visit_key": "a", "start_ts": today, "device_guess": 1, "duration_sec": 60,
         "drink_time_sec": 30},
        {"visit_key": "b", "start_ts": today + 600, "device_guess": 1, "duration_sec": 40,
         "drink_time_sec": 20},
        {"visit_key": "c", "start_ts": today + 1200, "device_guess": None, "duration_sec": 10,
         "drink_time_sec": None},
        {"visit_key": "d", "start_ts": today + 1800, "device_guess": None, "duration_sec": 5,
         "drink_time_sec": None},
        {"visit_key": "old", "start_ts": spans[0][1] - 10, "device_guess": 1,
         "duration_sec": 99, "drink_time_sec": 1},
    ]
    out = stats.aggregate(visits, {"b": "2", "d": "not_a_cat"}, spans, [1, 2])
    assert len(out["dates"]) == 7
    assert out["visits"] == 4 and out["labeled"] == 2 and out["not_a_cat"] == 1
    s1, s2, unknown = out["series"]
    assert (s1["key"], s1["visits"], s1["drink_sec"], s1["labeled"]) == ("1", 1, 60, 0)
    assert (s2["key"], s2["visits"], s2["drink_sec"], s2["labeled"]) == ("2", 1, 40, 1)
    assert s2["daily"][-1] == {"date": out["dates"][-1], "visits": 1, "drink_sec": 40}
    assert (unknown["key"], unknown["visits"]) == ("unknown", 1)


def test_day_spans_are_consecutive_local_days():
    spans = stats.day_spans(30, now=_noon())
    assert len(spans) == 30
    for (_, _, end), (_, start, _) in zip(spans, spans[1:], strict=False):
        assert end == start


# --- API -------------------------------------------------------------------


def _panel(tmp, device_type="w7h", with_store=True):
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=FOUNTAIN, device_type=device_type, serial_number="SN")
    store = EventStore(Path(tmp) / "petkit.db") if with_store else None
    pets = PetRegistry(store, str(Path(tmp) / "faces")) if with_store else None
    cfg = {"api_url": "http://x/6/", "capture": False, "capture_dir": "/nope",
           "data_dir": str(tmp), "media_root": str(Path(tmp) / "media")}
    app = create_panel_app(reg, None, EventHub(), cfg, bridge=None, event_store=store,
                           pet_registry=pets)
    return app, reg, store, pets


async def _client(app):
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


async def _seed(store, rows):
    for r in rows:
        await store.upsert_event({k: v for k, v in r.items() if k != "id"}
                                 | {"event_uid": f"{r['related_event']}-{r['event_type']}"})


async def test_visits_label_round_trip_and_stats(tmp_path):
    app, _reg, store, pets = _panel(tmp_path)
    archie = await pets.create("Archie")
    holly = await pets.create("Holly")
    t0 = _noon()
    await _seed(store, _appearance(t0, pet_id=archie["id"]) + _drink(t0 + 97, end=t0 + 171)
                + _drink(t0 + 3600, rel="C", end=t0 + 3640, first_id=30))
    await store.upsert_media({"file_id": "hl", "device_id": FOUNTAIN, "related_event": "B",
                              "category": "dynamicVideo", "status": "ready",
                              "media_path": str(tmp_path / "media" / "Highlight" / "b.mp4")})
    c = await _client(app)
    try:
        body = await (await c.get("/api/fountain/visits")).json()
        assert body["device"]["id"] == FOUNTAIN
        assert [p["pet_name"] for p in body["pets"]] == ["Archie", "Holly"]
        assert [v["visit_key"] for v in body["visits"]] == ["C", "A"]
        merged = body["visits"][1]
        assert merged["device_guess_name"] == "Archie" and merged["label"] is None
        assert merged["attributed"] == str(archie["id"])
        assert merged["episodes"][1]["media"]["highlight_url"] == "Highlight/b.mp4"

        r = await c.post("/api/fountain/visits/A/label",
                         json={"device": FOUNTAIN, "label": holly["id"]})
        assert r.status == 200
        assert (await r.json())["label_name"] == "Holly"
        r = await c.post("/api/fountain/visits/C/label",
                         json={"device": FOUNTAIN, "label": "not_a_cat"})
        assert r.status == 200

        body = await (await c.get(f"/api/fountain/visits?device={FOUNTAIN}")).json()
        labels = {v["visit_key"]: v["label"] for v in body["visits"]}
        assert labels == {"A": str(holly["id"]), "C": "not_a_cat"}

        st = await (await c.get("/api/fountain/stats?days=7")).json()
        by_key = {s["key"]: s for s in st["series"]}
        assert by_key[str(holly["id"])]["visits"] == 1
        assert by_key[str(holly["id"])]["label_name"] == "Holly"
        assert by_key[str(archie["id"])]["visits"] == 0
        assert (st["visits"], st["labeled"], st["not_a_cat"]) == (2, 2, 1)

        r = await c.delete(f"/api/fountain/visits/A/label?device={FOUNTAIN}")
        assert (await r.json())["removed"] is True
        body = await (await c.get("/api/fountain/visits")).json()
        assert body["visits"][1]["label"] is None
    finally:
        await c.close()
        await store.close()


async def test_label_refusals(tmp_path):
    app, _reg, store, _pets = _panel(tmp_path)
    await _seed(store, _drink(_noon(), end=_noon() + 30))
    c = await _client(app)
    try:
        url = "/api/fountain/visits/B/label"
        assert (await c.post(url, json={"device": FOUNTAIN, "label": "777"})).status == 400
        assert (await c.post(url, json={"device": FOUNTAIN, "label": "cat"})).status == 400
        assert (await c.post(url, json={"label": "unknown"})).status == 400
        assert (await c.post(url, json={"device": 42, "label": "unknown"})).status == 404
        r = await c.post("/api/fountain/visits/nope/label",
                         json={"device": FOUNTAIN, "label": "unknown"})
        assert r.status == 404
        assert (await c.post(url, json={"device": FOUNTAIN, "label": "unknown"})).status == 200
        assert (await c.get("/api/fountain/stats?days=9")).status == 400
    finally:
        await c.close()
        await store.close()


async def test_a_device_that_is_not_a_w7h_is_refused(tmp_path):
    app, _reg, store, _pets = _panel(tmp_path, device_type="t5")
    c = await _client(app)
    try:
        r = await c.get(f"/api/fountain/visits?device={FOUNTAIN}")
        assert r.status == 400
        assert (await r.json())["error"] == "not a W7H fountain"
        assert (await c.get("/api/fountain/visits")).status == 400
        assert (await c.get(f"/api/fountain/stats?device={FOUNTAIN}")).status == 400
    finally:
        await c.close()
        await store.close()


async def test_without_an_event_store_every_endpoint_says_so(tmp_path):
    app, *_ = _panel(tmp_path, with_store=False)
    c = await _client(app)
    try:
        for r in (await c.get("/api/fountain/visits"),
                  await c.get("/api/fountain/stats"),
                  await c.post("/api/fountain/visits/A/label", json={"label": "unknown"})):
            assert r.status == 400
            assert (await r.json())["error"] == "event store not available"
    finally:
        await c.close()


async def test_labels_survive_a_restart_on_the_same_database(tmp_path):
    app, _reg, store, _pets = _panel(tmp_path)
    await _seed(store, _drink(_noon(), end=_noon() + 30))
    c = await _client(app)
    try:
        r = await c.post("/api/fountain/visits/B/label",
                         json={"device": FOUNTAIN, "label": "unknown"})
        assert r.status == 200
    finally:
        await c.close()
        await store.close()

    app, _reg, store, _pets = _panel(tmp_path)
    c = await _client(app)
    try:
        body = await (await c.get("/api/fountain/visits")).json()
        assert body["visits"][0]["label"] == "unknown"
    finally:
        await c.close()
        await store.close()


# --- the tab (frontend) ----------------------------------------------------


async def test_the_fountain_tab_is_wired_into_the_panel(tmp_path):
    from petkit_local.web import panel as panel_mod

    app, *_ = _panel(tmp_path, with_store=False)
    c = await _client(app)
    try:
        html = await (await c.get("/")).text()
        assert '<button data-tab="fountain" role="tab"' in html
        assert 'aria-controls="tab-fountain"' in html
        assert '<section id="tab-fountain" role="tabpanel" class="hidden">' in html
        assert '<div id="fountainView">' in html
        r = await c.get(f"/asset/{panel_mod.ASSET_VERSION}/js/fountain.js")
        assert r.status == 200
        assert "export { loadFountain, scheduleFountainAlerts }" in await r.text()
    finally:
        await c.close()

    js_dir = panel_mod.STATIC_DIR / "js"
    nav = (js_dir / "nav.js").read_text()
    assert "import { loadFountain } from './fountain.js';" in nav
    assert "'fountain'," in nav
    assert "if (b.dataset.tab === 'fountain') loadFountain();" in nav
    assert "import './fountain.js';" in (js_dir / "main.js").read_text()


def test_the_fountain_tab_plays_clips_inline_and_labels_from_the_registry():
    from petkit_local.web import panel as panel_mod

    js = (panel_mod.STATIC_DIR / "js" / "fountain.js").read_text(encoding="utf-8")
    # The real-time full recording plays in the card, the snapshot as its
    # poster; the ~2x highlight is a labelled quick preview.
    assert '<video class="fx-clip" controls preload="none"' in js
    assert "const inlineUrl = m.playback_url || m.highlight_url;" in js
    assert "m.snapshot_url ? ` poster=" in js
    assert 'data-action="open-video"' in js and "Quick preview (2×)" in js
    assert "Full recording processing" in js
    # Cats come from the API's pet list; no cat is named in the source.
    for name in ("Archie", "Holly"):
        assert name not in js
    assert "['unknown', 'Unknown']" in js and "['not_a_cat', 'Not a cat']" in js

# --- dataset export ---------------------------------------------------------


async def _seed_export(tmp_path, store, pets):
    """Two labeled visits (one with media), one unlabeled, one pruned label."""
    from petkit_local.fountain_app import db

    archie = await pets.create("Archie")
    t0 = _noon()
    await _seed(store, _appearance(t0, pet_id=archie["id"]) + _drink(t0 + 97, end=t0 + 171)
                + _drink(t0 + 3600, rel="C", end=t0 + 3640, first_id=30)
                + _drink(t0 + 7200, rel="U", end=t0 + 7230, first_id=40))
    snap = tmp_path / "media" / "Image" / "a.jpg"
    snap.parent.mkdir(parents=True)
    snap.write_bytes(b"\xff\xd8\xff\xe0" + b"A" * 60)
    await store.upsert_media({"file_id": "img", "device_id": FOUNTAIN, "related_event": "A",
                              "category": "eventImage", "status": "ready",
                              "media_path": str(snap)})
    await db.TableGuard().ensure(store)
    await db.set_label(store, FOUNTAIN, "A", str(archie["id"]))
    await db.set_label(store, FOUNTAIN, "C", "not_a_cat")
    await db.set_label(store, FOUNTAIN, "gone", "unknown")
    return archie


async def test_export_writes_labeled_snapshots_and_a_csv(tmp_path, monkeypatch):
    import csv

    from petkit_local.fountain_app import export

    monkeypatch.setattr(export, "have_ffmpeg", lambda: False)
    app, _reg, store, pets = _panel(tmp_path)
    archie = await _seed_export(tmp_path, store, pets)
    c = await _client(app)
    try:
        r = await c.post("/api/fountain/export")
        assert r.status == 200
        body = await r.json()
    finally:
        await c.close()
        await store.close()

    out = Path(body["path"])
    assert out.parent == tmp_path / "datasets" and out.is_dir()
    assert not list(out.parent.glob("*.partial"))
    assert (body["visits"], body["images"], body["ffmpeg"]) == (1, 1, False)
    assert (body["missing"], body["no_media"]) == (1, 1)
    assert body["by_label"] == {str(archie["id"]): 1}
    image = out / "images" / str(archie["id"]) / "A_1.jpg"
    assert image.read_bytes().startswith(b"\xff\xd8")
    with open(out / "labels.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows == [{"file": f"images/{archie['id']}/A_1.jpg", "label": str(archie["id"]),
                     "label_name": "Archie", "device_guess": str(archie["id"]),
                     "visit_time": rows[0]["visit_time"], "source": "snapshot:appearance"}]
    assert not (out / ".frames").exists()


async def test_export_pulls_frames_from_the_highlight_clip(tmp_path):
    import shutil
    import subprocess

    import pytest

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    app, _reg, store, pets = _panel(tmp_path)
    await _seed_export(tmp_path, store, pets)
    clip = tmp_path / "media" / "Highlight" / "b.mp4"
    clip.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=duration=4:size=64x64:rate=5", str(clip)], check=True)
    await store.upsert_media({"file_id": "hl", "device_id": FOUNTAIN, "related_event": "B",
                              "category": "dynamicVideo", "status": "ready",
                              "media_path": str(clip)})
    c = await _client(app)
    try:
        body = await (await c.post(f"/api/fountain/export?device={FOUNTAIN}")).json()
    finally:
        await c.close()
        await store.close()
    assert body["ffmpeg"] is True
    assert body["images"] == 4  # the snapshot plus three clip frames
    names = sorted(p.name for p in Path(body["path"]).glob("images/*/*.jpg"))
    assert names == ["A_1.jpg", "A_2.jpg", "A_3.jpg", "A_4.jpg"]


async def test_a_second_export_while_one_runs_is_refused(tmp_path):
    from petkit_local.fountain_app.state import state_of

    app, _reg, store, _pets = _panel(tmp_path)
    c = await _client(app)
    try:
        async with state_of(app).export_lock:
            r = await c.post("/api/fountain/export")
            assert r.status == 409
    finally:
        await c.close()
        await store.close()

# --- alerts -----------------------------------------------------------------


async def test_no_drink_alerts_follow_attributed_drinking_visits(tmp_path):
    from petkit_local.fountain_app import alerts, db

    app, _reg, store, pets = _panel(tmp_path)
    archie = await pets.create("Archie")
    holly = await pets.create("Holly")
    hub = app["hub"]
    t0 = 1_792_000_000.0
    hour = 3600
    await _seed(store, _appearance(t0, pet_id=archie["id"]) + _drink(t0 + 97, end=t0 + 171))
    try:
        # No events from before the 12-hour window yet: nothing is judged.
        assert await alerts.check_app(app, now=t0 + hour) == {"raised": [], "cleared": []}

        await _seed(store, _drink(t0 - 20 * hour, rel="OLD", end=t0 - 20 * hour + 30,
                                  first_id=50))
        first = await alerts.check_app(app, now=t0 + hour)
        assert [a["subject"] for a in first["raised"]] == [f"pet:{holly['id']}"]
        assert first["raised"][0]["detail"]["message"] == (
            "Holly has not drunk from the fountain in 12 hours")
        assert first["raised"][0]["detail"]["last_drink_ts"] is None

        later = await alerts.check_app(app, now=t0 + 13 * hour)
        assert [a["subject"] for a in later["raised"]] == [f"pet:{archie['id']}"]
        assert later["raised"][0]["detail"]["last_drink_ts"] == t0
        assert later["cleared"] == []

        await _seed(store, _drink(t0 + 13.5 * hour, rel="H", end=t0 + 13.5 * hour + 40,
                                  first_id=60))
        await db.set_label(store, FOUNTAIN, "H", str(holly["id"]))
        cleared = await alerts.check_app(app, now=t0 + 14 * hour)
        assert cleared["raised"] == []
        assert [a["subject"] for a in cleared["cleared"]] == [f"pet:{holly['id']}"]
        assert await alerts.check_app(app, now=t0 + 14 * hour + 60) == {
            "raised": [], "cleared": []}
        assert [a["subject"] for a in await db.active_alerts(store)] == [f"pet:{archie['id']}"]
    finally:
        await store.close()
    logged = [e["summary"] for e in hub.recent(kinds={alerts.HUB_KIND})]
    assert logged == ["Holly has not drunk from the fountain in 12 hours",
                      "Archie has not drunk from the fountain in 12 hours",
                      "Cleared: Holly has not drunk from the fountain in 12 hours"]


async def test_tank_alerts_follow_error_start_and_error_over(tmp_path):
    from petkit_local.events.normalize import from_mqtt
    from petkit_local.fountain_app import alerts

    app, reg, store, _pets = _panel(tmp_path)
    dev = reg.get(FOUNTAIN)
    t0 = 1_792_000_000.0

    async def report(event_type, err, ts, eid):
        row = from_mqtt(dev, event_type, {"event_id": eid, "content": json.dumps({"err": err})})
        await store.upsert_event(row | {"ts": ts})

    try:
        await report("error_start", "tankCL", t0, "e1")
        await report("error_start", "taryF", t0 + 1, "e2")
        raised = (await alerts.check_app(app, now=t0 + 60))["raised"]
        assert [(a["kind"], a["subject"]) for a in raised] == [("tank", f"{FOUNTAIN}:tankCL")]
        assert raised[0]["detail"]["message"] == f"Clean water tank low (fountain #{FOUNTAIN})"

        await report("error_start", "tankDF", t0 + 100, "e3")
        await report("error_over", "tankCL", t0 + 200, "e4")
        step = await alerts.check_app(app, now=t0 + 260)
        assert [a["subject"] for a in step["raised"]] == [f"{FOUNTAIN}:tankDF"]
        assert [a["subject"] for a in step["cleared"]] == [f"{FOUNTAIN}:tankCL"]
    finally:
        await store.close()


def test_an_error_over_naming_nothing_clears_every_tank_flag():
    from petkit_local.fountain_app.alerts import tank_transitions

    def row(event_type, ts, content):
        return {"device_type": "w7h", "event_type": event_type, "ts": ts,
                "content_json": json.dumps(content)}

    rows = [row("error_start", 1, {"err": "tankCL,tankDF"}),
            row("error_over", 2, {})]
    assert tank_transitions(rows) == {"tankCL": None, "tankDF": None}
    assert set(tank_transitions(rows[:1])) == {"tankCL", "tankDF"}
    assert tank_transitions([row("error_start", 1, {"err": "taryF"})]) == {}


async def test_a_tank_alert_outlives_many_unrelated_faults(tmp_path, monkeypatch):
    """Review repro: tankCL, then 201 taryF start/clear pairs, must stay raised."""
    from petkit_local.events.normalize import from_mqtt
    from petkit_local.fountain_app import alerts, db

    monkeypatch.setattr(alerts, "TANK_SCAN_PAGE", 50)  # several pages, not one
    app, reg, store, _pets = _panel(tmp_path)
    dev = reg.get(FOUNTAIN)
    t0 = 1_792_000_000.0

    async def report(event_type, err, ts, eid):
        row = from_mqtt(dev, event_type, {"event_id": eid, "content": json.dumps({"err": err})})
        await store.upsert_event(row | {"ts": ts})

    try:
        await report("error_start", "tankCL", t0, "cl")
        assert [a["subject"] for a in (await alerts.check_app(app, now=t0 + 1))["raised"]] == [
            f"{FOUNTAIN}:tankCL"]
        for i in range(201):
            await report("error_start", "taryF", t0 + 10 + 2 * i, f"s{i}")
            await report("error_over", "taryF", t0 + 11 + 2 * i, f"o{i}")
        assert await alerts.check_app(app, now=t0 + 1000) == {"raised": [], "cleared": []}
        assert [a["subject"] for a in await db.active_alerts(store)] == [f"{FOUNTAIN}:tankCL"]

        await report("error_over", "tankCL", t0 + 2000, "clr")
        step = await alerts.check_app(app, now=t0 + 2001)
        assert [a["subject"] for a in step["cleared"]] == [f"{FOUNTAIN}:tankCL"]
    finally:
        await store.close()


async def test_an_alert_whose_start_is_out_of_reach_is_not_cleared(tmp_path, monkeypatch):
    from petkit_local.events.normalize import from_mqtt
    from petkit_local.fountain_app import alerts, db

    app, reg, store, _pets = _panel(tmp_path)
    t0 = 1_792_000_000.0
    row = from_mqtt(reg.get(FOUNTAIN), "error_start",
                    {"event_id": "cl", "content": json.dumps({"err": "tankCL"})})
    await store.upsert_event(row | {"ts": t0})
    try:
        await alerts.check_app(app, now=t0 + 1)
        monkeypatch.setattr(alerts, "TANK_SCAN_MAX", 0)  # nothing read at all
        assert await alerts.check_app(app, now=t0 + 2) == {"raised": [], "cleared": []}
        assert [a["subject"] for a in await db.active_alerts(store)] == [f"{FOUNTAIN}:tankCL"]
    finally:
        await store.close()


async def test_alert_settings_persist_and_the_check_task_starts(tmp_path):
    app, _reg, store, _pets = _panel(tmp_path)
    c = await _client(app)
    try:
        assert any(t.get_name() == "fountain-alerts" for t in app["background_tasks"])
        assert await (await c.get("/api/fountain/alert-settings")).json() == {
            "no_drink_hours": 12.0}
        for bad in (0.5, 200, "soon", True, None):
            r = await c.post("/api/fountain/alert-settings", json={"no_drink_hours": bad})
            assert r.status == 400, bad
        r = await c.post("/api/fountain/alert-settings", json={"no_drink_hours": 24})
        assert r.status == 200
        body = await (await c.get("/api/fountain/alerts")).json()
        assert body == {"active": [], "recent": [], "settings": {"no_drink_hours": 24.0}}
    finally:
        await c.close()
    assert not app["background_tasks"]
    app2, _reg2, store2, _pets2 = _panel(tmp_path)
    c = await _client(app2)
    try:
        assert await (await c.get("/api/fountain/alert-settings")).json() == {
            "no_drink_hours": 24.0}
    finally:
        await c.close()
        await store.close()
        await store2.close()


@pytest.mark.parametrize(("with_appearance", "with_start", "late"), [
    (True, True, None), (True, False, None), (False, False, None),
    (True, False, "drink"), (False, False, "drink"), (True, True, "all")],
    ids=["appearance-and-drink-start", "no-drink-start", "drink-over-alone",
         "no-drink-start-recorded-7h-late", "drink-over-alone-recorded-7h-late",
         "everything-recorded-7h-late"])
async def test_a_drink_that_ends_after_midnight_keeps_its_duration(
        tmp_path, with_appearance, with_start, late):
    """Review repro: appearance 23:58, drink 23:59:30 to 00:10 the next day.

    The drink-over arrives at 00:10, or (`late`) at 07:00 when the PC was off
    overnight. Without a drink-start it is the episode's only row; every
    episode files by the start it reports, however late it was recorded.
    """
    app, _reg, store, pets = _panel(tmp_path)
    archie = await pets.create("Archie")
    t0 = datetime(2026, 10, 14, 23, 58).timestamp()
    drink = datetime(2026, 10, 14, 23, 59, 30).timestamp()
    over = datetime(2026, 10, 15, 0, 10).timestamp()
    recorded = datetime(2026, 10, 15, 7, 0).timestamp()
    rows = _drink(drink, end=over, with_start=with_start,
                  arrival_end=recorded if late else None)
    if with_appearance:
        rows = _appearance(t0, pet_id=archie["id"]) + rows
    if late == "all":
        rows = [{**r, "ts": recorded + i} for i, r in enumerate(rows)]
    await _seed(store, rows)
    c = await _client(app)
    try:
        day = await (await c.get("/api/fountain/visits?date=2026-10-14")).json()
        [visit] = day["visits"]
        assert visit["visit_key"] == ("A" if with_appearance else "B")
        assert visit["has_appearance"] == with_appearance and visit["has_drinking"]
        assert visit["duration_sec"] == 630
        assert visit["drink_end_ts"] == over
        after = await (await c.get("/api/fountain/visits?date=2026-10-15")).json()
        assert after["visits"] == []
    finally:
        await c.close()
        await store.close()