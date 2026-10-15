"""Entry point: builds the device-facing app and owns the process lifecycle.

Everything that outlives a single request is created in `start_background` and
torn down in `cleanup_background`, in a deliberate order — see the comment on
`cleanup_background` for why the event store closes last.
"""
from __future__ import annotations

import logging
import sys
from functools import partial

from aiohttp import web

from petkit_local.config import show_in_sidebar_once
from petkit_local.http.proxy import close_proxy_session
from petkit_local.main.cli import build_config, parse_args
from petkit_local.main.lifecycle import cleanup_background, start_background
from petkit_local.main.wiring import build_services

# Every other module logs under `__name__`; this one is also an entry point, and
# `python3 -m petkit_local.main` (what the add-on's Dockerfile runs) would make
# that "__main__" — a useless prefix in the add-on log. Pin the module path so
# the name is the same whether this module is imported or executed.
log = logging.getLogger(__name__ if __name__ != "__main__" else "petkit_local.main")


def main() -> None:
    """Parse the command line, build the `Config`, and run until stopped.

    Two entry paths converge here: `--ha-addon` reads the add-on options file
    written by the Supervisor, while the individual flags are the way the
    add-on is run outside Home Assistant. Flags always win over the add-on
    options, so a bad option file can be overridden without editing it.
    """
    args = parse_args()
    config = build_config(args)

    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    if args.ha_addon:
        # After logging is configured, so the one line it writes is visible, and
        # before anything long-running, so the sidebar entry appears while the
        # user is still looking at the add-on page they just started.
        show_in_sidebar_once(config.data_dir)

    services = build_services(config, args)
    app = services.app

    app.on_startup.append(partial(start_background, services))
    app.on_cleanup.append(partial(cleanup_background, services))
    # Closed last: the shared upstream connection pool must outlive anything
    # above that could still be forwarding a request.
    app.on_cleanup.append(close_proxy_session)

    log.info("Starting petkit-local on port %d", config.http_port)
    log.info("API URL: %s", config.api_url)
    log.info("MQTT broker: port %d %s", config.mqtt_port, "(disabled)" if args.no_mqtt else "")
    log.info("HA MQTT: %s:%d %s", config.ha_mqtt_host, config.ha_mqtt_port,
             "(disabled)" if args.no_ha else "")
    log.info("Registered devices: %d", len(services.registry.all()))

    web.run_app(app, host=config.bind_host, port=config.http_port, print=None)
