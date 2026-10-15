"""What the fountain app keeps on the panel application between requests."""
from __future__ import annotations

from dataclasses import dataclass, field

from aiohttp import web

from petkit_local.fountain_app.db import TableGuard

#: The `app[...]` key holding this package's `FountainState`. Set once by
#: `register`, before the application is frozen.
FOUNTAIN_APP = "fountain_app"


@dataclass
class FountainState:
    """Per-application state that outlives one request."""

    tables: TableGuard = field(default_factory=TableGuard)


def state_of(app: web.Application) -> FountainState:
    """This app's `FountainState`."""
    return app[FOUNTAIN_APP]
