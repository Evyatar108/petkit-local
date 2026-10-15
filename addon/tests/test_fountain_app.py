"""The fountain app (`petkit_local/fountain_app/`): visits, labels, stats.

Row shapes follow the real W7H HTTP reports of 2026-10-14 (firmware 456):
appearance `20` with its own `related_event`, detection `24` pointing back at
it through `parent_event`, and drinking `5`/`6` sharing a different
`related_event` with `event_start`/`event_end` on the drink-over.
"""
from datetime import datetime
from pathlib import Path

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
