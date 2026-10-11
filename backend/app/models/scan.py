import uuid
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, Date, Enum, Float, ForeignKey, String, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import BiomarkerKey, ExtractionMethod, ScanStatus, Stage

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

    # --- Provenance (all nullable: rows that predate these columns stay valid) ---
    extraction_method: Mapped[ExtractionMethod | None] = mapped_column(
        Enum(ExtractionMethod, native_enum=False, length=30), nullable=True
    )
    # A research identifier such as "OAS1_0001_MR1". Never a name, an MRN or
    # anything else that identifies a person; the API validates its shape.
    external_subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Raw mm3 volumes and mm thicknesses exactly as parsed, before derivation.
    raw_volumes: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # [{"code": "...", "kind": "plausibility" | "info", "message": "...", ...}, ...]
    qc_flags: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    calibration_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    engine_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Research-only ground truth (CDR at this visit). Deliberately absent from
    # every schema in app/schemas/scan.py: only the research export may read it.
    reference_label: Mapped[float | None] = mapped_column(Float, nullable=True)

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
    # None when the active calibration does not apply to this value (for
    # example real data scored while only the phantom calibration exists).
    abnormality: Mapped[float | None] = mapped_column(Float, nullable=True)


class FISResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "fis_results"

    scan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scans.id", ondelete="CASCADE"), unique=True, index=True
    )
    scan: Mapped["Scan"] = relationship(back_populates="fis_result")

    # None when the result is indeterminate (see `determinate` below).
    stage: Mapped[Stage | None] = mapped_column(
        Enum(Stage, native_enum=False, length=5), nullable=True
    )
    confidence: Mapped[float] = mapped_column(Float)
    uncertainty: Mapped[float] = mapped_column(Float)
    needs_review: Mapped[bool] = mapped_column(default=False)

    # {"CN": 0.12, "MCI": 0.63, "AD": 0.25}
    membership: Mapped[dict[str, float]] = mapped_column(JSON)
    # [{"id": "R1", "antecedent": "...", "consequent": "AD", "firing_strength": 0.81}, ...]
    fired_rules: Mapped[list[dict]] = mapped_column(JSON)

    # False when the engine declined to stage this case; `stage` is then
    # None and `indeterminate_reason` says why (codes in fis_engine.py).
    determinate: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    indeterminate_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 0-1 centroid of the aggregated output sets. None when indeterminate or
    # when the calibration has no output sets.
    severity: Mapped[float | None] = mapped_column(Float, nullable=True)
    calibration_version: Mapped[str] = mapped_column(
        String(64), default="unknown", server_default="unknown"
    )

    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)

    @property
    def reviewed(self) -> bool:
        return self.reviewed_by_id is not None
