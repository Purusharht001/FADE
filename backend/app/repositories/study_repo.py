import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import DataSource, ScanStatus, Stage
from app.models.patient import Patient
from app.models.scan import Scan
from app.models.study import ClinicianRating


async def list_completed_scans(
    db: AsyncSession, *, include_phantom: bool, with_results: bool = False
) -> list[Scan]:
    """Completed scans with their patient and biomarkers. The FIS result is
    loaded only on request, so the study listing cannot serialize it even
    by mistake.
    """
    options = [selectinload(Scan.patient), selectinload(Scan.biomarkers)]
    if with_results:
        options.append(selectinload(Scan.fis_result))
    stmt = (
        select(Scan)
        .join(Patient, Scan.patient_id == Patient.id)
        .where(Scan.status == ScanStatus.COMPLETED)
        .options(*options)
    )
    if not include_phantom:
        stmt = stmt.where(Patient.source != DataSource.PHANTOM)
    return list((await db.execute(stmt)).scalars().unique().all())


async def list_all_scans_for_export(db: AsyncSession, source: DataSource | None) -> list[Scan]:
    stmt = (
        select(Scan)
        .join(Patient, Scan.patient_id == Patient.id)
        .options(
            selectinload(Scan.patient), selectinload(Scan.biomarkers), selectinload(Scan.fis_result)
        )
        .order_by(Patient.display_id, Scan.scan_date, Scan.created_at)
    )
    if source is not None:
        stmt = stmt.where(Patient.source == source)
    return list((await db.execute(stmt)).scalars().unique().all())


async def get_completed_scan(db: AsyncSession, scan_id: uuid.UUID) -> Scan | None:
    result = await db.execute(
        select(Scan).where(Scan.id == scan_id, Scan.status == ScanStatus.COMPLETED)
    )
    return result.scalar_one_or_none()


async def get_rating(
    db: AsyncSession, scan_id: uuid.UUID, rater_id: uuid.UUID
) -> ClinicianRating | None:
    result = await db.execute(
        select(ClinicianRating).where(
            ClinicianRating.scan_id == scan_id, ClinicianRating.rater_id == rater_id
        )
    )
    return result.scalar_one_or_none()


async def ratings_by_scan_for_rater(
    db: AsyncSession, rater_id: uuid.UUID
) -> dict[uuid.UUID, ClinicianRating]:
    result = await db.execute(select(ClinicianRating).where(ClinicianRating.rater_id == rater_id))
    return {rating.scan_id: rating for rating in result.scalars().all()}


async def all_ratings(db: AsyncSession) -> list[ClinicianRating]:
    """Every rating, oldest change first, so the last one seen per scan is
    the latest.
    """
    result = await db.execute(
        select(ClinicianRating).order_by(ClinicianRating.updated_at, ClinicianRating.created_at)
    )
    return list(result.scalars().all())


async def create_rating(
    db: AsyncSession,
    *,
    scan_id: uuid.UUID,
    rater_id: uuid.UUID,
    stage_called: Stage,
    borderline: bool,
    confidence: int,
    notes: str | None,
) -> ClinicianRating:
    rating = ClinicianRating(
        scan_id=scan_id,
        rater_id=rater_id,
        stage_called=stage_called,
        borderline=borderline,
        confidence=confidence,
        notes=notes,
    )
    db.add(rating)
    await db.flush()
    return rating
