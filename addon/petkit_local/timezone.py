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
                        bridge: MQTTBridge | None = None) -> str:
    """Send a string-valued offset over MQTT, or queue for an HTTP heartbeat.

    Firmware reads cJSON.valuestring, so a JSON number silently does nothing.
    Replace a pending timezone write instead of stacking stale targets.
    """
    envelope = make_mqtt_property_set({"timezone": f"{offset:g}"})
    _replace_pending_timezone(device)
    if device.mqtt_connected and bridge is not None:
        try:
            await bridge.publish_to_device(device, PROPERTY_SET_SUFFIX, envelope)
            return "mqtt"
        except Exception as exc:  # noqa: BLE001 - retain the manual control's queue fallback
            log.warning("timezone push failed for device %d: %s", device.petkit_id, exc)
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

    Stop after three non-echoed writes for the target in this signup session
    and warn once. A matching report clears the attempt history. A new target
    or signup permits correction again. No timer sends commands on its own:
    another state report is needed for each retry.
    """
    current = to_float(live_zone, None)
    if current is None:
        return False
    want, source = device.resolved_timezone
    if source == "server":
        return False
    if abs(current - want) < _TZ_SAME:
        device.timezone_correction = None
        return False
    state = device.timezone_correction
    if state is None or state.target != want:
        state = device.timezone_correction = TimezoneCorrection(want)
    if state.attempts >= _TZ_MAX_ATTEMPTS:
        if not state.warned:
            log.warning("Device %d still reports UTC offset %g after %d timezone "
                        "pushes toward %g; stopping automatic correction until "
                        "a matching report, new target or signup",
                        device.petkit_id, current, state.attempts, want)
            state.warned = True
        return False
    now = time.monotonic() if now is None else now
    if now - state.last_attempt < _TZ_RETRY_SEC:
        return False
    # Reserve the attempt before publishing yields, so concurrent reports do
    # not both pass the throttle.
    state.last_attempt = now
    state.attempts += 1
    delivered = await push_timezone(device, want, bridge)
    log.info("Device %d reports UTC offset %g but belongs at %g; "
             "timezone push via %s (attempt %d/%d)",
             device.petkit_id, current, want, delivered, state.attempts, _TZ_MAX_ATTEMPTS)
    return True
