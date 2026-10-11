import datetime
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.api.deps import CurrentUser, DbSession
from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError, UnprocessableScanError
from app.core.identifiers import validate_external_subject_id
from app.core.rate_limit import limiter, upload_rate_limit
from app.models.enums import BiomarkerKey, ExtractionMethod, ScanStatus
from app.models.patient import Patient
from app.models.scan import Scan
from app.repositories import patient_repo
from app.schemas.biomarkers import ManualBiomarkersRequest
from app.schemas.scan import ReviewRequest, ScanRead, SyntheticScanRequest
from app.services import (
    audit,
    dicom_ingest,
    scan_service,
    upload_validation,
    volumetry_providers,
)
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
from app.services.upload_validation import FileKind
from app.services.volumetry_providers import VolumetryProvider

router = APIRouter(prefix="/patients/{patient_id}/scans", tags=["scans"])

# What to tell someone who sent the NIfTI endpoint something else. Chosen
# by what the file's content is, never by its name.
_NOT_NIFTI_MESSAGE = {
    FileKind.ZIP: (
        "This is a .zip archive, not a NIfTI volume. Upload a zipped DICOM series to "
        "/scans/upload-dicom instead."
    ),
    FileKind.DICOM: (
        "This is a single DICOM file. Zip the whole series and upload it to "
        "/scans/upload-dicom, or convert it to NIfTI first."
    ),
    FileKind.NIFTI_PAIR: (
        "This is the header half of a two-file NIfTI/Analyze pair. Convert it to a single "
        ".nii or .nii.gz file and upload that."
    ),
    FileKind.GZIP: "This gzip file does not contain a NIfTI volume.",
    FileKind.UNKNOWN: "Only NIfTI volumes (.nii or .nii.gz) are accepted here.",
}
_NOT_ZIP_MESSAGE: dict[FileKind | None, str] = {
    FileKind.NIFTI: "This is a NIfTI volume, not a .zip. Upload it to /scans/upload instead.",
    FileKind.NIFTI_GZ: "This is a NIfTI volume, not a .zip. Upload it to /scans/upload instead.",
    FileKind.DICOM: "This is a single DICOM file. Zip the whole series and upload the .zip.",
    None: "Only a .zip archive of a DICOM series is accepted here.",
}


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


async def _create_scan_for_file(
    db: DbSession,
    patient: Patient,
    nifti_path: Path,
    provider: VolumetryProvider,
    scan_date: datetime.date | None,
    external_subject_id: str | None,
) -> Scan:
    """Records a stored, sanitised NIfTI as a scan and hands it to the
    volumetry provider. If that fails inside the request the stored file is
    removed: the failed scan row is rolled back, and a file with no row
    would never be found again.
    """
    try:
        scan = await patient_repo.create_scan(
            db,
            patient_id=patient.id,
            scan_date=scan_date or datetime.date.today(),
            file_path=str(nifti_path),
            external_subject_id=external_subject_id,
        )
        return await volumetry_providers.submit_stored_scan(db, scan, nifti_path, provider)
    except BaseException:
        nifti_path.unlink(missing_ok=True)  # noqa: ASYNC240 - one-shot local unlink on the error path
        raise


@router.post("/upload", response_model=ScanRead, status_code=201)
@limiter.limit(upload_rate_limit)
async def upload_scan(
    request: Request,
    patient_id: uuid.UUID,
    db: DbSession,
    _current_user: CurrentUser,
    file: UploadFile,
    scan_date: Annotated[datetime.date | None, Query(alias="scanDate")] = None,
    external_subject_id: Annotated[str | None, Query(alias="externalSubjectId")] = None,
) -> ScanRead:
    """Uploads one T1-weighted volume as NIfTI (.nii or .nii.gz).

    What happens next depends on VOLUMETRY_PROVIDER: the scan is returned
    `awaiting_volumetry` (external), `segmenting` (fastsurfer_docker; poll
    the scan until it completes) or already scored (inhouse, PHANTOM
    patients only).
    """
    patient = await _require_patient(db, patient_id)
    settings = get_settings()
    provider = volumetry_providers.active_provider(settings)
    # Before the body is read: a refused upload is never written to disk.
    volumetry_providers.check_provider_accepts(provider, patient)
    subject_id = _checked_subject_id(external_subject_id)

    incoming = upload_validation.incoming_dir(settings)
    raw_path = incoming / upload_validation.new_upload_name(".upload")
    await upload_validation.store_upload(
        file, raw_path, settings.max_upload_mb * 1024 * 1024, settings.max_upload_mb
    )
    try:
        kind = await run_in_threadpool(upload_validation.detect_kind, raw_path)
        if kind not in (FileKind.NIFTI, FileKind.NIFTI_GZ):
            raise UnprocessableScanError(_NOT_NIFTI_MESSAGE[kind])
        # nibabel chooses its reader from the suffix, so give the file the
        # suffix its content calls for (never the one the client sent).
        typed_path = raw_path.with_suffix(".nii.gz" if kind == FileKind.NIFTI_GZ else ".nii")
        raw_path.rename(typed_path)
        raw_path = typed_path

        nifti_path = Path(settings.upload_dir) / upload_validation.new_upload_name(".nii.gz")
        await run_in_threadpool(upload_validation.sanitize_nifti, raw_path, nifti_path)
    finally:
        raw_path.unlink(missing_ok=True)

    scan = await _create_scan_for_file(db, patient, nifti_path, provider, scan_date, subject_id)
    return ScanRead.model_validate(scan)


@router.post("/upload-dicom", response_model=ScanRead, status_code=201)
@limiter.limit(upload_rate_limit)
async def upload_dicom_scan(
    request: Request,
    patient_id: uuid.UUID,
    db: DbSession,
    _current_user: CurrentUser,
    file: UploadFile,
    scan_date: Annotated[datetime.date | None, Query(alias="scanDate")] = None,
    external_subject_id: Annotated[str | None, Query(alias="externalSubjectId")] = None,
) -> ScanRead:
    """Uploads one T1-weighted series as a .zip of DICOM files.

    The series must already be anonymised: with REQUIRE_ANONYMISED_DICOM on
    (the default) an archive whose identifying header tags are not blank is
    rejected, and the response lists which tags. The DICOM is converted to
    NIfTI and then deleted; only the NIfTI is kept.
    """
    patient = await _require_patient(db, patient_id)
    settings = get_settings()
    provider = volumetry_providers.active_provider(settings)
    if provider == VolumetryProvider.INHOUSE:
        # The in-house path is for phantom volumes, which are never DICOM.
        raise UnprocessableScanError(volumetry_providers.INHOUSE_NOT_FOR_REAL_DATA)
    volumetry_providers.check_provider_accepts(provider, patient)
    subject_id = _checked_subject_id(external_subject_id)

    work_dir = upload_validation.incoming_dir(settings) / upload_validation.new_upload_name("")
    zip_path = work_dir / "upload.zip"
    try:
        await upload_validation.store_upload(
            file, zip_path, settings.max_upload_mb * 1024 * 1024, settings.max_upload_mb
        )
        kind = await run_in_threadpool(upload_validation.detect_kind, zip_path)
        if kind != FileKind.ZIP:
            raise UnprocessableScanError(_NOT_ZIP_MESSAGE.get(kind, _NOT_ZIP_MESSAGE[None]))

        converted = await run_in_threadpool(dicom_ingest.convert_dicom_zip, zip_path, work_dir)
        nifti_path = Path(settings.upload_dir) / upload_validation.new_upload_name(".nii.gz")
        await run_in_threadpool(upload_validation.sanitize_nifti, converted, nifti_path)
    finally:
        # The original DICOM never outlives the request, success or failure.
        await run_in_threadpool(dicom_ingest.discard, work_dir)

    scan = await _create_scan_for_file(db, patient, nifti_path, provider, scan_date, subject_id)
    return ScanRead.model_validate(scan)


@router.post("/freesurfer", response_model=ScanRead, status_code=201)
@limiter.limit(upload_rate_limit)
async def create_scan_from_freesurfer_stats(
    request: Request,
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
@limiter.limit(upload_rate_limit)
async def attach_stats_to_scan(
    request: Request,
    patient_id: uuid.UUID,
    scan_id: uuid.UUID,
    db: DbSession,
    current_user: CurrentUser,
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
    previous_status = scan.status
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
    await audit.record(
        db,
        actor=current_user,
        action=audit.ACTION_STATS_ATTACHED,
        entity_type="scan",
        entity_id=scan.id,
        detail={
            "extraction_method": extraction_method.value,
            "previous_status": previous_status.value,
            "determinate": scan.fis_result.determinate if scan.fis_result else None,
            "qc_flags": [str(flag.get("code")) for flag in scan.qc_flags or []],
        },
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
    await audit.record(
        db,
        actor=current_user,
        action=audit.ACTION_SCAN_REVIEW,
        entity_type="scan",
        entity_id=scan.id,
        detail={
            "reviewed": payload.reviewed,
            "stage": scan.fis_result.stage.value if scan.fis_result.stage else None,
            "determinate": scan.fis_result.determinate,
        },
    )
    await db.refresh(scan, attribute_names=["fis_result"])
    return ScanRead.model_validate(scan)
