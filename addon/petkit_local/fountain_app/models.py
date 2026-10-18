"""The fountain app's own tables, on its own declarative base.

Kept apart from `events/models.py` so upstream's schema, its `create_all` and
its migrations stay untouched: `fountain_app.register` creates these on the
event store's engine from a start-up hook, and nothing upstream knows they
exist.

A label is keyed by (`device_id`, `visit_key`), never written into
`events.pet_id`/`pet_ref`: `EventStore.bind_pet_ref` and the start-up backfill
rewrite those columns, so a correction stored there would be lost. Labels also
outlive event pruning on purpose, so a training set is not lost with its
events.

The cat-ID job's answers live in their own table, `fountain_model_guesses`,
so the model never writes a label (`requirement:labels-stay-human`).
"""
from __future__ import annotations

from sqlalchemy import REAL, Integer, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Label values that are not a pet id.
LABEL_UNKNOWN = "unknown"
LABEL_NOT_A_CAT = "not_a_cat"
SPECIAL_LABELS = (LABEL_UNKNOWN, LABEL_NOT_A_CAT)
#: The model's answer when it cannot tell the cats apart.
ANSWER_NOT_SURE = "not_sure"


class FountainBase(DeclarativeBase):
    """Declarative base for the fountain app's tables only."""


class VisitLabel(FountainBase):
    """Evyatar's answer for one visit.

    `label` is a pet id as a decimal string (`"1"`), or one of
    `SPECIAL_LABELS`. `visit_key` is the `related_event` of the visit's anchor:
    the appearance if there was one, else the drinking episode.
    """

    __tablename__ = "fountain_visit_labels"
    __table_args__ = (
        UniqueConstraint("device_id", "visit_key", name="uq_fountain_visit_labels_visit"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(Integer, nullable=False)
    visit_key: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    labeled_at: Mapped[float] = mapped_column(REAL, nullable=False)


class ModelGuess(FountainBase):
    """The cat-ID model's answer for one visit, one row per device and visit.

    `label` is a pet id as a decimal string, or `ANSWER_NOT_SURE`.
    `confidence` (0 to 1) is the share of the model's sure crops that agree;
    `crops` counts the whole-cat crops it saw. `input_rev` is the visit's
    input revision the answer was made from, and `model_seq` the model's
    sequence number: the panel refuses an answer for footage that is no
    longer current or from an older model than the stored one.
    """

    __tablename__ = "fountain_model_guesses"
    __table_args__ = (
        UniqueConstraint("device_id", "visit_key", name="uq_fountain_model_guesses_visit"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(Integer, nullable=False)
    visit_key: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(REAL, nullable=False)
    crops: Mapped[int] = mapped_column(Integer, nullable=False)
    model_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    input_rev: Mapped[str] = mapped_column(Text, nullable=False)
    scored_at: Mapped[float] = mapped_column(REAL, nullable=False)
    #: The cats the model saw in the visit, a JSON list of pet ids as decimal
    #: strings, beside the answer and never counted as one
    #: (`decision:show-cats-seen`). Null from a job that does not send it, and
    #: on rows stored before the column existed (`db.TableGuard` adds it).
    seen: Mapped[str | None] = mapped_column(Text)

class FountainAlert(FountainBase):
    """One alert, from the moment its condition held to the moment it ended.

    `kind` is `no_drink` or `tank`. `subject` names what the alert is about,
    one active row per subject: `pet:<id>` for a cat, `<device id>:<flag>`
    for a tank. `cleared_at` is null while the alert is active. `detail` is a
    JSON object with the message shown in the banner and the facts behind it.
    """

    __tablename__ = "fountain_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    started_at: Mapped[float] = mapped_column(REAL, nullable=False)
    cleared_at: Mapped[float | None] = mapped_column(REAL)
    detail: Mapped[str | None] = mapped_column(Text)


class PreAlertCheck(FountainBase):
    """One durable, bounded classification attempt for a pet's no-drink gap.

    Result assessments are alert evidence only, never labels or normal guesses.
    """

    __tablename__ = "fountain_pre_alert_checks"
    __table_args__ = (
        UniqueConstraint("pet_id", "generation", name="uq_pre_alert_pet_generation"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    episode_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    pet_id: Mapped[int] = mapped_column(Integer, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    hours: Mapped[float] = mapped_column(REAL, nullable=False)
    anchor_ts: Mapped[float | None] = mapped_column(REAL)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    deadline: Mapped[float] = mapped_column(REAL, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    completed_at: Mapped[float | None] = mapped_column(REAL)
    model_seq: Mapped[int | None] = mapped_column(Integer)
    result: Mapped[str | None] = mapped_column(Text)