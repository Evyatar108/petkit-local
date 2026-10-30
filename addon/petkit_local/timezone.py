"""Runtime clock correction shared by HTTP, MQTT and the manual control."""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from petkit_local.devices.base import Device, TimezoneCorrection
from petkit_local.ha.commands import PROPERTY_SET_SUFFIX, make_mqtt_property_set
from petkit_local.utils.coerce import to_float

if TYPE_CHECKING:
    from petkit_local.mqtt.bridge import MQTTBridge

log = logging.getLogger(__name__)

# Firmware reads back %.1f: Nepal's 5.75 is reported as 5.8.
_TZ_SAME = 0.051
_TZ_RETRY_SEC = 600
_TZ_MAX_ATTEMPTS = 3


def _replace_pending_timezone(device: Device) -> None:
    """Drop stale timezone values without dropping other settings beside them."""
    remaining = []
    for command in device.command_queue:
        params = command.get("params") if isinstance(command, dict) else None
        if isinstance(params, dict) and "timezone" in params:
            params = {key: value for key, value in params.items() if key != "timezone"}
            if not params:
                continue
            command = {**command, "params": params}
        remaining.append(command)
    device.command_queue[:] = remaining


async def push_timezone(device: Device, offset: float,
                        bridge: MQTTBridge | None = None, *,
                        now: float | None = None) -> str:
    """Send a string-valued offset over MQTT, or queue for an HTTP heartbeat.

    Firmware reads cJSON.valuestring, so a JSON number silently does nothing.
    Replace a pending timezone write instead of stacking stale targets.
    Return "superseded" instead of queuing an old target if the configured
    timezone changes while an MQTT publish is in flight.
    """
    target = device.resolved_timezone
    state = device.timezone_correction
    if state is None or state.target != offset:
        state = device.timezone_correction = TimezoneCorrection(offset)
    # Manual writes start the same cooldown without consuming an automatic
    # attempt or reopening an exhausted budget for this target.
    state.last_attempt = time.monotonic() if now is None else now
    envelope = make_mqtt_property_set({"timezone": f"{offset:g}"})
    _replace_pending_timezone(device)
    if device.mqtt_connected and bridge is not None:
        try:
            await bridge.publish_to_device(device, PROPERTY_SET_SUFFIX, envelope)
            return "mqtt"
        except Exception as exc:  # noqa: BLE001 - retain the manual control's queue fallback
            log.warning("timezone push failed for device %d: %s", device.petkit_id, exc)
    if device.resolved_timezone != target:
        return "superseded"
    # Publishing yielded: a newer queued command may now exist. Recheck the
    # target before replacing it, then mutate the queue without another await.
    _replace_pending_timezone(device)
    envelope["_service_suffix"] = PROPERTY_SET_SUFFIX
    device.command_queue.append(envelope)
    return "heartbeat-queue"


async def correct_timezone_if_drifted(device: Device, live_zone: Any,
                                       bridge: MQTTBridge | None = None,
                                       now: float | None = None) -> bool:
    """Check a live HTTP state report or MQTT property/post, not saved config.

    Never move a clock toward the server fallback: the device's report is
    better evidence in that case. Wait ten minutes from enqueue/publish before
    retrying. HTTP delivery is at-most-once, so a lost heartbeat response can
    delay recovery by that full interval; no delivery acknowledgement exists.

    Stop after three automatic writes for the target in this signup session
    and warn once. Matching echoes keep both budget and cooldown: firmware
    can briefly apply the correction and then revert. Only a new target or
    signup reopens the budget. Manual writes start the cooldown but consume
    no automatic attempt. No timer sends commands on its own: another state
    report is needed for each retry.
    """
    current = to_float(live_zone, None)
    if current is None:
        return False
    want, source = device.resolved_timezone
    if source == "server":
        return False
    if abs(current - want) < _TZ_SAME:
        return False
    state = device.timezone_correction
    if state is None or state.target != want:
        state = device.timezone_correction = TimezoneCorrection(want)
    if state.attempts >= _TZ_MAX_ATTEMPTS:
        if not state.warned:
            log.warning("Device %d still reports UTC offset %g after %d timezone "
                        "pushes toward %g; stopping automatic correction until "
                        "a new target or signup",
                        device.petkit_id, current, state.attempts, want)
            state.warned = True
        return False
    now = time.monotonic() if now is None else now
    if now - state.last_attempt < _TZ_RETRY_SEC:
        return False
    # Reserve the attempt before publishing yields, so concurrent reports do
    # not both pass the throttle.
    state.attempts += 1
    delivered = await push_timezone(device, want, bridge, now=now)
    if delivered == "superseded":
        return False
    log.info("Device %d reports UTC offset %g but belongs at %g; "
             "timezone push via %s (attempt %d/%d)",
             device.petkit_id, current, want, delivered, state.attempts, _TZ_MAX_ATTEMPTS)
    return True
