"""Export labeled fountain visits as an image dataset for training.

Layout, under `{data_dir}/datasets/<timestamp>/`::

    images/<label>/<visit_key>_<n>.jpg
    labels.csv      file, label, label_name, device_guess, visit_time, source

`<label>` is the stored label: a pet id, `unknown` or `not_a_cat`; the CSV
also names the pet, because a folder of ids alone does not say who is who.
Each visit contributes its episodes' snapshots (copied as-is) and a few
frames pulled from each highlight clip with ffmpeg. Without ffmpeg the
export still runs, with snapshots only, and says so.

The folder is built under a `.partial` name and renamed when complete, so a
failed or interrupted export never looks like a finished dataset.
Unlabeled visits are skipped: only labeled `visit_key`s are read at all.
"""
from __future__ import annotations

import csv
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from petkit_local.fountain_app import db
from petkit_local.fountain_app.visits import VISIT_WINDOW_SEC, build_visits
from petkit_local.media.transcode import THUMB_TIMEOUT, have_ffmpeg, run_ffmpeg

log = logging.getLogger(__name__)

#: Frames taken from one highlight clip (about 4 s long), one per second.
FRAMES_PER_CLIP = 3
#: Media roles read as a still, and as a clip (see web/api/media.py).
STILL_CATEGORIES = ("eventImage",)
CLIP_CATEGORIES = ("dynamicVideo", "highLight")
CSV_FIELDS = ("file", "label", "label_name", "device_guess", "visit_time", "source")


def _ready(media: list[dict[str, Any]], categories: tuple[str, ...]) -> list[str]:
    """Paths of the ready media files of some roles that exist on disk."""
    return [m["media_path"] for m in media
            if m.get("category") in categories and m.get("status") == "ready"
            and m.get("media_path") and os.path.isfile(m["media_path"])]


async def _visit_for(store, device_id: int, visit_key: str) -> dict[str, Any] | None:
    """The visit anchored at `visit_key`, rebuilt from its stored events."""
    anchor = await store.event_by_related(visit_key)
    if anchor is None or anchor.get("device_id") != device_id or anchor.get("ts") is None:
        return None
    ts = float(anchor["ts"])
    rows = await db.visit_rows(store, device_id, ts - VISIT_WINDOW_SEC, ts + VISIT_WINDOW_SEC,
                               VISIT_WINDOW_SEC)
    return next((v for v in build_visits(rows) if v["visit_key"] == visit_key), None)


async def _clip_frames(clip: str, staging: Path, prefix: str) -> list[Path]:
    """Up to `FRAMES_PER_CLIP` JPEG frames of one clip, in time order."""
    rc, _, _ = await run_ffmpeg(
        ["ffmpeg", "-y", "-v", "error", "-i", clip, "-vf", "fps=1",
         "-frames:v", str(FRAMES_PER_CLIP), "-q:v", "2", str(staging / f"{prefix}_%d.jpg")],
        timeout=THUMB_TIMEOUT, what=clip)
    frames = sorted(staging.glob(f"{prefix}_*.jpg"),
                    key=lambda p: int(p.stem.rsplit("_", 1)[1]))
    if rc != 0:
        log.warning("fountain export: no frames from %s (ffmpeg exit %s)", clip, rc)
    return [f for f in frames if f.stat().st_size > 0]


def _safe_name(value: str) -> str:
    """A path component from a device-supplied string: no separators, no dots."""
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in value)
    return cleaned or "_"


def _new_folder(root: Path, now: float) -> tuple[Path, Path]:
    """`(partial, final)` dataset paths, unique within `root`."""
    stamp = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
    n = 0
    while True:
        name = stamp if n == 0 else f"{stamp}-{n}"
        final = root / name
        partial = root / f"{name}.partial"
        if not final.exists() and not partial.exists():
            return partial, final
        n += 1


async def export_dataset(store, device_id: int, data_dir: str | Path,
                         pet_names: dict[int, str], now: float | None = None,
                         use_ffmpeg: bool | None = None) -> dict[str, Any]:
    """Write one dataset folder from every labeled visit of a fountain.

    Args:
        pet_names: Pet id to name, for the CSV's `label_name`.
        use_ffmpeg: Override the ffmpeg check (tests); None asks `have_ffmpeg`.

    Returns:
        `{path, visits, images, by_label, missing, no_media, ffmpeg}`:
        `missing` counts labels whose events are gone (pruned), `no_media`
        visits that had nothing to export.
    """
    now = now if now is not None else time.time()
    ffmpeg = have_ffmpeg() if use_ffmpeg is None else use_ffmpeg
    root = Path(data_dir) / "datasets"
    root.mkdir(parents=True, exist_ok=True)
    partial, final = _new_folder(root, now)
    staging = partial / ".frames"
    staging.mkdir(parents=True)

    labels = await db.all_labels(store, device_id)
    out_rows: list[dict[str, Any]] = []
    by_label: dict[str, int] = {}
    visits = missing = no_media = 0
    try:
        for item in labels:
            key, label = item["visit_key"], item["label"]
            visit = await _visit_for(store, device_id, key)
            if visit is None:
                missing += 1
                continue
            folder = partial / "images" / _safe_name(label)
            stem = _safe_name(key)
            label_name = (pet_names.get(int(label), f"pet {label}") if label.isdigit()
                          else label)
            visit_time = datetime.fromtimestamp(visit["start_ts"]).astimezone().isoformat(
                timespec="seconds")
            n = 0
            for ep in visit["episodes"]:
                sources: list[tuple[Path, str]] = [
                    (Path(p), f"snapshot:{ep['kind']}")
                    for p in _ready(ep["media"], STILL_CATEGORIES)
                    if Path(p).suffix.lower() in (".jpg", ".jpeg")]
                if ffmpeg:
                    for i, clip in enumerate(_ready(ep["media"], CLIP_CATEGORIES)):
                        frames = await _clip_frames(clip, staging, f"{stem}_{ep['kind']}_{i}")
                        sources += [(f, f"clip:{ep['kind']}@{j}s")
                                    for j, f in enumerate(frames)]
                for src, source in sources:
                    n += 1
                    folder.mkdir(parents=True, exist_ok=True)
                    dest = folder / f"{stem}_{n}.jpg"
                    if src.parent == staging:
                        os.replace(src, dest)
                    else:
                        shutil.copyfile(src, dest)
                    out_rows.append({
                        "file": dest.relative_to(partial).as_posix(), "label": label,
                        "label_name": label_name,
                        "device_guess": "" if visit["device_guess"] is None
                        else visit["device_guess"],
                        "visit_time": visit_time, "source": source})
            if n == 0:
                no_media += 1
                continue
            visits += 1
            by_label[label] = by_label.get(label, 0) + n

        with open(partial / "labels.csv", "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(out_rows)
        shutil.rmtree(staging, ignore_errors=True)
        os.replace(partial, final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise

    return {"path": str(final), "visits": visits, "images": len(out_rows),
            "by_label": by_label, "missing": missing, "no_media": no_media,
            "ffmpeg": ffmpeg}
