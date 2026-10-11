import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import Stage

if TYPE_CHECKING:
    from app.models.scan import Scan
    from app.models.user import User

NOTES_MAX_LENGTH = 2000


class ClinicianRating(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One clinician's blind call on one scan, for the validation study.

    A rater has at most one rating per scan. Changing it updates the row in
    place (`updated_at` moves) and writes an audit entry holding the
    previous call, so the history is not lost.
    """

    __tablename__ = "clinician_ratings"
    __table_args__ = (
        UniqueConstraint("scan_id", "rater_id", name="uq_clinician_ratings_scan_rater"),
        CheckConstraint("confidence BETWEEN 1 AND 5", name="ck_clinician_ratings_confidence"),
    )

    scan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scans.id", ondelete="CASCADE"), index=True
    )
    scan: Mapped["Scan"] = relationship()

    # RESTRICT: a rater with recorded ratings cannot be deleted out from
    # under the study. Deactivate the account instead.
    rater_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    rater: Mapped["User"] = relationship()

    stage_called: Mapped[Stage] = mapped_column(Enum(Stage, native_enum=False, length=5))
    # The clinician's own judgement that this case sits between two stages.
    borderline: Mapped[bool] = mapped_column(Boolean, default=False)
    # 1 (guessing) to 5 (certain).
    confidence: Mapped[int] = mapped_column(Integer)
    # Free text. The API tells raters not to enter identifying details; it
    # is the one field in this table that cannot be checked mechanically.
    notes: Mapped[str | None] = mapped_column(String(NOTES_MAX_LENGTH), nullable=True)
