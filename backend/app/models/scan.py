import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Date, Enum, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import BiomarkerKey, ScanStatus, Stage

if TYPE_CHECKING:
    from app.models.patient import Patient


class Scan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "scans"

    patient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), index=True
    )
    patient: Mapped["Patient"] = relationship(back_populates="scans")

    scan_date: Mapped[date] = mapped_column(Date)
    modality: Mapped[str] = mapped_column(String(20), default="T1w")
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[ScanStatus] = mapped_column(
        Enum(ScanStatus, native_enum=False, length=30), default=ScanStatus.PENDING
    )
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    biomarkers: Mapped[list["BiomarkerReading"]] = relationship(
        back_populates="scan", cascade="all, delete-orphan"
    )
    fis_result: Mapped["FISResult | None"] = relationship(
        back_populates="scan", cascade="all, delete-orphan", uselist=False
    )


class BiomarkerReading(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "biomarker_readings"

    scan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scans.id", ondelete="CASCADE"), index=True
    )
    scan: Mapped["Scan"] = relationship(back_populates="biomarkers")

    key: Mapped[BiomarkerKey] = mapped_column(Enum(BiomarkerKey, native_enum=False, length=40))
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(10))
    abnormality: Mapped[float] = mapped_column(Float)


class FISResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "fis_results"

    scan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scans.id", ondelete="CASCADE"), unique=True, index=True
    )
    scan: Mapped["Scan"] = relationship(back_populates="fis_result")

    stage: Mapped[Stage] = mapped_column(Enum(Stage, native_enum=False, length=5))
    confidence: Mapped[float] = mapped_column(Float)
    uncertainty: Mapped[float] = mapped_column(Float)
    needs_review: Mapped[bool] = mapped_column(default=False)

    # {"CN": 0.12, "MCI": 0.63, "AD": 0.25}
    membership: Mapped[dict[str, float]] = mapped_column(JSON)
    # [{"id": "R1", "antecedent": "...", "consequent": "AD", "firing_strength": 0.81}, ...]
    fired_rules: Mapped[list[dict]] = mapped_column(JSON)

    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)

    @property
    def reviewed(self) -> bool:
        return self.reviewed_by_id is not None
