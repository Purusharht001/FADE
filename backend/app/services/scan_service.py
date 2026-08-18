"""Ties the preprocessing/volumetry/FIS pipeline to persistence: run the
pipeline for a scan, then write biomarkers + FIS result to the database in
one place so both the file-upload and synthetic-demo entry points share
identical persistence behavior.
"""

import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import UnprocessableScanError
from app.core.logging import get_logger
from app.models.enums import ScanStatus
from app.models.scan import BiomarkerReading, FISResult, Scan
from app.repositories import patient_repo
from app.services.biomarkers import BIOMARKER_DEFS
from app.services.pipeline import PipelineResult, run_pipeline_from_file, run_pipeline_synthetic

logger = get_logger(__name__)


async def _persist_result(db: AsyncSession, scan: Scan, result: PipelineResult) -> Scan:
    readings = [
        BiomarkerReading(
            key=key,
            value=value,
            unit=BIOMARKER_DEFS[key].unit,
            abnormality=result.abnormality[key],
        )
        for key, value in result.biomarkers.items()
    ]
    await patient_repo.replace_biomarkers(db, scan, readings)

    fis = result.fis_output
    fis_result = FISResult(
        stage=fis.stage,
        confidence=fis.confidence,
        uncertainty=fis.uncertainty,
        needs_review=fis.needs_review,
        membership={stage.value: degree for stage, degree in fis.membership.items()},
        fired_rules=[
            {
                "id": r.id,
                "antecedent": r.antecedent,
                "consequent": r.consequent.value,
                "firing_strength": r.firing_strength,
            }
            for r in fis.fired_rules
        ],
    )
    await patient_repo.upsert_fis_result(db, scan, fis_result)

    scan.status = ScanStatus.COMPLETED
    scan.failure_reason = None
    await db.flush()
    await db.refresh(scan, attribute_names=["biomarkers", "fis_result"])
    return scan


async def process_uploaded_scan(db: AsyncSession, scan: Scan, file_path: str) -> Scan:
    scan.status = ScanStatus.PREPROCESSING
    await db.flush()
    try:
        result = run_pipeline_from_file(file_path)
    except UnprocessableScanError as exc:
        scan.status = ScanStatus.FAILED
        scan.failure_reason = str(exc)
        await db.flush()
        logger.warning("scan_processing_failed", scan_id=str(scan.id), reason=str(exc))
        raise

    return await _persist_result(db, scan, result)


async def process_synthetic_scan(
    db: AsyncSession, scan: Scan, severity: float, *, seed: int | None
) -> Scan:
    settings = get_settings()
    scan.status = ScanStatus.PREPROCESSING
    await db.flush()

    save_path = None
    if seed is not None:
        save_path = Path(settings.upload_dir) / f"synthetic-{scan.id}.nii.gz"

    try:
        result = run_pipeline_synthetic(severity, seed=seed, save_to=save_path)
    except UnprocessableScanError as exc:
        scan.status = ScanStatus.FAILED
        scan.failure_reason = str(exc)
        await db.flush()
        raise

    if save_path is not None:
        scan.file_path = str(save_path)

    return await _persist_result(db, scan, result)


def new_scan_id() -> uuid.UUID:
    return uuid.uuid4()
