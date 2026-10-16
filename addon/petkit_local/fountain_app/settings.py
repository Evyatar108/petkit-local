"""The fountain app's settings, kept in `{data_dir}/fountain_app.json`.

* `no_drink_hours`: the no-drink alert threshold, default 12.
* `use_model_answers`: whether a confident model answer decides a visit's
  attributed cat (`requirement:model-attribution-switch`), default on. Off
  keeps the answers as suggestions only.

Read once per process and then kept in memory. A write names only the fields
it changes; the others keep their stored values, so a client that knows only
the threshold cannot switch the model back on by saving it.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from aiohttp import web

from petkit_local.fountain_app.state import state_of
from petkit_local.utils.coerce import to_float
from petkit_local.utils.jsonio import atomic_write_json, read_json

SETTINGS_FILE = "fountain_app.json"
DEFAULT_NO_DRINK_HOURS = 12.0
NO_DRINK_HOURS_RANGE = (1.0, 168.0)
DEFAULT_USE_MODEL_ANSWERS = True


class SettingsError(ValueError):
    """A settings change that names a bad value; the message says which."""


def _settings_path(data_dir: str) -> str:
    return os.path.join(data_dir, SETTINGS_FILE)


def _hours(value: Any) -> float | None:
    """A valid `no_drink_hours`, or None."""
    hours = None if isinstance(value, bool) else to_float(value, None)
    low, high = NO_DRINK_HOURS_RANGE
    return hours if hours is not None and low <= hours <= high else None


def load_settings(data_dir: str | None) -> dict[str, Any]:
    """The settings, defaults filled in; defaults alone without a data dir."""
    raw = read_json(_settings_path(data_dir), {}) if data_dir else {}
    raw = raw if isinstance(raw, dict) else {}
    hours = _hours(raw.get("no_drink_hours"))
    use_model = raw.get("use_model_answers")
    return {
        "no_drink_hours": DEFAULT_NO_DRINK_HOURS if hours is None else hours,
        "use_model_answers": (use_model if isinstance(use_model, bool)
                              else DEFAULT_USE_MODEL_ANSWERS),
    }


def settings_of(app: web.Application) -> dict[str, Any]:
    """The settings, read from disk once per process and then kept in memory."""
    state = state_of(app)
    if state.alert_settings is None:
        state.alert_settings = load_settings(app["cfg"].get("data_dir"))
    return state.alert_settings


def merged(current: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """`current` with the fields `changes` names, each validated.

    Raises:
        SettingsError: a named field has a bad value, or `changes` names no
            setting at all.
    """
    out = dict(current)
    if "no_drink_hours" in changes:
        hours = _hours(changes["no_drink_hours"])
        if hours is None:
            low, high = NO_DRINK_HOURS_RANGE
            raise SettingsError(f"no_drink_hours must be a number from {low:g} to {high:g}")
        out["no_drink_hours"] = hours
    if "use_model_answers" in changes:
        if not isinstance(changes["use_model_answers"], bool):
            raise SettingsError("use_model_answers must be true or false")
        out["use_model_answers"] = changes["use_model_answers"]
    if not {"no_drink_hours", "use_model_answers"} & set(changes):
        raise SettingsError("give no_drink_hours or use_model_answers")
    return out


async def update_settings(app: web.Application, changes: dict[str, Any]) -> dict[str, Any]:
    """Validate, save and keep a partial settings change; returns the new settings.

    Raises:
        SettingsError: see `merged`, or there is no data directory.
    """
    data_dir = app["cfg"].get("data_dir")
    state = state_of(app)
    async with state.settings_lock:
        settings = merged(settings_of(app), changes)
        if not data_dir:
            raise SettingsError("no data directory configured")
        await asyncio.to_thread(atomic_write_json, _settings_path(data_dir), settings)
        state.alert_settings = settings
    return settings
