"""Shared helpers for device patchers.

- run_cmd delivery over MQTT or the heartbeat queue, whichever the device is
  actually using (`build_run_cmd` documents the envelope)
- Temporary httpd on the device for file download
- File staging on the add-on for device wget
- Unified app_init.sh wrapper generation
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from collections.abc import Sequence
from typing import Any

import aiohttp

from petkit_local.devices.base import Device
log = logging.getLogger(__name__)

DEVICE_HTTPD_PORT = 8888
#: A SECOND httpd, rooted at /tmp, used only to read command output back.
#: It cannot share 8888: that instance is rooted at /app/bin, which is
#: read-only squashfs, so nothing can be written there for it to serve.
DEVICE_PROBE_PORT = 8889
STAGE_DIR = "/tmp/petkit_patcher"

#: app_init.sh is ~1 KB today and every patcher rewrites it.
WRAPPER_RESERVE_BYTES = 4096
#: Slack for a jffs2 erase block and its metadata. Kept small deliberately:
#: jffs2 compresses on write, so `df` UNDER-reports what will actually fit and
#: a generous margin would refuse patches that would have succeeded.
SPACE_MARGIN_BYTES = 65536


class InsufficientDeviceSpace(RuntimeError):
    """Not enough free space on the device for a patch that was about to write."""

# Patched files live in the writable boot override area. Ingenic boards use
# /system; Axera ARM boards use /opt. Codename cannot decide this because D4SH
# ships with both board families under the same type string, so patcher runs
# probe the device filesystem before choosing.
APP_INIT_WRAPPER = "/system/app_init.sh"
OPT_APP_INIT_WRAPPER = "/opt/app_init.sh"
PATCH_STORAGE_CONFIG_KEY = "patch_storage_dir"


def set_patch_storage_dir(device: Device, storage_dir: str) -> None:
    """Remember the probed writable patch storage directory for this device."""
    if storage_dir not in ("/system", "/opt"):
        raise ValueError(f"unsupported patch storage directory: {storage_dir}")
    device.config[PATCH_STORAGE_CONFIG_KEY] = storage_dir


async def detect_patch_storage_dir(device: Device, device_ip: str, *,
                                   bridge: Any = None,
                                   timeout: float = 60.0) -> str:
    """Detect whether patches should persist under /system or /opt.

    The persistent app storage carries user.conf: Ingenic exposes it under
    /system/user.conf, while Axera exposes it under /opt/user.conf. Do not fall
    back to codename guesses here: D4SH may be either MIPS/Ingenic or ARM/Axera.
    """
    command = (
        "[ -f /system/user.conf ] && echo STORAGE /system; "
        "[ -f /opt/user.conf ] && echo STORAGE /opt"
    )
    text = await run_cmd_capture(device, device_ip, command, bridge=bridge,
                                 timeout=timeout)
    for storage_dir in ("/system", "/opt"):
        if f"STORAGE {storage_dir}" in (text or ""):
            set_patch_storage_dir(device, storage_dir)
            return storage_dir

    raise RuntimeError(
        f"could not determine writable patch storage for device {device.petkit_id}: "
        "neither /system/user.conf nor /opt/user.conf exists")


def uses_opt_boot(device: Device | None) -> bool:
    """Whether this device uses the Axera-style /opt app storage."""
    if device is not None:
        storage_dir = device.config.get(PATCH_STORAGE_CONFIG_KEY)
        if storage_dir in ("/system", "/opt"):
            return storage_dir == "/opt"
    return False


def app_init_wrapper_path(device: Device | None = None) -> str:
    """Where this device's generated init wrapper lives."""
    return OPT_APP_INIT_WRAPPER if uses_opt_boot(device) else APP_INIT_WRAPPER


def patch_storage_dir(device: Device | None = None) -> str:
    """The writable partition patched files are kept on, per device."""
    return "/opt" if uses_opt_boot(device) else "/system"


def patched_file_path(filename: str, device: Device | None = None) -> str:
    """Where a patched copy of `filename` is stored on the device."""
    return f"{patch_storage_dir(device)}/{filename}"


def patcher_device_files(pinfo: dict[str, Any],
                         device: Device | None = None) -> list[str]:
    """Absolute on-device paths for a patcher's declared files.

    Binary/cert patchers declare bare names because their storage directory is
    model-dependent: /system on Ingenic, /opt on Axera. Absolute paths are passed
    through as a defensive guard for future patchers with fixed locations.
    """
    result: list[str] = []
    for name in pinfo["files"]:
        result.append(name if name.startswith("/") else patched_file_path(name, device))
    return result


def build_run_cmd(command: str) -> str:
    """The `msgType` envelope carrying one shell command.

    Uses PATH 2 of the firmware's run_cmd handling - a direct `system()` call
    with no uptime guard and no length limit - which is why the proxy blocks
    this field coming from upstream (see http/proxy.py).
    """
    return json.dumps({
        "msgType": 0,
        "user_cmd": {"run_cmd": command},
    })


async def send_run_cmd(device: Device, command: str, bridge: Any = None) -> str:
    """Deliver a shell command over whichever transport the device is using.

    A device that has joined MQTT STOPS polling the HTTP heartbeat (confirmed
    on a T5: quiet ~40s after CONNECT, for as long as the session lives), so a
    command left on the heartbeat queue is never drained and every patcher step
    times out — which is why this picks the transport rather than assuming one.

    MQTT delivery goes to `/{pk}/{dn}/user/get`, the one downstream topic that
    carries the same `msgType` envelope the heartbeat does.

    **Confirmed on hardware (2026-07-29): a T5 executes a `user_cmd.run_cmd`
    delivered this way.** Worth recording that the real cloud has never been
    seen doing it — 0 run_cmd frames in 1152 captured proxy records — so this is
    our use of the transport, not a reproduction of PetKit's.

    Returns:
        The transport used, "mqtt" or "heartbeat" — logged by the caller so a
        failed run can be read afterwards without guessing which path it took.
    """
    content = build_run_cmd(command)

    on_mqtt = bool(getattr(device, "mqtt_connected", False)) and bridge is not None
    if on_mqtt:
        try:
            if await bridge.publish_user_get(device, json.loads(content)):
                log.info("Patcher: run_cmd over MQTT for device %d: %.120s",
                         device.petkit_id, command)
                return "mqtt"
        except Exception:
            # Never fatal: falling through to the queue is strictly better than
            # aborting a multi-step patcher run halfway.
            log.exception("Patcher: MQTT run_cmd failed for device %d, queueing instead",
                          device.petkit_id)

    device.command_queue.append(content)
    log.info("Patcher: queued run_cmd for device %d: %.120s", device.petkit_id, command)
    return "heartbeat"


async def wait_for_heartbeat(device: Device, timeout: float = 30) -> bool:
    """Wait until the device picks up a QUEUED command via heartbeat.

    Returns True immediately when the queue is empty, which is also the MQTT
    case: `send_run_cmd` published rather than queued, and that transport has
    no delivery report to wait for — the broker accepts a publish whether or
    not anyone is subscribed. So a True here means "nothing is still waiting to
    be collected", never "the device ran it"; the patcher steps prove that for
    themselves by downloading the result back off the device.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not device.command_queue:
            return True
        await asyncio.sleep(2)
    return not device.command_queue


async def download_from_device(device_ip: str, filename: str, timeout: float = 30,
                               port: int = DEVICE_HTTPD_PORT) -> bytes:
    """Download a file from a temporary busybox httpd on the device.

    Args:
        port: Which httpd to ask. The default is the file server rooted at
            /app/bin; `run_cmd_capture` uses `DEVICE_PROBE_PORT`, rooted at
            /tmp, because command output cannot be written under /app/bin.
    """
    url = f"http://{device_ip}:{port}/{filename}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"GET {url} returned {resp.status}")
            data = await resp.read()
            log.info("Patcher: downloaded %s from device (%d bytes)", filename, len(data))
            return data


async def run_cmd_capture(device: Device, device_ip: str, command: str, *,
                          bridge: Any = None, port: int = DEVICE_PROBE_PORT,
                          timeout: float = 60.0, poll_interval: float = 3.0) -> str | None:
    """Run a shell command on the device and return what it printed.

    `send_run_cmd` is fire-and-forget and `wait_for_heartbeat` only reports that
    the queue drained, so until now nothing here could read a command's OUTPUT.
    This closes that gap the only way the device allows: the command redirects
    into /tmp (tmpfs — /app/bin is read-only squashfs), a second busybox httpd
    serves /tmp, and we poll for the file.

    Three properties make the result trustworthy despite the transport having no
    delivery acknowledgement:

    * **A stale file cannot be misread.** The filename carries 16 random hex
      characters minted in THIS call, and the same token is repeated in a
      sentinel line inside the file. A leftover from an earlier run has neither.
    * **A partial file cannot be misread.** Output is written to `.part` and
      renamed — atomic within one tmpfs — before the httpd is started, and the
      reader additionally requires the trailing sentinel.
    * **stderr is kept** (`2>&1`), so `df: /system: No such file` ends up in the
      returned text and therefore in the operator's log, instead of vanishing.

    Returns:
        The captured output with the sentinel stripped, or None if the device
        never produced it within `timeout`. Never raises for a device-side
        failure — the caller decides what an unknown result means.
    """
    nonce = secrets.token_hex(8)
    name = f"pk_{nonce}"
    sentinel = f"__PK_END_{nonce}"
    out = f"/tmp/{name}"

    # One command: run, mark the end, publish atomically, then serve. Starting
    # the httpd last means a 200 can only ever be a complete file. Re-issuing
    # `busybox httpd` when one is already bound simply fails and leaves the
    # running one serving, which is exactly what we want.
    await send_run_cmd(device, (
        f"{{ {command}; }} > {out}.part 2>&1; "
        f"echo {sentinel} >> {out}.part; "
        f"mv {out}.part {out}; "
        f"busybox httpd -p {port} -h /tmp"
    ), bridge)

    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            await asyncio.sleep(poll_interval)
            try:
                raw = await download_from_device(device_ip, name, timeout=10, port=port)
            except Exception:
                continue  # not written yet, or the httpd has not come up
            text = raw.decode("utf-8", "replace")
            if text.rstrip().endswith(sentinel):
                log.info("Patcher: captured %d bytes of output for device %d",
                         len(raw), device.petkit_id)
                return text.rstrip()[: -len(sentinel)].rstrip("\n")
        log.warning("Patcher: no output from device %d within %.0fs for: %.80s",
                    device.petkit_id, timeout, command)
        return None
    finally:
        # Best-effort: a leaked file is 1 KB of tmpfs and a leaked httpd is
        # harmless, but neither should outlive the run if the device is healthy.
        await send_run_cmd(device, f"rm -f {out} {out}.part", bridge)


def parse_df_available_bytes(text: str, mount: str = "/system") -> int | None:
    """Available bytes on the filesystem holding `mount`, from `df -k` output.

    Parsed here rather than on the device because busybox on these boxes is
    stripped — no `cut`, `head`, `tr` or `grep -E` — so an `awk`/pipeline
    one-liner would be a guess about which applets survived. A bare `df` always
    works.

    Fields are read from the RIGHT (`Available` is third from the end), because
    busybox wraps a long filesystem name onto its own line, which shifts every
    column when counting from the left.

    Returns:
        Available bytes, or None if `mount` is absent or nothing parses — which
        callers must treat as "unknown", not as "full".
    """
    if not text:
        return None
    best: tuple[int, int] | None = None  # (len(mountpoint), available_bytes)
    pending: list[str] = []
    for line in text.splitlines():
        fields = pending + line.split()
        pending = []
        if not fields or fields[0] in ("Filesystem", "df:"):
            continue
        # A wrapped filesystem name is a line of its own; hold it for the next.
        if len(fields) < 6:
            pending = fields
            continue
        mounted_on = fields[-1]
        if not (mount == mounted_on or mount.startswith(mounted_on.rstrip("/") + "/")):
            continue
        try:
            available_kb = int(fields[-3])
        except ValueError:
            continue
        # Longest matching mountpoint wins: /system beats / for /system/foo.
        if best is None or len(mounted_on) > best[0]:
            best = (len(mounted_on), available_kb * 1024)
    return best[1] if best else None


def parse_wc_c_sizes(text: str) -> dict[str, int]:
    """Map path -> size from `wc -c` output.

    `wc -c` is used rather than `ls -l` because its output is two fields in a
    fixed order regardless of locale, permissions or timestamp format. A file
    that does not exist produces an error line and is simply absent from the
    result, which is the correct reading: nothing there to reclaim.
    """
    sizes: dict[str, int] = {}
    for line in (text or "").splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[1] == "total":
            continue
        try:
            sizes[fields[1]] = int(fields[0])
        except ValueError:
            continue
    return sizes


def required_free_bytes(write_bytes: int, existing_bytes: int = 0) -> int:
    """Free space a patch needs, crediting the file it will overwrite.

    The credit is not an optimisation. Re-applying the mqtt patcher overwrites
    an existing 1.4 MB /system/ctrl_patched, and that space comes back; without
    the credit the check would fail on precisely the devices where the patch is
    already installed and working.
    """
    return max(0, write_bytes - existing_bytes) + WRAPPER_RESERVE_BYTES + SPACE_MARGIN_BYTES


def _fmt_bytes(n: int) -> str:
    """Byte count for a progress line the operator reads."""
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


async def ensure_space_for(device: Device, device_ip: str, *, write_bytes: int,
                           targets: Sequence[str] = (), bridge: Any = None,
                           mount: str = "/system", timeout: float = 60.0) -> str:
    """Check the device has room for a patch, before anything is written.

    Reads `df` and the current sizes of `targets` in ONE round trip, so a
    re-apply is credited the space its own previous output already occupies.

    Returns:
        A human-readable line for the operator's log — including the case where
        the probe did not answer.

    Raises:
        InsufficientDeviceSpace: Only when the free space is KNOWN and is too
            small. A probe that fails to answer yields a warning and lets the
            patch continue: "unknown" is not evidence of danger, and refusing to
            patch a device whose busybox lacks `df` would break a working
            feature to enforce a check that never ran.
    """
    quoted = " ".join(targets)
    command = f"df -k {mount}" + (f"; wc -c {quoted}" if quoted else "")
    text = await run_cmd_capture(device, device_ip, command, bridge=bridge, timeout=timeout)

    if text is None:
        return (f"WARNING: could not read {mount} free space (no answer from the device) "
                "— proceeding without the space check")

    free = parse_df_available_bytes(text, mount)
    if free is None:
        return (f"WARNING: could not parse {mount} free space from the device's df output "
                "— proceeding without the space check")

    existing = sum(parse_wc_c_sizes(text).get(t, 0) for t in targets)
    needed = required_free_bytes(write_bytes, existing)
    if free < needed:
        raise InsufficientDeviceSpace(
            f"{mount} has {_fmt_bytes(free)} free but this patch needs "
            f"{_fmt_bytes(needed)} ({_fmt_bytes(write_bytes)} to write"
            + (f", {_fmt_bytes(existing)} reclaimed from the current version" if existing else "")
            + f", plus {_fmt_bytes(WRAPPER_RESERVE_BYTES + SPACE_MARGIN_BYTES)} for the boot "
            "wrapper and filesystem overhead)"
        )
    return f"{mount}: {_fmt_bytes(free)} free, needs {_fmt_bytes(needed)}"


def _device_stage_dir(device_id: int) -> str:
    return os.path.join(STAGE_DIR, str(device_id))


def stage_file(filename: str, data: bytes, device_id: int) -> str:
    """Write a patched file to a per-device staging subdirectory."""
    d = _device_stage_dir(device_id)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, filename)
    with open(path, "wb") as f:
        f.write(data)
    log.info("Patcher: staged %s for device %d (%d bytes, md5=%s)",
             filename, device_id, len(data), hashlib.md5(data).hexdigest())
    return path


def cleanup_staged(filename: str, device_id: int) -> None:
    """Drop a staged file once the device has fetched it.

    A file that is already gone is not an error: the patcher's cleanup runs on
    both the success and failure paths.
    """
    path = os.path.join(_device_stage_dir(device_id), filename)
    try:
        os.unlink(path)
    except OSError:
        pass


def get_staged_path(filename: str, device_id: int) -> str | None:
    """Path of a staged file, or None if it is not staged right now.

    Note that `/patcher/download` does NOT go through this: it resolves the
    name against `STAGE_DIR` with `safe_join` (see http/server.py), so this
    helper exists for callers that need to ask without serving.
    """
    path = os.path.join(_device_stage_dir(device_id), filename)
    return path if os.path.isfile(path) else None


def md5hex(data: bytes) -> str:
    """MD5 of `data` as lowercase hex - a transfer check, never a security one."""
    return hashlib.md5(data).hexdigest()


# --------------- Unified /system/app_init.sh wrapper -------------------------
# Boot chain: kernel → system_init.sh → if /system/app_init.sh exists, run it
# INSTEAD of /app/script/app_init.sh (stock, read-only squashfs).
#
# The wrapper does bind-mounts for all active patches BEFORE sourcing the stock
# init, so all patched binaries are in place when app_start.sh starts processes.
# This avoids the test_case_* timing problem (test_case hooks run AFTER
# processes are already loaded into memory).

WRAPPER_HEADER_TEMPLATE = """\
#!/bin/sh
# {wrapper} - petkit-local patcher wrapper
# Auto-generated. Bind-mounts patched files before running the stock init.
"""

WRAPPER_FOOTER = """\

# Run the stock init (which starts all processes using the patched files)
. /app/script/app_init.sh
"""

# Per-patcher bind-mount blocks. Each block is formatted with `store`; write
# literal shell braces as `{{` and `}}`.
# Each is a tuple (patcher_id, shell_lines).
# Only active patchers are included in the generated wrapper.
#
# Camera bind-mounts the real tserver ELF over agora, not a shell placeholder.
# It starts through stock app_start.sh and /proc/PID/exe names /app/bin/agora.
# This preserves the name used by the stock watchdog's kill/restart commands.
#
# On the W7H fountain (firmware 456), process identity alone is not enough.
# Its watchdog, read in
# Ghidra and confirmed on the live device, keeps one counter per supervised
# process in the POSIX shm /config_shm, adds 1 to each every 2 s, and once a
# counter passes 29 runs `killall -9 <name>` and restarts it: about every 60 s.
# A stock process checks in by writing 0 to its own counter (logUpload's
# logUpload_feed_dog does it every 3 s). tserver never writes agora's, so the
# watchdog killed it every minute and every viewer's stream dropped. There the
# lsof line is a file-descriptor leak check: run hourly, it kills a process only when
# more than 511 lines match. So on that watchdog build the camera block below
# also checks in for agora, see AGORA_CHECKIN_*.

#: Where the W7H watchdog keeps agora's counter, and the size of that shared
#: memory on the build it was read from. The loop writes only while both hold.
AGORA_CHECKIN_SHM = "/dev/shm/config_shm"
AGORA_CHECKIN_SHM_BYTES = 15328
AGORA_CHECKIN_OFFSET = 0x30B4
#: md5 of each /app/bin/watchdog build whose counter layout was confirmed.
#: On any other build the loop never starts and the device behaves as before.
AGORA_CHECKIN_WATCHDOG_MD5 = (
    "dda400d6df9b38f226fba9b7776921d8",  # W7H, firmware 456
)
#: The watchdog kills after 30 ticks of 2 s; logUpload checks in every 3 s.
AGORA_CHECKIN_PERIOD_S = 5
#: tserver's RSS grew from 4.8 to 8.4 MB during longer uptime. Three controlled
#: reconnects added a frame-buffer mapping and 60-64 kB anonymous RSS each.
#: Total RSS includes repeated mappings of the same shared pages; it is not
#: an equivalent loss of physical RAM. A fresh tserver holds about 2.4 MB.
#: Past this threshold, and only while no stream connection is open, the loop
#: skips its check-in so the watchdog reclaims the retained resources.
AGORA_RECYCLE_RSS_KB = 12288
#: tserver's port, as /proc/net/tcp prints it (hex). State 01 is established.
AGORA_STREAM_PORT_HEX = f"{80:04X}"

# The check-in runs only while a process named agora exists, so a tserver that
# died is still restarted by the watchdog within a minute. It cannot see a
# tserver that runs but hangs. No backslash, % or brace here: the camera block
# goes through str.format(store=...), and the wrapper reaches the device as a
# printf format string (build_wrapper_upload_cmd).
AGORA_CHECKIN_BLOCK = (
    "# Check in for agora with the W7H watchdog, which otherwise kills tserver\n"
    "# every 60 s. Only on the watchdog build whose counters were read.\n"
    'case "$(md5sum /app/bin/watchdog 2>/dev/null)" in\n'
    "  " + "|".join(f"{md5}*" for md5 in AGORA_CHECKIN_WATCHDOG_MD5) + ")\n"
    "    ( while true; do\n"
    "        p=$(pidof agora)\n"
    f'        if [ -n "$p" ] && [ "$(wc -c 2>/dev/null < {AGORA_CHECKIN_SHM})" = '
    f"{AGORA_CHECKIN_SHM_BYTES} ]; then\n"
    "          kb=$(grep VmRSS /proc/$p/status 2>/dev/null | tr -dc 0-9)\n"
    f'          grep -q ":{AGORA_STREAM_PORT_HEX} [0-9A-F]*:[0-9A-F]* 01 " '
    "/proc/net/tcp 2>/dev/null\n"
    "          viewers=$?\n"
    f'          if [ -n "$kb" ] && [ "$kb" -gt {AGORA_RECYCLE_RSS_KB} ] '
    '&& [ "$viewers" = 1 ]; then\n'
    "            : # above the limit and confirmed idle: let the watchdog restart it\n"
    "          else\n"
    f"            dd if=/dev/zero of={AGORA_CHECKIN_SHM} bs=4 "
    f"seek={AGORA_CHECKIN_OFFSET // 4} count=1 conv=notrunc 2>/dev/null\n"
    "          fi\n"
    "        fi\n"
    f"        sleep {AGORA_CHECKIN_PERIOD_S}\n"
    "      done ) &\n"
    "    ;;\n"
    "esac\n"
)

BIND_MOUNT_TEMPLATES = {
    "mqtt": (
        "# MQTT TLS bypass: patched ctrl accepts any broker certificate\n"
        "mount --bind {store}/ctrl_patched /app/bin/ctrl\n"
    ),
    "cloud": (
        "# Local storage: patched cloud accepts LAN IPs + skips TLS verify\n"
        "mount --bind {store}/cloud_patched /app/bin/cloud\n"
    ),
    "cacert": (
        "# CA cert: cloud trusts our self-signed bucket certificate\n"
        "mount --bind {store}/ca_patched.crt /app/bin/ca.crt\n"
    ),
    "camera": (
        "# Local camera: stock app_start.sh runs ./agora, which is tserver now\n"
        "mount --bind /app/bin/tserver /app/bin/agora\n"
        + AGORA_CHECKIN_BLOCK
    ),
    # ssh: no bind-mount needed (dropbear is not replacing a stock binary)
}

#: Two-way talk (intercom) audio sink. The panel's `/api/devices/{id}/talk`
#: WebSocket transcodes the browser mic to 16 kHz mono ADTS-AAC and streams it
#: to this TCP port on the device; the sink script below feeds it to the
#: firmware's own `pktool play_aac`, which owns the IMP speaker path. See
#: patchers/talk.py.
TALK_TCP_PORT = 9010
#: The sink script is installed ONCE into the patch store (persistent, like
#: dropbear) — so `talk` declares it in `files` and removal deletes it. Nothing
#: is written to /tmp at boot; only the per-connection FIFO the script makes at
#: runtime is ephemeral (named by the handler's PID).
TALK_SINK_NAME = "pktalk_sink.sh"
#: Body of that sink script. `nc -e /bin/sh <this>` runs it once per connection:
#: it makes a private FIFO, starts `pktool play_aac` reading it, and copies the
#: socket into the FIFO until the client hangs up. `$$` is the running sh's PID,
#: so concurrent connections never collide on the pipe name.
TALK_SINK_SCRIPT = (
    "#!/bin/sh\n"
    "F=/tmp/pktalk.$$\n"
    "rm -f $F; mknod $F p\n"
    # /app/bin FIRST: that is where `libbase.so` (and pktool's other deps) live on
    # the D4SH — the firmware's own `media` process runs with LD_LIBRARY_PATH=
    # /app/bin:/usr/lib. Without it pktool dies at load ("libbase.so: cannot open
    # shared object file"), which also WEDGED the listener: a pktool that never
    # opens the FIFO leaves `cat > $F` blocked forever, and `nc -e` has replaced
    # the accept loop, so one connection killed the port. The rest of the path is
    # kept as a fallback for models that place the lib elsewhere (T6).
    "LD_LIBRARY_PATH=/app/bin:/syslib/lib:/app/lib:/system/lib:/usr/lib:/lib "
    "/app/bin/pktool play_aac $F &\n"
    "cat > $F\n"
    "rm -f $F\n"
)

# Pre-init commands (run BEFORE stock app_init.sh is sourced).
# SSH needs this — dropbear should be up even if stock init fails. Talk starts
# its nc listener here too, and that is fine despite there being no post-init
# phase: the listener only needs to be LISTENING at boot; the speaker path
# (pktool → media) is touched lazily, once per talk connection, long after stock
# init has started media.
PRE_INIT_BLOCKS = {
    "ssh": (
        "# Persistent SSH (dropbear on port 22)\n"
        "mkdir -p /tmp/.ssh\n"
        "cp {store}/authorized_keys /tmp/.ssh/authorized_keys\n"
        "{store}/dropbear -r {store}/dbkey_ecdsa -p 22 &\n"
    ),
    # The sink script is installed once into {store} at apply time (see
    # TALK_SINK_NAME); this block only starts the listener that runs it — it
    # writes nothing at boot. {store} is filled by generate_app_init_wrapper.
    "talk": (
        "# Two-way talk: a TCP audio sink. The add-on streams the browser mic\n"
        f"# to TCP {TALK_TCP_PORT}; the sink script (installed in the patch\n"
        "# store) hands it to pktool play_aac -> media -> speaker.\n"
        f"( while true; do nc -l -p {TALK_TCP_PORT} -e /bin/sh {{store}}/{TALK_SINK_NAME}; "
        "sleep 1; done ) &\n"
    ),
}

def generate_app_init_wrapper(active_patchers: set[str],
                              device: Device | None = None) -> str:
    """Generate the app_init.sh wrapper for the given set of active
    patchers. Returns the shell script content.

    Everything happens before the stock init is sourced, because the stock
    init is what starts the processes: a bind-mount that lands after it would
    only take effect on the following boot. There is deliberately no post-init
    phase — camera reaches tserver through the agora bind-mount instead, which
    also puts it under the watchdog.
    """
    lines = [WRAPPER_HEADER_TEMPLATE.format(
        wrapper=app_init_wrapper_path(device))]
    store = patch_storage_dir(device)
    # Pre-init: things that must run before stock init (SSH, the talk sink).
    for pid in ("ssh", "talk"):
        if pid in active_patchers and pid in PRE_INIT_BLOCKS:
            lines.append(PRE_INIT_BLOCKS[pid].format(store=store))
    for pid in ("mqtt", "cloud", "cacert", "camera"):
        if pid in active_patchers and pid in BIND_MOUNT_TEMPLATES:
            lines.append(BIND_MOUNT_TEMPLATES[pid].format(store=store))
    lines.append(WRAPPER_FOOTER)
    return "".join(lines)


def build_wrapper_upload_cmd(active_patchers: set[str],
                             device: Device | None = None) -> str:
    """Build a shell command that writes the boot wrapper on the device."""
    wrapper = app_init_wrapper_path(device)
    content = generate_app_init_wrapper(active_patchers, device)
    escaped = content.replace("'", "'\\''")
    return f"printf '{escaped}' > {wrapper} && chmod +x {wrapper}"


def build_wrapper_remove_cmd(device: Device | None = None) -> str:
    """Shell command to remove the wrapper (reverts to stock init on next boot)."""
    return f"rm -f {app_init_wrapper_path(device)}"
