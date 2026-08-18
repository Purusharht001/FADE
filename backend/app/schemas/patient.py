import uuid
from typing import TYPE_CHECKING

from pydantic import Field

from app.models.enums import DataSource, Sex
from app.schemas.base import CamelModel
from app.schemas.scan import ScanRead, ScanSummary

if TYPE_CHECKING:
    from app.models.patient import Patient


class PatientCreate(CamelModel):
    age: int = Field(ge=0, le=120)
    sex: Sex
    source: DataSource = DataSource.CLINIC


class PatientListItem(CamelModel):
    """Row shape for the triage dashboard — one patient, its most recent
    scan's staging result, nothing else. The backend sorts by uncertainty
    by default (`GET /patients?sortByUncertainty=true`); the frontend may
    additionally re-sort/filter this same already-computed field client
    side without that counting as "computing clinical logic" — it's
    ordering numbers the backend produced, not deriving them.
    """

    id: uuid.UUID
    display_id: str
    age: int
    sex: Sex
    source: DataSource
    latest_scan: ScanSummary | None = None

    @classmethod
    def from_patient(cls, patient: "Patient") -> "PatientListItem":
        latest = patient.scans[0] if patient.scans else None
        return cls(
            id=patient.id,
            display_id=patient.display_id,
            age=patient.age,
            sex=patient.sex,
            source=patient.source,
            latest_scan=ScanSummary.from_scan(latest) if latest else None,
        )


class PatientDetailRead(CamelModel):
    id: uuid.UUID
    display_id: str
    age: int
    sex: Sex
    source: DataSource
    scans: list[ScanRead]
