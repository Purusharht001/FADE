import datetime
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, Query, UploadFile

from app.api.deps import CurrentUser, DbSession
from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError, UnprocessableScanError
from app.core.identifiers import validate_external_subject_id
from app.models.enums import BiomarkerKey, ExtractionMethod, ScanStatus
from app.models.patient import Patient
from app.models.scan import Scan
from app.repositories import patient_repo
from app.schemas.biomarkers import ManualBiomarkersRequest
from app.schemas.scan import ReviewRequest, ScanRead, SyntheticScanRequest
from app.services import scan_service
from app.services.biomarker_derivation import (
    DerivedBiomarkers,
    canonical_unit,
    derive_from_stats,
    derive_from_values,
    derive_from_volumes,
)
from app.services.calibration import data_domain_for
from app.services.freesurfer_stats import StatsParseError, parse_stats_bytes
from app.services.pipeline import Provenance

router = APIRouter(prefix="/patients/{patient_id}/scans", tags=["scans"])

_ALLOWED_SUFFIXES = {".nii", ".gz"}


# Stats attached to a stored scan may come from FreeSurfer or FastSurfer.
_ATTACHABLE_METHODS = {ExtractionMethod.FREESURFER_STATS, ExtractionMethod.FASTSURFER}


async def _require_patient(db: DbSession, patient_id: uuid.UUID) -> Patient:
    patient = await patient_repo.get_by_id(db, patient_id)
    if patient is None:
        raise NotFoundError(f"No patient with id {patient_id}")
    return patient


async def _require_scan(db: DbSession, patient_id: uuid.UUID, scan_id: uuid.UUID) -> Scan:
    scan = await patient_repo.get_scan(db, scan_id)
    if scan is None or scan.patient_id != patient_id:
        raise NotFoundError(f"No scan with id {scan_id} for patient {patient_id}")
    return scan


def _checked_subject_id(value: str | None) -> str | None:
    try:
        return validate_external_subject_id(value)
    except ValueError as exc:
        raise UnprocessableScanError(str(exc)) from exc


async def _read_stats_upload(file: UploadFile | None, label: str) -> bytes | None:
    """Reads one uploaded stats file into memory, bounded by
    `max_stats_upload_kb`. Stats files are a few KB of text.
    """
    if file is None:
        return None
    max_bytes = get_settings().max_stats_upload_kb * 1024
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise StatsParseError(
            f"The {label} file exceeds the {get_settings().max_stats_upload_kb} KB limit for a "
            "stats file."
        )
    return content


async def _read_stats_form(
    aseg: UploadFile, lh_aparc: UploadFile | None, rh_aparc: UploadFile | None
) -> DerivedBiomarkers:
    aseg_bytes = await _read_stats_upload(aseg, "aseg")
    parsed = parse_stats_bytes(
        aseg_bytes or b"",
        await _read_stats_upload(lh_aparc, "lh_aparc"),
        await _read_stats_upload(rh_aparc, "rh_aparc"),
    )
    return derive_from_stats(parsed)


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


@router.post("/freesurfer", response_model=ScanRead, status_code=201)
async def create_scan_from_freesurfer_stats(
    patient_id: uuid.UUID,
    db: DbSession,
    _current_user: CurrentUser,
    aseg: UploadFile,
    lh_aparc: UploadFile | None = None,
    rh_aparc: UploadFile | None = None,
    external_subject_id: Annotated[str | None, Form()] = None,
    scan_date: Annotated[datetime.date | None, Query(alias="scanDate")] = None,
) -> ScanRead:
    """Creates a scan from FreeSurfer / FastSurfer stats files: `aseg`
    (required) and `lh_aparc` + `rh_aparc` (optional, both or neither).
    Without the aparc files cortical thickness is not measured and the
    result is indeterminate.
    """
    patient = await _require_patient(db, patient_id)
    scan_service.ensure_real_data_patient(patient)
    subject_id = _checked_subject_id(external_subject_id)
    # Parsed before anything is written: a bad file leaves no scan behind.
    derived = await _read_stats_form(aseg, lh_aparc, rh_aparc)

    scan = await patient_repo.create_scan(
        db, patient_id=patient_id, scan_date=scan_date or datetime.date.today(), file_path=None
    )
    scan = await scan_service.process_derived_biomarkers(
        db,
        scan,
        derived,
        None,
        Provenance(
            extraction_method=ExtractionMethod.FREESURFER_STATS,
            data_domain=data_domain_for(patient.source),
            external_subject_id=subject_id,
        ),
    )
    return ScanRead.model_validate(scan)


def _derive_from_manual(payload: ManualBiomarkersRequest) -> DerivedBiomarkers:
    if payload.raw_volumes is not None:
        raw = payload.raw_volumes
        raw_dict = raw.model_dump()
        thickness = raw_dict.pop("cortical_thickness_mm")
        raw_dict["supplied_as"] = "raw_volumes"
        return derive_from_volumes(
            icv_mm3=raw.icv_mm3,
            brain_seg_vol_mm3=raw.brain_seg_vol_mm3,
            left_hippocampus_mm3=raw.left_hippocampus_mm3,
            right_hippocampus_mm3=raw.right_hippocampus_mm3,
            left_lateral_ventricle_mm3=raw.left_lateral_ventricle_mm3,
            right_lateral_ventricle_mm3=raw.right_lateral_ventricle_mm3,
            left_inf_lat_vent_mm3=raw.left_inf_lat_vent_mm3,
            right_inf_lat_vent_mm3=raw.right_inf_lat_vent_mm3,
            left_cerebral_cortex_mm3=raw.left_cerebral_cortex_mm3,
            right_cerebral_cortex_mm3=raw.right_cerebral_cortex_mm3,
            cortical_thickness_mm=thickness,
            raw_volumes=raw_dict,
        )

    assert payload.derived is not None  # the schema guarantees exactly one payload
    values: dict[BiomarkerKey, float | None] = {}
    for key in BiomarkerKey:
        entry = payload.derived.entry_of(key)
        if entry is None:
            values[key] = None
            continue
        canonical_unit(key, entry.unit)  # raises UnitError (422); units are never converted
        values[key] = entry.value
    return derive_from_values(
        hippocampal_volume_norm=values[BiomarkerKey.HIPPOCAMPAL_VOLUME],
        ventricle_brain_ratio=values[BiomarkerKey.VENTRICLE_BRAIN_RATIO],
        cortical_thickness_mm=values[BiomarkerKey.CORTICAL_THICKNESS],
        raw_volumes={"supplied_as": "derived_values"},
    )


@router.post("/biomarkers", response_model=ScanRead, status_code=201)
async def create_scan_from_biomarkers(
    patient_id: uuid.UUID,
    payload: ManualBiomarkersRequest,
    db: DbSession,
    _current_user: CurrentUser,
) -> ScanRead:
    """Creates a scan from biomarkers measured elsewhere: either raw mm3
    volumes (`rawVolumes`, derived here) or the three derived values with
    explicit units (`derived`).
    """
    patient = await _require_patient(db, patient_id)
    scan_service.ensure_real_data_patient(patient)
    derived = _derive_from_manual(payload)

    scan = await patient_repo.create_scan(
        db,
        patient_id=patient_id,
        scan_date=payload.scan_date or datetime.date.today(),
        file_path=None,
    )
    scan = await scan_service.process_derived_biomarkers(
        db,
        scan,
        derived,
        None,
        Provenance(
            extraction_method=ExtractionMethod.MANUAL_BIOMARKERS,
            data_domain=data_domain_for(patient.source),
            external_subject_id=payload.external_subject_id,
        ),
    )
    return ScanRead.model_validate(scan)


@router.post("/{scan_id}/stats", response_model=ScanRead)
async def attach_stats_to_scan(
    patient_id: uuid.UUID,
    scan_id: uuid.UUID,
    db: DbSession,
    _current_user: CurrentUser,
    aseg: UploadFile,
    lh_aparc: UploadFile | None = None,
    rh_aparc: UploadFile | None = None,
    external_subject_id: Annotated[str | None, Form()] = None,
    extraction_method: Annotated[ExtractionMethod, Form()] = ExtractionMethod.FREESURFER_STATS,
) -> ScanRead:
    """Attaches FreeSurfer / FastSurfer stats to a stored scan that is
    waiting for volumetry (or whose automatic segmentation failed), and
    scores it. A file that does not parse leaves the scan as it was.
    """
    patient = await _require_patient(db, patient_id)
    scan = await _require_scan(db, patient_id, scan_id)
    scan_service.ensure_real_data_patient(patient)

    if extraction_method not in _ATTACHABLE_METHODS:
        raise UnprocessableScanError(
            "extraction_method must be freesurfer_stats or fastsurfer when attaching stats."
        )
    recoverable_failure = (
        scan.status == ScanStatus.FAILED
        and scan.extraction_method != ExtractionMethod.INHOUSE_PHANTOM
    )
    if scan.status != ScanStatus.AWAITING_VOLUMETRY and not recoverable_failure:
        raise ConflictError(
            f"Stats can only be attached to a scan that is awaiting volumetry "
            f"(this scan is {scan.status.value})."
        )

    subject_id = _checked_subject_id(external_subject_id)
    derived = await _read_stats_form(aseg, lh_aparc, rh_aparc)
    scan = await scan_service.process_derived_biomarkers(
        db,
        scan,
        derived,
        None,
        Provenance(
            extraction_method=extraction_method,
            data_domain=data_domain_for(patient.source),
            external_subject_id=subject_id,
        ),
    )
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
    scan = await _require_scan(db, patient_id, scan_id)
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
    scan = await _require_scan(db, patient_id, scan_id)
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
