"""The blind clinician-rating study.

A clinician sees cases as biomarkers only and records their own staging
call. The engine's output and the reference label are not in these
responses; `app/schemas/study.py` is the whole of what a rater receives.

Blinding here covers what this API returns. A rater who also opens the
triage dashboard can see the engine's stage there, so a real study needs
its raters kept off those screens: see docs/REAL_DATA_RUNBOOK.md.
"""

import hashlib
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import DbSession, require_role
from app.core.exceptions import NotFoundError
from app.models.enums import UserRole
from app.models.scan import Scan
from app.models.study import ClinicianRating
from app.models.user import User
from app.repositories import study_repo
from app.schemas.study import RatingIn, RatingRead, StudyBiomarker, StudyCase
from app.services import audit

router = APIRouter(prefix="/study", tags=["study"])

Rater = Annotated[User, Depends(require_role(UserRole.CLINICIAN))]


def case_code(scan_id: uuid.UUID) -> str:
    return f"CASE-{scan_id.hex[:8].upper()}"


def _rating_read(rating: ClinicianRating) -> RatingRead:
    return RatingRead(
        scan_id=rating.scan_id,
        stage_called=rating.stage_called,
        borderline=rating.borderline,
        confidence=rating.confidence,
        notes=rating.notes,
        created_at=rating.created_at,
        updated_at=rating.updated_at,
    )


def _presentation_order(scan: Scan, rater_id: uuid.UUID) -> str:
    """A stable order that differs per rater and reveals nothing: not the
    import order (a cohort file is often sorted by diagnosis), not the
    triage order (sorted by the engine's uncertainty).
    """
    return hashlib.sha256(f"{rater_id}:{scan.id}".encode()).hexdigest()


@router.get("/cases", response_model=list[StudyCase])
async def list_study_cases(
    db: DbSession,
    rater: Rater,
    include_phantom: Annotated[bool, Query(alias="includePhantom")] = False,
    unrated_only: Annotated[bool, Query(alias="unratedOnly")] = False,
) -> list[StudyCase]:
    """The cases to rate: completed scans, shown as biomarkers only.
    Synthetic phantom cases are left out unless `includePhantom=true`.
    """
    scans = await study_repo.list_completed_scans(db, include_phantom=include_phantom)
    my_ratings = await study_repo.ratings_by_scan_for_rater(db, rater.id)

    cases = []
    for scan in sorted(scans, key=lambda s: _presentation_order(s, rater.id)):
        mine = my_ratings.get(scan.id)
        if unrated_only and mine is not None:
            continue
        cases.append(
            StudyCase(
                scan_id=scan.id,
                case_code=case_code(scan.id),
                age=scan.patient.age,
                sex=scan.patient.sex,
                biomarkers=[
                    StudyBiomarker(key=b.key, value=b.value, unit=b.unit)
                    for b in sorted(scan.biomarkers, key=lambda b: b.key.value)
                ],
                my_rating=_rating_read(mine) if mine is not None else None,
            )
        )
    return cases


@router.post("/cases/{scan_id}/rating", response_model=RatingRead)
async def rate_study_case(
    scan_id: uuid.UUID, payload: RatingIn, db: DbSession, rater: Rater, response: Response
) -> RatingRead:
    """Records the caller's rating of a case. One rating per rater per
    scan: posting again replaces it, and the change is audited with the
    previous call. Returns 201 when created and 200 when updated.
    """
    scan = await study_repo.get_completed_scan(db, scan_id)
    if scan is None:
        raise NotFoundError(f"No study case with id {scan_id}")

    rating = await study_repo.get_rating(db, scan_id, rater.id)
    if rating is None:
        rating = await study_repo.create_rating(
            db,
            scan_id=scan_id,
            rater_id=rater.id,
            stage_called=payload.stage_called,
            borderline=payload.borderline,
            confidence=payload.confidence,
            notes=payload.notes,
        )
        await audit.record(
            db,
            actor=rater,
            action=audit.ACTION_RATING_CREATE,
            entity_type="scan",
            entity_id=scan_id,
            detail={
                "stage_called": payload.stage_called.value,
                "borderline": payload.borderline,
                "confidence": payload.confidence,
                "has_notes": bool(payload.notes),
            },
        )
        response.status_code = status.HTTP_201_CREATED
    else:
        # The audit entry keeps the call being replaced. Notes are recorded
        # only as "changed or not": they are free text.
        detail = {
            "stage_called": payload.stage_called.value,
            "borderline": payload.borderline,
            "confidence": payload.confidence,
            "previous_stage_called": rating.stage_called.value,
            "previous_borderline": rating.borderline,
            "previous_confidence": rating.confidence,
            "notes_changed": (payload.notes or None) != (rating.notes or None),
        }
        rating.stage_called = payload.stage_called
        rating.borderline = payload.borderline
        rating.confidence = payload.confidence
        rating.notes = payload.notes
        await db.flush()
        await audit.record(
            db,
            actor=rater,
            action=audit.ACTION_RATING_UPDATE,
            entity_type="scan",
            entity_id=scan_id,
            detail=detail,
        )

    await db.refresh(rating)
    return _rating_read(rating)
