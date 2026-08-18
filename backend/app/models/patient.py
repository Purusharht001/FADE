import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import DataSource, Sex

if TYPE_CHECKING:
    from app.models.scan import Scan
    from app.models.user import User


class Patient(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "patients"

    display_id: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    age: Mapped[int] = mapped_column(Integer)
    sex: Mapped[Sex] = mapped_column(Enum(Sex, native_enum=False, length=1))
    source: Mapped[DataSource] = mapped_column(Enum(DataSource, native_enum=False, length=10))
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped["User | None"] = relationship(back_populates="patients_created")

    scans: Mapped[list["Scan"]] = relationship(
        back_populates="patient", cascade="all, delete-orphan", order_by="Scan.scan_date.desc()"
    )
