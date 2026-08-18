import datetime
import uuid
from typing import TYPE_CHECKING

from pydantic import Field

from app.models.enums import ScanStatus, Stage
from app.schemas.base import CamelModel
from app.schemas.fis import BiomarkerReadingRead, FISResultRead

if TYPE_CHECKING:
    from app.models.scan import Scan


class SyntheticScanRequest(CamelModel):
    """Demo-only: generates a synthetic MRI at the given severity and runs
    it through the real preprocessing/volumetry/FIS pipeline. See
    `app/services/synthetic_mri.py` for why this exists — there is no real
    imaging dataset wired up yet (Phase 1 is still open).
    """

    severity: float = Field(ge=0.0, le=1.0, description="0 = healthy, 1 = maximally atrophied")
    seed: int | None = Field(default=None, description="Deterministic RNG seed; omit for random")
    scan_date: datetime.date | None = None


class ScanRead(CamelModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    scan_date: datetime.date
    modality: str
    status: ScanStatus
    failure_reason: str | None
    biomarkers: list[BiomarkerReadingRead]
    fis_result: FISResultRead | None


class ScanSummary(CamelModel):
    """Lightweight scan representation for embedding in patient list views —
    avoids shipping the full rule list for every row of a triage table.
    """

    id: uuid.UUID
    scan_date: datetime.date
    status: ScanStatus
    stage: Stage | None = None
    confidence: float | None = None
    uncertainty: float | None = None
    needs_review: bool | None = None

    @classmethod
    def from_scan(cls, scan: "Scan") -> "ScanSummary":
        result = scan.fis_result
        return cls(
            id=scan.id,
            scan_date=scan.scan_date,
            status=scan.status,
            stage=result.stage if result else None,
            confidence=result.confidence if result else None,
            uncertainty=result.uncertainty if result else None,
            needs_review=result.needs_review if result else None,
        )


class ScanCreateResponse(CamelModel):
    scan_id: uuid.UUID
    status: ScanStatus
    message: str


class ReviewRequest(CamelModel):
    reviewed: bool = True
