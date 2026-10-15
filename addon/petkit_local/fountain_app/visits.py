"""Visits: one cat at the fountain, built from stored W7H event rows.

Pure functions, no I/O. The input is stored event rows as `db.visit_rows` reads
them (each already carrying its `"media"` list); the output is plain dicts the
API decorates with labels and media URLs.

A visit joins two device episodes that share nothing on the wire:

* an **appearance** (`20`, or the MQTT names with the same role) with its
  detection result (`24`), which points back at it only through
  `parent_event`, and
* a **drinking episode** (`5` drink start, `6` drink over), which shares its
  own `related_event` and carries no pet id.

A drinking episode belongs to the latest appearance that started at most
`VISIT_WINDOW_SEC` before it (`claim:visit-window-180s`). Either half alone is
still a visit: detection was off earlier, and a cat can look without drinking.

Codes are resolved through `codes.lookup` with the row's own `device_type`,
because HTTP codes are per device category.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from petkit_local.events import codes
from petkit_local.events.sessions import content_of_row
from petkit_local.utils.coerce import to_float, to_int

#: How long after an appearance starts a drinking episode still belongs to it.
#: The first real pair measured 102 s (2026-10-14).
VISIT_WINDOW_SEC = 180

APPEARANCE = "appearance"
DETECTION = "detection"
DRINK_START = "drink_start"
DRINK_OVER = "drink_over"


def classify(row: dict[str, Any]) -> str | None:
    """Which part of a visit this row is, or None if it is not one."""
    code = codes.lookup(row.get("event_type"), row.get("device_type"))
    if code is None:
        return None
    if code.kind == codes.KIND_PET:
        if code.role == codes.ROLE_DETECTION:
            return DETECTION
        if code.anchor:
            return APPEARANCE
        return None
    if code.kind == codes.KIND_DRINKING:
        if code.role == codes.ROLE_START:
            return DRINK_START
        if code.role == codes.ROLE_DONE:
            return DRINK_OVER
    return None


def _epoch(value: Any) -> float | None:
    """A device epoch from content, or None when absent or not positive."""
    v = to_float(value, None)
    return v if v is not None and v > 0 else None


def reported_start(row: dict[str, Any]) -> float:
    """When this row says its episode began, else when the row arrived.

    The per-row half of `_Episode.start`: whichever row sets an episode's
    start reports that same time here, so a store read filtered on this
    finds every episode that starts in its span, however late it arrived.
    """
    part = classify(row)
    if part == APPEARANCE:
        keys: tuple[str, ...] = ("start_time", "mark")
    elif part in (DRINK_START, DRINK_OVER):
        keys = ("event_start", "start_time")
    else:
        keys = ()
    content = content_of_row(row) if keys else {}
    for key in keys:
        t = _epoch(content.get(key))
        if t is not None:
            return t
    return float(row.get("ts") or 0)


@dataclass
class _Episode:
    """One device episode: every row sharing a `related_event`."""

    related_event: str
    kind: str  # APPEARANCE or "drinking"
    rows: list[dict[str, Any]] = field(default_factory=list)
    detections: list[dict[str, Any]] = field(default_factory=list)

    def first(self, part: str) -> dict[str, Any] | None:
        """The earliest row of one part, or None."""
        return next((r for r in self.rows if r["_part"] == part), None)

    @property
    def media(self) -> list[dict[str, Any]]:
        """The media rows of this episode (every row carries the same list)."""
        for r in self.rows:
            if r.get("media"):
                return list(r["media"])
        return []

    @cached_property
    def start(self) -> float:
        """When the episode began, in the device's own clock where it said.

        Cached: read only once `_episodes` has added every row.
        """
        if self.kind == APPEARANCE:
            row = self.first(APPEARANCE) or self.rows[0]
            content = content_of_row(row)
            return (_epoch(content.get("start_time")) or _epoch(content.get("mark"))
                    or float(row.get("ts") or 0))
        over = self.first(DRINK_OVER)
        start = self.first(DRINK_START)
        for row in (over, start):
            if row is not None:
                content = content_of_row(row)
                t = _epoch(content.get("event_start")) or _epoch(content.get("start_time"))
                if t is not None:
                    return t
        row = start or over or self.rows[0]
        return float(row.get("ts") or 0)

    def drink_span(self) -> tuple[float | None, float | None, float | None]:
        """`(start, end, duration_sec)` of a drinking episode.

        The device's own `event_start`/`event_end` win when the drink-over
        carries both; otherwise the two reports' arrival times
        (`claim:drink-duration-from-arrival-times`). An episode whose
        drink-over has not arrived yet has no end and no duration.
        """
        start_row = self.first(DRINK_START)
        over_row = self.first(DRINK_OVER)
        content = content_of_row(over_row) if over_row is not None else {}
        dev_start = _epoch(content.get("event_start"))
        dev_end = _epoch(content.get("event_end"))
        if dev_start is not None and dev_end is not None and dev_end >= dev_start:
            return dev_start, dev_end, dev_end - dev_start
        start = self.start
        if over_row is None:
            return start, None, None
        end = float(over_row.get("ts") or 0) or None
        if start_row is not None and end is not None and start_row.get("ts") is not None:
            return start, end, max(0.0, end - float(start_row["ts"]))
        return start, end, None

    def drink_time(self) -> int | None:
        """The device's own `drink_time` on the drink-over, in seconds."""
        over_row = self.first(DRINK_OVER)
        if over_row is None:
            return None
        v = to_int(content_of_row(over_row).get("drink_time"), None)
        return v if v is not None and v >= 0 else None


def _episodes(rows: list[dict[str, Any]]) -> tuple[list[_Episode], list[_Episode]]:
    """Appearance and drinking episodes, each list oldest first."""
    appearances: dict[str, _Episode] = {}
    drinking: dict[str, _Episode] = {}
    detections: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: r.get("ts") or 0):
        part = classify(row)
        rel = row.get("related_event")
        if part is None or not rel:
            continue
        row = {**row, "_part": part}
        if part == APPEARANCE:
            appearances.setdefault(rel, _Episode(rel, APPEARANCE)).rows.append(row)
        elif part == DETECTION:
            detections.append(row)
        else:
            drinking.setdefault(rel, _Episode(rel, "drinking")).rows.append(row)
    for det in detections:
        parent = det.get("parent_event")
        if parent and parent in appearances:
            appearances[parent].detections.append(det)
    return (sorted(appearances.values(), key=lambda e: e.start),
            sorted(drinking.values(), key=lambda e: e.start))


def _device_guess(appearance: _Episode | None, drinks: list[_Episode]) -> int | None:
    """The cat the fountain's own recognition named, as one of our pet ids.

    The detection result is the fountain's answer; a drink-over's `pet_id`
    is read only when there is no detection. Only `events.pet_id` counts,
    the column `resolve_pet_ref` fills when the reported id is one of ours.
    """
    candidates: list[dict[str, Any]] = []
    if appearance is not None:
        candidates += appearance.detections + appearance.rows
    for d in drinks:
        candidates += d.rows
    for row in candidates:
        if row.get("pet_id") is not None:
            return int(row["pet_id"])
    return None


def _visit(appearance: _Episode | None, drinks: list[_Episode]) -> dict[str, Any]:
    """One visit dict from its appearance (or None) and drinking episodes."""
    anchor = appearance or drinks[0]
    spans = [d.drink_span() for d in drinks]
    durations = [s[2] for s in spans if s[2] is not None]
    drink_times = [t for t in (d.drink_time() for d in drinks) if t is not None]
    ends = [s[1] for s in spans if s[1] is not None]
    episodes = ([appearance] if appearance else []) + drinks
    sample = anchor.rows[0]
    return {
        "visit_key": anchor.related_event,
        "device_id": sample.get("device_id"),
        "device_type": sample.get("device_type"),
        "start_ts": anchor.start,
        "appeared_ts": appearance.start if appearance else None,
        "drink_start_ts": drinks[0].start if drinks else None,
        "drink_end_ts": max(ends) if ends else None,
        # Summed when one appearance holds several drinking episodes.
        "duration_sec": round(sum(durations)) if durations else None,
        "drink_time_sec": sum(drink_times) if drink_times else None,
        "device_guess": _device_guess(appearance, drinks),
        "has_appearance": appearance is not None,
        "has_drinking": bool(drinks),
        "event_ids": sorted(r["id"] for e in episodes
                            for r in e.rows + e.detections if r.get("id") is not None),
        "episodes": [
            {"kind": e.kind, "related_event": e.related_event, "start_ts": e.start,
             "media": e.media}
            for e in episodes
        ],
    }


def build_visits(rows: list[dict[str, Any]],
                 window_sec: float = VISIT_WINDOW_SEC) -> list[dict[str, Any]]:
    """Every visit in `rows`, newest first.

    Args:
        rows: Stored event rows (any order, any kinds; non-visit rows are
            ignored), each optionally carrying `query_timeline`'s `"media"`.
        window_sec: How long after an appearance starts a drinking episode
            still joins it.
    """
    appearances, drinking = _episodes(rows)
    starts = [a.start for a in appearances]  # ascending: `_episodes` sorts by start
    attached: dict[str, list[_Episode]] = {a.related_event: [] for a in appearances}
    loose: list[_Episode] = []
    for drink in drinking:
        # The LATEST appearance that started at or before the drink, if in the window.
        i = bisect_right(starts, drink.start) - 1
        if i >= 0 and drink.start - starts[i] <= window_sec:
            attached[appearances[i].related_event].append(drink)
        else:
            loose.append(drink)

    visits = [_visit(a, attached[a.related_event]) for a in appearances]
    visits += [_visit(None, [d]) for d in loose]
    visits.sort(key=lambda v: v["start_ts"], reverse=True)
    return visits
