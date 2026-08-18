import datetime
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query, UploadFile

from app.api.deps import CurrentUser, DbSession
from app.core.config import get_settings
from app.core.exceptions import NotFoundError, UnprocessableScanError
from app.models.enums import ScanStatus
from app.repositories import patient_repo
from app.schemas.scan import ReviewRequest, ScanRead, SyntheticScanRequest
from app.services import scan_service

router = APIRouter(prefix="/patients/{patient_id}/scans", tags=["scans"])

_ALLOWED_SUFFIXES = {".nii", ".gz"}


async def _require_patient(db: DbSession, patient_id: uuid.UUID):  # noqa: ANN202
    patient = await patient_repo.get_by_id(db, patient_id)
    if patient is None:
        raise NotFoundError(f"No patient with id {patient_id}")
    return patient


@router.post("/upload", response_model=ScanRead, status_code=201)
async def upload_scan(
    patient_id: uuid.UUID,
    db: DbSession,
    _current_user: CurrentUser,
    file: UploadFile,
    scan_date: Annotated[datetime.date | None, Query(alias="scanDate")] = None,
) -> ScanRead:
    await _require_patient(db, patient_id)
    settings = get_settings()

    suffix = "".join(Path(file.filename or "").suffixes[-2:]) or ".nii.gz"
    if not any(suffix.endswith(s) for s in _ALLOWED_SUFFIXES):
        raise UnprocessableScanError("Only .nii or .nii.gz files are accepted.")

    scan = await patient_repo.create_scan(
        db, patient_id=patient_id, scan_date=scan_date or datetime.date.today(), file_path=None
    )

    dest_dir = Path(settings.upload_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - one-shot idempotent mkdir, not worth a thread hop
    dest_path = dest_dir / f"{scan.id}{suffix}"

    size = 0
    max_bytes = settings.max_upload_mb * 1024 * 1024
    with dest_path.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                f.close()
                dest_path.unlink(missing_ok=True)
                raise UnprocessableScanError(
                    f"File exceeds the {settings.max_upload_mb}MB upload limit."
                )
            f.write(chunk)

    scan.file_path = str(dest_path)
    await db.flush()

    scan = await scan_service.process_uploaded_scan(db, scan, str(dest_path))
    return ScanRead.model_validate(scan)


@router.post("/synthetic", response_model=ScanRead, status_code=201)
async def create_synthetic_scan(
    patient_id: uuid.UUID, payload: SyntheticScanRequest, db: DbSession, _current_user: CurrentUser
) -> ScanRead:
    await _require_patient(db, patient_id)
    scan = await patient_repo.create_scan(
        db,
        patient_id=patient_id,
        scan_date=payload.scan_date or datetime.date.today(),
        file_path=None,
    )
    scan = await scan_service.process_synthetic_scan(db, scan, payload.severity, seed=payload.seed)
    return ScanRead.model_validate(scan)


@router.get("/{scan_id}", response_model=ScanRead)
async def get_scan(
    patient_id: uuid.UUID, scan_id: uuid.UUID, db: DbSession, _current_user: CurrentUser
) -> ScanRead:
    await _require_patient(db, patient_id)
    scan = await patient_repo.get_scan(db, scan_id)
    if scan is None or scan.patient_id != patient_id:
        raise NotFoundError(f"No scan with id {scan_id} for patient {patient_id}")
    return ScanRead.model_validate(scan)


@router.post("/{scan_id}/review", response_model=ScanRead)
async def review_scan(
    patient_id: uuid.UUID,
    scan_id: uuid.UUID,
    payload: ReviewRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> ScanRead:
    await _require_patient(db, patient_id)
    scan = await patient_repo.get_scan(db, scan_id)
    if scan is None or scan.patient_id != patient_id:
        raise NotFoundError(f"No scan with id {scan_id} for patient {patient_id}")
    if scan.fis_result is None:
        raise UnprocessableScanError("Scan has no FIS result to review yet.")
    if scan.status != ScanStatus.COMPLETED:
        raise UnprocessableScanError(f"Scan is not completed yet (status={scan.status.value}).")

    scan.fis_result.reviewed_by_id = current_user.id if payload.reviewed else None
    scan.fis_result.reviewed_at = (
        datetime.datetime.now(datetime.UTC).isoformat() if payload.reviewed else None
    )
    await db.flush()
    await db.refresh(scan, attribute_names=["fis_result"])
    return ScanRead.model_validate(scan)
