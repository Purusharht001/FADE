from app.models.enums import Stage
from app.schemas.base import CamelModel


class CohortStats(CamelModel):
    total_patients: int
    total_scans: int
    needs_review: int
    avg_confidence: float
    by_stage: dict[Stage, int]
    # Results the engine declined to stage. Not counted in `by_stage`.
    indeterminate: int = 0
