from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.repositories import patient_repo
from app.schemas.cohort import CohortStats

router = APIRouter(prefix="/cohort", tags=["cohort"])


@router.get("/stats", response_model=CohortStats)
async def get_cohort_stats(db: DbSession, _current_user: CurrentUser) -> CohortStats:
    stats = await patient_repo.cohort_stats(db)
    return CohortStats.model_validate(stats)
