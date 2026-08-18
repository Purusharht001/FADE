import datetime
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import DataSource, Sex, Stage
from app.models.patient import Patient
from app.models.scan import BiomarkerReading, FISResult, Scan

_PATIENT_WITH_SCANS = selectinload(Patient.scans).options(
    selectinload(Scan.biomarkers), selectinload(Scan.fis_result)
)


async def list_patients(db: AsyncSession) -> list[Patient]:
    result = await db.execute(
        select(Patient).options(_PATIENT_WITH_SCANS).order_by(Patient.display_id)
    )
    return list(result.scalars().unique().all())


async def get_by_id(db: AsyncSession, patient_id: uuid.UUID) -> Patient | None:
    result = await db.execute(
        select(Patient).where(Patient.id == patient_id).options(_PATIENT_WITH_SCANS)
    )
    return result.scalar_one_or_none()


async def get_by_display_id(db: AsyncSession, display_id: str) -> Patient | None:
    result = await db.execute(
        select(Patient).where(Patient.display_id == display_id).options(_PATIENT_WITH_SCANS)
    )
    return result.scalar_one_or_none()


async def next_display_id(db: AsyncSession) -> str:
    result = await db.execute(select(func.count()).select_from(Patient))
    count = result.scalar_one()
    return f"PT-{1000 + count + 1}"


async def create(
    db: AsyncSession,
    *,
    age: int,
    sex: Sex,
    source: DataSource,
    created_by_id: uuid.UUID | None = None,
) -> Patient:
    patient = Patient(
        display_id=await next_display_id(db),
        age=age,
        sex=sex,
        source=source,
        created_by_id=created_by_id,
        scans=[],  # explicitly "loaded empty" — a brand new patient has none;
        # without this, accessing `.scans` to serialize the response would
        # trigger a lazy DB load, which raises MissingGreenlet under asyncio.
    )
    db.add(patient)
    await db.flush()
    return patient


async def get_scan(db: AsyncSession, scan_id: uuid.UUID) -> Scan | None:
    result = await db.execute(
        select(Scan)
        .where(Scan.id == scan_id)
        .options(selectinload(Scan.biomarkers), selectinload(Scan.fis_result))
    )
    return result.scalar_one_or_none()


async def create_scan(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    scan_date: datetime.date,
    file_path: str | None,
    modality: str = "T1w",
) -> Scan:
    scan = Scan(
        patient_id=patient_id,
        scan_date=scan_date,
        file_path=file_path,
        modality=modality,
        biomarkers=[],  # explicitly "loaded empty"/"loaded None" — see
        fis_result=None,  # patient_repo.create()'s comment for why this matters.
    )
    db.add(scan)
    await db.flush()
    return scan


async def replace_biomarkers(
    db: AsyncSession, scan: Scan, readings: list[BiomarkerReading]
) -> None:
    scan.biomarkers.clear()
    await db.flush()
    for reading in readings:
        reading.scan_id = scan.id
        db.add(reading)
    await db.flush()


async def upsert_fis_result(db: AsyncSession, scan: Scan, result: FISResult) -> FISResult:
    result.scan_id = scan.id
    if scan.fis_result is not None:
        await db.delete(scan.fis_result)
        await db.flush()
    db.add(result)
    await db.flush()
    await db.refresh(result)
    return result


async def cohort_stats(db: AsyncSession) -> dict:
    total_patients = (await db.execute(select(func.count()).select_from(Patient))).scalar_one()
    total_scans = (await db.execute(select(func.count()).select_from(Scan))).scalar_one()
    needs_review = (
        await db.execute(
            select(func.count()).select_from(FISResult).where(FISResult.needs_review.is_(True))
        )
    ).scalar_one()
    avg_confidence = (await db.execute(select(func.avg(FISResult.confidence)))).scalar() or 0.0

    by_stage: dict[Stage, int] = dict.fromkeys(Stage, 0)
    rows = await db.execute(select(FISResult.stage, func.count()).group_by(FISResult.stage))
    for stage, count in rows.all():
        by_stage[stage] = count

    return {
        "total_patients": total_patients,
        "total_scans": total_scans,
        "needs_review": needs_review,
        "avg_confidence": round(float(avg_confidence), 1),
        "by_stage": by_stage,
    }
