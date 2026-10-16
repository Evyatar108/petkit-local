"""Per-day, per-cat drinking stats over the last N local days.

Pure functions. Days are cut at LOCAL midnight through
`utils/timeutil.local_day_bounds`, never `start + 86400`, so a DST day is 23
or 25 hours long and still counted once.

Each visit counts for its **attributed cat**: the label if there is one, else
the cat-ID model's answer when it names a cat with confidence of at least
`MODEL_CONFIDENT` and the `use_model_answers` setting is on
(`decision:model-answer-counts-when-confident`), else the fountain's own
guess, else unknown. A visit labeled "not a cat" counts for nobody and is
reported only as a total.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from petkit_local.fountain_app.models import LABEL_NOT_A_CAT, LABEL_UNKNOWN
from petkit_local.utils.timeutil import DATE_FORMAT, local_day_bounds

#: The ranges the stats view offers.
ALLOWED_DAYS = (7, 30)
#: The least confidence at which a model answer decides attribution.
MODEL_CONFIDENT = 0.8


def model_pet(guess: dict[str, Any] | None) -> int | None:
    """The pet a stored model answer names, or None for "not sure" or no answer."""
    label = guess.get("label") if guess else None
    return int(label) if isinstance(label, str) and label.isdigit() else None


def model_confident(guess: dict[str, Any] | None) -> bool:
    """True if a stored model answer names a cat confidently enough to count."""
    return (model_pet(guess) is not None
            and (guess.get("confidence") or 0) >= MODEL_CONFIDENT)


def attributed(visit: dict[str, Any], label: str | None,
               guess: dict[str, Any] | None = None, use_model: bool = False) -> str:
    """The attributed cat of one visit: a pet id string, or a special label.

    Args:
        guess: The visit's stored model answer (`db.guesses_for`), or None.
        use_model: The `use_model_answers` setting; off ignores `guess`.
    """
    if label:
        return label
    if use_model and model_confident(guess):
        return str(model_pet(guess))
    guess_id = visit.get("device_guess")
    return str(guess_id) if guess_id is not None else LABEL_UNKNOWN


def day_spans(days: int, now: float | None = None) -> list[tuple[str, float, float]]:
    """`(YYYY-MM-DD, start, end)` for the last `days` local days, oldest first.

    Today is the last entry. Each span comes from `local_day_bounds`, so the
    ends are re-localized midnights.
    """
    moment = datetime.fromtimestamp(now) if now is not None else datetime.now()
    today: date = moment.date()
    out = []
    for back in range(days - 1, -1, -1):
        day = (today - timedelta(days=back)).strftime(DATE_FORMAT)
        start, end, label = local_day_bounds(day)
        out.append((label, start, end))
    return out


def _empty_series(key: str, n_days: int, dates: list[str]) -> dict[str, Any]:
    return {
        "key": key,
        "pet_id": int(key) if key.isdigit() else None,
        "visits": 0, "labeled": 0, "drink_sec": 0, "drink_time_sec": 0,
        "daily": [{"date": d, "visits": 0, "drink_sec": 0} for d in dates[:n_days]],
    }


def aggregate(visits: list[dict[str, Any]], labels: dict[str, str],
              spans: list[tuple[str, float, float]],
              pet_ids: list[int], guesses: dict[str, dict[str, Any]] | None = None,
              use_model: bool = False) -> dict[str, Any]:
    """Stats for `visits` over `spans`.

    Args:
        visits: `visits.build_visits` output; visits outside every span are
            ignored.
        labels: `visit_key -> label` for this device.
        spans: `day_spans` output.
        pet_ids: Every known pet, so a cat with no visits still gets a row.
        guesses: `visit_key -> stored model answer`, for `attributed`.
        use_model: The `use_model_answers` setting, for `attributed`.

    Returns:
        `{dates, series, not_a_cat, visits, labeled}`. `series` holds one entry
        per pet (in `pet_ids` order), then any other attributed key, then
        `unknown`; each entry has totals and a `daily` list aligned with
        `dates`. `drink_sec` sums visit durations; `drink_time_sec` sums the
        device's own `drink_time`.
    """
    dates = [s[0] for s in spans]
    series: dict[str, dict[str, Any]] = {}
    for pid in pet_ids:
        series[str(pid)] = _empty_series(str(pid), len(dates), dates)
    not_a_cat = 0
    total = 0
    labeled = 0
    for v in visits:
        ts = v.get("start_ts") or 0
        idx = next((i for i, (_, s, e) in enumerate(spans) if s <= ts < e), None)
        if idx is None:
            continue
        label = labels.get(v["visit_key"])
        total += 1
        if label:
            labeled += 1
        key = attributed(v, label, (guesses or {}).get(v["visit_key"]), use_model)
        if key == LABEL_NOT_A_CAT:
            not_a_cat += 1
            continue
        row = series.setdefault(key, _empty_series(key, len(dates), dates))
        dur = v.get("duration_sec") or 0
        row["visits"] += 1
        row["labeled"] += 1 if label else 0
        row["drink_sec"] += dur
        row["drink_time_sec"] += v.get("drink_time_sec") or 0
        row["daily"][idx]["visits"] += 1
        row["daily"][idx]["drink_sec"] += dur

    ordered = [series.pop(str(pid)) for pid in pet_ids]
    unknown = series.pop(LABEL_UNKNOWN, None) or _empty_series(LABEL_UNKNOWN, len(dates), dates)
    ordered += sorted(series.values(), key=lambda s: s["key"]) + [unknown]
    return {"dates": dates, "series": ordered, "not_a_cat": not_a_cat,
            "visits": total, "labeled": labeled}
