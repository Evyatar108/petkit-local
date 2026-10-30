"""Bounded runtime clock correction through both device transports."""
import asyncio
import json
from functools import partial

import pytest
from aiohttp.test_utils import TestClient, TestServer

from petkit_local.devices.base import Device
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.http.server import create_app
from petkit_local.main.wiring import _on_signup, _on_state_report
from petkit_local.mqtt.bridge import MQTTBridge
from petkit_local.timezone import correct_timezone_if_drifted, push_timezone
from tests._fakes import FakeMqttClient


def _device(locale="Asia/Kolkata", petkit_id=1):
    d = Device(device_type="w7h", petkit_id=petkit_id, serial_number="SN")
    d.config["locale"] = locale
    # What signup stores: the CONFIGURED zone, which is right even when the
    # clock is not. It must not be what the check reads.
    d.config["reported_timezone"] = 5.5
    return d


def _timezone_pushes(device):
    return [c for c in device.command_queue
            if isinstance(c.get("params"), dict) and "timezone" in c["params"]]


async def test_a_clock_back_on_utc_is_sent_its_own_zone_as_a_string():
    device = _device()

    assert await correct_timezone_if_drifted(device, "0.0", now=1000.0) is True

    [push] = _timezone_pushes(device)
    assert push["method"] == "thing.service.property.set"
    assert push["params"] == {"timezone": "5.5"}, "the firmware ignores a number"
    assert push["_service_suffix"] == "property/set"


async def test_a_clock_already_in_its_zone_is_left_alone():
    assert await correct_timezone_if_drifted(_device(), "5.5", now=1000.0) is False


async def test_the_lossy_readback_of_a_quarter_hour_zone_is_not_drift():
    # The device reports its own offset as %.1f, so Nepal's 5.75 comes back 5.8.
    device = _device(locale="Asia/Kathmandu")
    assert await correct_timezone_if_drifted(device, "5.8", now=1000.0) is False
    assert device.command_queue == []


async def test_repeated_reports_wait_for_the_echo_instead_of_stacking_pushes():
    device = _device()
    device.command_queue.append({"method": "thing.service.property.set",
                                 "params": {"fountainMode": 1}})

    assert await correct_timezone_if_drifted(device, "0.0", now=1000.0) is True
    assert await correct_timezone_if_drifted(device, "0.0", now=1030.0) is False
    assert len(_timezone_pushes(device)) == 1
    assert {"fountainMode": 1} in [c["params"] for c in device.command_queue]

    # Still wrong ten minutes later: try again, still only one queued.
    assert await correct_timezone_if_drifted(device, "0.0", now=1000.0 + 601) is True
    assert len(_timezone_pushes(device)) == 1


@pytest.mark.parametrize("zone", [None, "", "nan", {}, []])
async def test_a_report_without_a_usable_zone_is_not_pushed(zone):
    device = _device()
    assert await correct_timezone_if_drifted(device, zone, now=1000.0) is False
    assert device.command_queue == []


async def test_the_state_report_hook_pushes_without_home_assistant():
    device = _device()
    await _on_state_report(None, None, device, {"timezone": "0.0"})
    assert _timezone_pushes(device)


@pytest.mark.parametrize("config", [{}, {"locale": "not/a-zone"},
                                     {"reported_timezone": "invalid"}])
async def test_a_server_fallback_never_overrides_the_live_report(config, monkeypatch):
    monkeypatch.setattr("petkit_local.devices.base.local_offset_hours", lambda: 0.0)
    device = Device(device_type="w7h", petkit_id=1, config=config)
    assert device.resolved_timezone == (0.0, "server")
    assert await correct_timezone_if_drifted(device, "1.0", now=1000.0) is False
    assert device.command_queue == []
    assert device.timezone_correction is None


@pytest.mark.parametrize(("config", "expected"), [
    ({"timezone": -7, "locale": "Asia/Kolkata", "reported_timezone": 0}, (-7, "override")),
    ({"locale": "Asia/Kolkata", "reported_timezone": 0}, (5.5, "locale")),
    ({"locale": "not/a-zone", "reported_timezone": -4}, (-4, "device")),
    ({"reported_timezone": 0}, (0, "device")),
])
async def test_correction_uses_the_same_resolved_source_as_device_info(config, expected):
    device = Device(device_type="w7h", petkit_id=1, config=config)
    assert device.resolved_timezone == expected
    assert device.timezone_offset == expected[0]
    assert await correct_timezone_if_drifted(device, "9.0", now=1000.0) is True
    assert _timezone_pushes(device)[0]["params"]["timezone"] == f"{expected[0]:g}"


async def test_an_mqtt_device_is_published_to_not_queued():
    device = _device()
    device.mqtt_connected = True
    bridge = MQTTBridge(DeviceRegistry())
    bridge._client = FakeMqttClient()
    assert await correct_timezone_if_drifted(device, "0", bridge, now=1000) is True
    assert device.command_queue == []
    [(topic, raw, _)] = bridge._client.published
    assert topic.endswith("/thing/service/property/set")
    assert json.loads(raw)["params"] == {"timezone": "5.5"}


async def test_a_failed_mqtt_publish_retains_the_heartbeat_fallback():
    device = _device()
    device.mqtt_connected = True
    bridge = MQTTBridge(DeviceRegistry())  # disconnected; publish raises
    assert await push_timezone(device, 5.5, bridge) == "heartbeat-queue"
    assert _timezone_pushes(device)[0]["params"] == {"timezone": "5.5"}


async def test_replacing_a_queued_zone_preserves_other_settings_and_encoded_commands():
    device = _device()
    device.command_queue.extend([
        {"method": "thing.service.property.set",
         "params": {"timezone": "2", "fountainMode": 1}},
        {"method": "thing.service.property.set", "params": {"timezone": "3"}},
        '{"method":"thing.service.start","params":{}}',
    ])
    assert await push_timezone(device, 5.5) == "heartbeat-queue"
    assert device.command_queue[0]["params"] == {"fountainMode": 1}
    assert isinstance(device.command_queue[1], str)
    assert device.command_queue[2]["params"] == {"timezone": "5.5"}


async def test_mqtt_property_posts_trigger_correction_without_ha():
    registry = DeviceRegistry()
    device = registry.get_or_create(1, "t5", serial_number="SN")
    device.config["reported_timezone"] = 5.5
    device.mqtt_connected = True
    bridge = MQTTBridge(registry)
    bridge._client = FakeMqttClient()
    await bridge._handle_event(device, "property", {"params": {"timezone": "0.0"}})
    assert device.command_queue == []
    commands = [json.loads(raw) for topic, raw, _ in bridge._client.published
                if topic.endswith("/thing/service/property/set")]
    assert [c["params"] for c in commands] == [{"timezone": "5.5"}]


async def test_http_report_hook_uses_mqtt_for_a_connected_device():
    device = _device()
    device.mqtt_connected = True
    bridge = MQTTBridge(DeviceRegistry())
    bridge._client = FakeMqttClient()
    await _on_state_report(None, None, device, {"timezone": "0"}, bridge=bridge)
    assert bridge._client.published
    assert device.command_queue == []


async def test_three_non_echoed_attempts_stop_and_warn_once(caplog):
    device = _device()
    for now in (1000, 1600, 2200):
        assert await correct_timezone_if_drifted(device, "0", now=now) is True
        device.pop_commands()
    for now in (2230, 2800, 3400):
        assert await correct_timezone_if_drifted(device, "0", now=now) is False
    assert device.command_queue == []
    assert device.timezone_correction.attempts == 3
    assert sum("stopping automatic correction" in r.message for r in caplog.records) == 1
    assert await push_timezone(device, 5.5) == "heartbeat-queue"
    assert _timezone_pushes(device)[0]["params"] == {"timezone": "5.5"}


async def test_a_matching_echo_allows_a_later_drift_to_be_corrected():
    device = _device()
    assert await correct_timezone_if_drifted(device, "0", now=1000)
    device.pop_commands()
    assert not await correct_timezone_if_drifted(device, "5.5", now=1030)
    assert device.timezone_correction is None
    assert await correct_timezone_if_drifted(device, "0", now=1060)


async def test_a_new_target_does_not_inherit_the_previous_targets_cap():
    device = _device()
    for now in (1000, 1600, 2200):
        assert await correct_timezone_if_drifted(device, "0", now=now)
    device.config["timezone"] = 6
    assert await correct_timezone_if_drifted(device, "0", now=2230)
    assert device.timezone_correction.attempts == 1
    assert _timezone_pushes(device)[0]["params"] == {"timezone": "6"}


async def test_runtime_state_does_not_survive_removal_or_serialization():
    registry = DeviceRegistry()
    first = registry.get_or_create(1, "w7h")
    first.config["reported_timezone"] = 5.5
    assert await correct_timezone_if_drifted(first, "0", now=1000)
    saved = first.to_dict()
    assert "timezone_correction" not in saved
    assert Device.from_dict(saved).timezone_correction is None
    registry.remove(1)
    second = registry.get_or_create(1, "w7h")
    second.config["reported_timezone"] = 5.5
    assert await correct_timezone_if_drifted(second, "0", now=1030)


async def test_two_public_signup_report_cycles_inside_the_retry_window():
    registry = DeviceRegistry()
    app = create_app(registry, {"api_url": "http://server/6/"})
    app["on_signup"] = partial(_on_signup, None)
    app["on_state_report"] = partial(_on_state_report, None, None)
    client = TestClient(TestServer(app))
    await client.start_server()
    headers = {"X-Device": "id=1&sn=SN"}
    try:
        for _ in range(2):
            response = await client.post("/6/w7h/dev_signup", headers=headers,
                                         data={"timezone": "5.5", "locale": "Asia/Kolkata"})
            assert response.status == 200
            response = await client.post("/6/w7h/dev_state_report", headers=headers,
                                         data={"state": json.dumps({"timezone": "0.0"})})
            assert response.status == 200
            device = registry.get(1)
            assert len(device.pop_commands()) == 1
            # A duplicate in the same boot does not send another write.
            await client.post("/6/w7h/dev_state_report", headers=headers,
                               data={"state": json.dumps({"timezone": "0.0"})})
            assert device.command_queue == []
    finally:
        await client.close()


async def test_concurrent_reports_reserve_only_one_mqtt_attempt():
    started, release = asyncio.Event(), asyncio.Event()

    class SlowBridge:
        async def publish_to_device(self, *_):
            started.set()
            await release.wait()

    device = _device()
    device.mqtt_connected = True
    first = asyncio.create_task(
        correct_timezone_if_drifted(device, "0", SlowBridge(), now=1000))
    try:
        await started.wait()
        assert not await correct_timezone_if_drifted(device, "0", SlowBridge(), now=1000)
    finally:
        release.set()
        await first
    assert device.timezone_correction.attempts == 1
