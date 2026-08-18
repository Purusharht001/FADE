import uuid

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession
from app.core.exceptions import NotFoundError
from app.models.enums import Stage
from app.repositories import patient_repo
from app.schemas.patient import PatientCreate, PatientDetailRead, PatientListItem

router = APIRouter(prefix="/patients", tags=["patients"])


@router.get("", response_model=list[PatientListItem])
async def list_patients(
    db: DbSession,
    _current_user: CurrentUser,
    stage: Stage | None = None,
    needs_review: bool | None = Query(None, alias="needsReview"),
    sort_by_uncertainty: bool = Query(
        True,
        alias="sortByUncertainty",
        description="Sort by triage uncertainty (descending) instead of patient ID.",
    ),
) -> list[PatientListItem]:
    patients = await patient_repo.list_patients(db)
    items = [PatientListItem.from_patient(p) for p in patients]

    if stage is not None:
        items = [i for i in items if i.latest_scan and i.latest_scan.stage == stage]
    if needs_review is not None:
        items = [i for i in items if i.latest_scan and i.latest_scan.needs_review == needs_review]

    if sort_by_uncertainty:
        items.sort(
            key=lambda i: (
                i.latest_scan.uncertainty
                if i.latest_scan and i.latest_scan.uncertainty is not None
                else -1
            ),
            reverse=True,
        )
    return items


@router.post("", response_model=PatientDetailRead, status_code=201)
async def create_patient(
    payload: PatientCreate, db: DbSession, current_user: CurrentUser
) -> PatientDetailRead:
    patient = await patient_repo.create(
        db, age=payload.age, sex=payload.sex, source=payload.source, created_by_id=current_user.id
    )
    return PatientDetailRead.model_validate(patient)


@router.get("/{patient_id}", response_model=PatientDetailRead)
async def get_patient(
    patient_id: uuid.UUID, db: DbSession, _current_user: CurrentUser
) -> PatientDetailRead:
    patient = await patient_repo.get_by_id(db, patient_id)
    if patient is None:
        raise NotFoundError(f"No patient with id {patient_id}")
    return PatientDetailRead.model_validate(patient)
