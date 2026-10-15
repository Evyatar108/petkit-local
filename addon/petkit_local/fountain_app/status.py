"""One fountain at a glance: `GET /api/fountain/status?device=`.

For the phone's "Everything else is fine" row. Read-only. Each item is
`{key, label, value, ok, detail}`: `value` is a short display string, `ok` is
True (fine), False (needs a look) or None (unknown), and `detail` carries the
machine-readable fields behind `value`. `all_ok` is True only when every item
is ok, so an unknown item makes it False.

Device input never raises here: a setting that is absent or not a number
makes its item unknown. Without an event store the tank and alert items are
unknown and the rest is still answered.

* **Online**: `ha/publisher.py::device_is_stale` over every contact time
  (HTTP and MQTT), with the add-on's default offline timeout; the panel is
  not handed the configured one. Never heard from is unknown.
* **Flow mode and auto refill**: `settings.fountainMode` (0 off is not ok)
  and `settings.addWaterSwitch` (off is not ok: the bowl is not refilled).
* **Tanks**: the active `tankCL` / `tankDF` alerts `alerts.py` raises from
  the fountain's error events, never the raw hall sensors.
* **Scheduled water change and flush**: enabled, every N days, at HH:MM in
  the server's local time. The device stores seconds since midnight on its
  own clock, and the W7H (firmware 456) keeps that clock on UTC whatever it
  is told (`debt:timezone-resets-on-boot` in the fountain repo's
  DECISIONS.md), so the device time is read as UTC and converted; `detail`
  keeps it as `device_time`. Off is a choice and is ok; on without a usable
  cycle or time is unknown.
* **Alerts**: how many alerts of any kind are active.
"""
from __future__ import annotations

import time
from typing import Any

from aiohttp import web

from petkit_local.config import Config
from petkit_local.events import codes
from petkit_local.fountain_app import db
from petkit_local.fountain_app.alerts import tank_subject
from petkit_local.fountain_app.api import _device, _device_summary
from petkit_local.fountain_app.state import state_of
from petkit_local.ha.publisher import device_is_stale
from petkit_local.utils.coerce import to_int
from petkit_local.utils.timeutil import local_offset_hours

#: The add-on's default `offline_timeout`, in seconds.
OFFLINE_TIMEOUT_SEC = Config.offline_timeout
UNKNOWN = "Unknown"
FLOW_MODES = {0: "Off", 1: "Continuous", 2: "Intermittent", 3: "Motion-activated"}
#: `(item key, error flag, label, value while the alert is active)`.
TANKS = (
    ("clean_tank", "tankCL", "Clean water tank", "Low"),
    ("waste_tank", "tankDF", "Waste water tank", "Full"),
)
#: `(item key, label, enable setting, cycle setting, time setting)`.
SCHEDULES = (
    ("water_change", "Scheduled water change", "autoWaterChange", "waterChangeCycle",
     "waterChangeTime"),
    ("flush", "Scheduled flush", "autoFlush", "flushCycle", "flushTime"),
)


def _item(key: str, label: str, value: str | None, ok: bool | None,
          **detail: Any) -> dict[str, Any]:
    return {"key": key, "label": label, "value": UNKNOWN if value is None else value,
            "ok": ok, "detail": detail}


def _switch(settings: dict[str, Any], name: str) -> bool | None:
    value = to_int(settings.get(name), None)
    return None if value not in (0, 1) else bool(value)


def hhmm(seconds: Any) -> str | None:
    """Seconds since midnight as `HH:MM`, or None if it is not a time of day."""
    s = to_int(seconds, None)
    if s is None or not 0 <= s < 86400:
        return None
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}"


def online_item(dev: Any, now: float) -> dict[str, Any]:
    last = max(dev.last_heartbeat, dev.last_state_report, dev.last_seen, dev.last_mqtt)
    if last <= 0:
        return _item("online", "Fountain online", "Never seen", None, last_seen_ts=None)
    stale = device_is_stale(dev, now, OFFLINE_TIMEOUT_SEC)
    return _item("online", "Fountain online", "Offline" if stale else "Online", not stale,
                 last_seen_ts=last)


def flow_items(settings: dict[str, Any]) -> list[dict[str, Any]]:
    mode = to_int(settings.get("fountainMode"), None)
    if mode not in FLOW_MODES:
        mode = None
    refill = _switch(settings, "addWaterSwitch")
    return [
        _item("flow_mode", "Water flow", FLOW_MODES.get(mode),
              None if mode is None else mode != 0, mode=mode),
        _item("auto_refill", "Auto refill",
              None if refill is None else ("On" if refill else "Off"), refill,
              enabled=refill),
    ]


def schedule_items(settings: dict[str, Any], local_offset: float) -> list[dict[str, Any]]:
    """`local_offset`: hours east of UTC here; device times are on UTC (see module doc)."""
    out = []
    for key, label, enable, cycle_name, time_name in SCHEDULES:
        enabled = _switch(settings, enable)
        cycle = to_int(settings.get(cycle_name), None)
        cycle = cycle if cycle is not None and cycle >= 1 else None
        device_secs = to_int(settings.get(time_name), None)
        device_at = hhmm(device_secs)
        at = None if device_at is None else hhmm(
            (device_secs + round(local_offset * 3600)) % 86400)
        if enabled is None:
            value, ok = None, None
        elif not enabled:
            value, ok = "Off", True
        elif cycle is None or at is None:
            value, ok = None, None
        else:
            every = "Every day" if cycle == 1 else f"Every {cycle} days"
            value, ok = f"{every} at {at}", True
        out.append(_item(key, label, value, ok, enabled=enabled, cycle_days=cycle, time=at,
                         device_time=device_at))
    return out


def tank_items(device_id: int, active: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if active is None:
        return [_item(key, label, None, None, alert=None, since_ts=None)
                for key, _flag, label, _bad in TANKS]
    by_subject = {a.get("subject"): a for a in active}
    out = []
    for key, flag, label, bad in TANKS:
        alert = by_subject.get(tank_subject(device_id, flag))
        since = alert.get("detail", {}).get("since_ts") if alert else None
        out.append(_item(key, label, bad if alert else "OK", alert is None,
                         alert=codes.FOUNTAIN_W7H_ERROR_FLAGS.get(flag, flag) if alert
                         else None, since_ts=since))
    return out


def alerts_item(active: list[dict[str, Any]] | None) -> dict[str, Any]:
    if active is None:
        return _item("alerts", "Active alerts", None, None, count=None)
    n = len(active)
    return _item("alerts", "Active alerts", "None" if n == 0 else str(n), n == 0, count=n)


def build_status(dev: Any, active: list[dict[str, Any]] | None,
                 now: float) -> dict[str, Any]:
    """The status of one fountain; `active` is None without an event store."""
    config = dev.config if isinstance(dev.config, dict) else {}
    settings = config.get("settings")
    settings = settings if isinstance(settings, dict) else {}
    items = [online_item(dev, now), *flow_items(settings),
             *tank_items(dev.petkit_id, active),
             *schedule_items(settings, local_offset_hours(now)),
             alerts_item(active)]
    return {"device": _device_summary(dev), "checked_at": now,
            "all_ok": all(i["ok"] is True for i in items), "items": items}


async def api_fountain_status(request: web.Request) -> web.Response:
    """`GET /api/fountain/status?device=`; see the module doc.

    `device` defaults to the first W7H; an unknown one is 404, a non-W7H 400.
    """
    dev = _device(request.app, request.query.get("device"))
    store = request.app.get("event_store")
    active = None
    if store is not None:
        await state_of(request.app).tables.ensure(store)
        active = await db.active_alerts(store)
    return web.json_response(build_status(dev, active, time.time()))
