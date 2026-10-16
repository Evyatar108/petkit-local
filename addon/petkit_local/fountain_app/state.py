"""What the fountain app keeps on the panel application between requests."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

from petkit_local.fountain_app.db import TableGuard

#: The `app[...]` key holding this package's `FountainState`. Set once by
#: `register`, before the application is frozen.
FOUNTAIN_APP = "fountain_app"


@dataclass
class FountainState:
    """Per-application state that outlives one request."""

    tables: TableGuard = field(default_factory=TableGuard)
    #: Held while a dataset export runs; a second one is refused, not queued.
    export_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Serializes alert checks (the periodic task and a settings change).
    alert_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Serializes settings writes, so two partial changes cannot lose one another.
    settings_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Held across a model answer's revision check and write (`model_guess.py`).
    model_guess_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: `settings.load_settings` result, read on first use.
    alert_settings: dict[str, Any] | None = None


def state_of(app: web.Application) -> FountainState:
    """This app's `FountainState`."""
    return app[FOUNTAIN_APP]
