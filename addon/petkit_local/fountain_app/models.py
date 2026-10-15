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
"""
from __future__ import annotations

from sqlalchemy import REAL, Integer, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Label values that are not a pet id.
LABEL_UNKNOWN = "unknown"
LABEL_NOT_A_CAT = "not_a_cat"
SPECIAL_LABELS = (LABEL_UNKNOWN, LABEL_NOT_A_CAT)


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
