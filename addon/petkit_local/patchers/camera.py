"""Local camera streaming patcher.

Replaces the Agora P2P client with tserver (local HTTP video streamer, port
80, zero auth) by bind-mounting one over the other, so the stock app_start.sh
line `./agora &` starts the local streamer instead. tserver reads frames from
the same POSIX shm `media` writes to, so it needs media running and nothing
else.

No binary patching and no file of ours on the device — the whole patch is the
bind-mount line in the /system/app_init.sh wrapper, plus, on the W7H watchdog
build, a loop that checks in for agora (`AGORA_CHECKIN_*` in common.py).

**Never point Home Assistant's Generic Camera straight at the device.** HA's
`stream` component opens the URL with PyAV and `av.open()` segfaults inside
libav, killing the whole HA process. Put go2rtc in between; `STREAM_PATHS`
below records the evidence and the second reason for it.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from petkit_local.devices.base import Device

log = logging.getLogger(__name__)

#: What tserver serves, on port 80. Measured against a T5 on 2026-07-31:
#:
#: * `main.flv?audio=1` / `sub.flv?audio=1` — H.264 (1056² and 528²) plus AAC at
#:   16 kHz mono, verified over sustained reads both directly and through
#:   go2rtc. These are the ones to hand out.
#: * `main.ts` / `sub.ts` — dropped. They announce `sample_rate=0, channels=0`
#:   and list every stream twice, so nothing loads them. The FLV endpoints
#:   reported a valid `16000/1` every time they were sampled.
#: * The `audio` parameter is required, not optional — `sub.flv` with no query
#:   at all resets the connection.
#:
#: The PyAV segfault in the module docstring was observed twice, with and
#: without audio, so audio is not the trigger; it is not reproducible with
#: `av.open` in isolation, so it needs HA's threading context. go2rtc reads
#: this FLV and republishes clean RTSP.
#:
#: tserver also needs **seconds between connections**: opened back to back it
#: refuses the second, the same pair six seconds apart both succeed, and three
#: concurrent opens serve one and refuse two. So one long-lived reader in front
#: of it is not a nicety — it is what makes the camera usable for more than one
#: viewer.
#:
#: These are paths rather than one URL because tserver answers the same stream on
#: every path it is given: `/snapshot.jpg`, `/jpeg` and `/cgi-bin/snapshot.cgi`
#: all return `video/x-flv`. There is no still-image endpoint to point anything
#: at, which is why HA gets a URL for its own camera stack rather than an MQTT
#: camera entity fed with JPEGs.
STREAM_PATHS = {
    "flv": "main.flv?audio=1",
    "sub_flv": "sub.flv?audio=1",
}


def stream_urls(device: Device) -> dict[str, str]:
    """This device's local stream URLs, or `{}` when they would not answer.

    Gated on `state["streamAvailable"]` — the verdict of an actual probe, from
    `media/go2rtc.py::probe_stream` — and NOT on `config["active_patchers"]`.
    That distinction is the point: `active_patchers` is what we recorded, and a
    factory reset or an app OTA wipes /system and the patch with it while our
    JSON still says applied. Equally, a device could be serving a stream nobody
    recorded. Publishing a URL that then refuses the connection is worse than
    publishing none — it reads as a broken camera rather than as a patch that
    was never applied.

    The IP is checked too, because it only ever arrives in a state report, so a
    device we have not heard from has no address to build.
    """
    ip = (device.state or {}).get("ip", "")
    if not ip or not (device.state or {}).get("streamAvailable"):
        return {}
    return {name: f"http://{ip}/{path}" for name, path in STREAM_PATHS.items()}


PATCHER_INFO = {
    "id": "camera",
    "name": "Local Camera Streaming",
    "description": (
        "Replaces the Agora cloud video streaming client with tserver, a local "
        "HTTP video server built into the firmware. This gives you direct LAN "
        "access to the camera feed without going through PetKit's cloud.\n\n"
        "Once applied, the add-on's bundled go2rtc republishes the feed as RTSP "
        "and the URL to use appears above - that is the one to give Home "
        "Assistant's Generic Camera, and it is also the Camera Stream URL "
        "sensor.\n\n"
        "The device's own addresses are listed too, for VLC or your own tooling. "
        "Do NOT give those to Home Assistant: its stream component opens them "
        "with PyAV, which segfaults inside libav and restarts the whole of HA. "
        "go2rtc exists here to stand between the two, and it also spares "
        "the device a connection per viewer by keeping just one open.\n\n"
        "What it does: bind-mounts /app/bin/tserver over /app/bin/agora, so the "
        "stock startup line './agora &' launches the local streamer. Only the "
        "boot wrapper changes; removing the patch and rebooting restores "
        "Agora exactly.\n\n"
        "The firmware watchdog: every stock process checks in with it by "
        "calling its own *_feed_dog; tserver is a different binary and never "
        "does, so the watchdog decides Agora died. Measured on a T5 over 16 "
        "minutes: it logs 'kill agora' and 'reboot agora' once a minute, but "
        "the kill never lands, tserver keeps the same PID, its memory stays "
        "flat and the device does not reboot. On a W7H fountain (firmware 456) "
        "the kill does land and the stream dropped every minute, so there the "
        "patch also checks in for Agora from the boot script. This is enabled "
        "only for the verified watchdog binary and shared-memory layout. "
        "If tserver grows past 12 MB RSS, check-ins pause while no stream "
        "connection is open so the watchdog can reclaim its memory. An "
        "established viewer keeps check-ins active; a viewer arriving during "
        "an idle restart may need to retry.\n\n"
        "The PetKit app's live view will stop working - use the RTSP URL above "
        "instead.\n\n"
        "One rough edge: the device needs a few seconds between connections, so "
        "reopening the stream immediately after closing it can fail once. "
        "Waiting a moment and retrying works, and normal viewing does not hit "
        "it."
    ),
    # No extra helper file; removal drops the camera block, including its
    # check-in loop, from the wrapper and takes effect after reboot.
    "files": [],
    # No architecture: this bind-mounts one binary the device already ships
    # over another. Both are whatever the device was built with.
    "arch": None,
    # Conservative UI figure: what to tell the user BEFORE we know the model.
    # writes no file of its own, but every patcher rewrites
    # /system/app_init.sh — so the floor is the wrapper plus margin, not zero.
    "needs_bytes": 131072,
}
