"""Re-running the engine on scans that are already stored.

Results are computed once, when a scan is ingested, with whatever
calibration was active then. When the calibration file changes (most
importantly, when the first real one replaces the phantom default) the
stored results do not change by themselves, and the cohort import will
not re-import subjects it already has. This module re-scores them in
place from the stored biomarker readings, so no file has to be uploaded
or parsed again.

Re-scoring replaces a scan's result. A clinician's review sign-off on the
old result is therefore cleared (the audit log keeps the record of it);
clinician study ratings are separate rows and are not touched.
"""

from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import DataSource
from app.repositories import study_repo
from app.services import audit, scan_service
from app.services.calibration import get_calibration

logger = get_logger(__name__)

ACTION_RESCORE = "scans.rescore"


@dataclass(slots=True)
class RescoreSummary:
    calibration_version: str
    dry_run: bool = False
    scanned: int = 0
    determinate_before: int = 0
    determinate_after: int = 0
    stage_changed: int = 0
    # Why the scans that are still unstaged are unstaged, by reason code.
    indeterminate_reasons: Counter[str] = field(default_factory=Counter)

    def format(self) -> str:
        heading = f"Re-scored with calibration {self.calibration_version}"
        if self.dry_run:
            heading += " (DRY RUN: nothing was written)"
        lines = [
            heading,
            f"  scans:               {self.scanned}",
            f"  staged before:       {self.determinate_before}",
            f"  staged after:        {self.determinate_after}",
            f"  stage changed:       {self.stage_changed}",
            f"  still indeterminate: {self.scanned - self.determinate_after}",
        ]
        for reason, count in sorted(self.indeterminate_reasons.items()):
            lines.append(f"    {count:>5}  {reason}")
        return "\n".join(lines)


def _reason_codes(reason: str | None) -> list[str]:
    """`a:detail; b` -> ["a", "b"]: the codes without their per-scan detail."""
    return [part.split(":", 1)[0].strip() for part in (reason or "").split(";") if part.strip()]


async def rescore_all(
    db: AsyncSession, *, source: DataSource | None = None, dry_run: bool = False
) -> RescoreSummary:
    """Re-scores every completed scan (optionally of one data source) under
    the active calibration. Commits, or rolls back if `dry_run`.
    """
    calibration = get_calibration()
    summary = RescoreSummary(calibration_version=calibration.version, dry_run=dry_run)
    scans = await study_repo.list_completed_scans(db, include_phantom=True, with_results=True)

    try:
        for scan in scans:
            patient = scan.patient
            if source is not None and patient.source != source:
                continue
            before = scan.fis_result
            stage_before = before.stage if before is not None else None
            if before is not None and before.determinate:
                summary.determinate_before += 1

            scan = await scan_service.rescore_scan(db, scan, patient, calibration=calibration)
            after = scan.fis_result
            assert after is not None  # rescore_scan always writes a result
            summary.scanned += 1
            if after.determinate:
                summary.determinate_after += 1
            else:
                summary.indeterminate_reasons.update(_reason_codes(after.indeterminate_reason))
            if after.stage != stage_before:
                summary.stage_changed += 1

        if dry_run:
            await db.rollback()
        else:
            await audit.record(
                db,
                actor=None,
                action=ACTION_RESCORE,
                entity_type="calibration",
                entity_id=calibration.version,
                detail={
                    "source": source.value if source else "all",
                    "scanned": summary.scanned,
                    "determinate_before": summary.determinate_before,
                    "determinate_after": summary.determinate_after,
                    "stage_changed": summary.stage_changed,
                },
            )
            await db.commit()
    except Exception:
        await db.rollback()
        raise

    logger.info(
        "scans_rescored",
        calibration=calibration.version,
        dry_run=dry_run,
        scanned=summary.scanned,
        determinate_after=summary.determinate_after,
    )
    return summary
