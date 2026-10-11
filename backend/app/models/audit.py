import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin


class AuditLog(UUIDPrimaryKeyMixin, Base):
    """Who did what, to which record, and when. Append-only.

    Rows are written through `app.services.audit.record`, which refuses any
    `detail` value that is not a number, a boolean or a short code-like
    token. That is what keeps free text, and so PHI, out of this table.
    """

    __tablename__ = "audit_log"

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    # NULL for actions taken by a command-line tool rather than a user.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The actor's role at the time, which a later role change must not rewrite.
    actor_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # "scan.review", "rating.create", "rating.update", "export.results_csv", ...
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
