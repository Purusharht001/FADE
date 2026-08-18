from app.models.enums import Stage
from app.schemas.base import CamelModel


class CohortStats(CamelModel):
    total_patients: int
    total_scans: int
    needs_review: int
    avg_confidence: float
    by_stage: dict[Stage, int]
