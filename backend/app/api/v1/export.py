"""Research export. Researchers and administrators only."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.api.deps import DbSession, require_role
from app.models.enums import DataSource, UserRole
from app.models.user import User
from app.repositories import study_repo
from app.services import audit, export_service

router = APIRouter(prefix="/export", tags=["export"])

Researcher = Annotated[User, Depends(require_role(UserRole.RESEARCHER, UserRole.ADMIN))]


@router.get(
    "/results.csv",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}, "description": "One row per scan."}},
)
async def export_results_csv(
    db: DbSession, researcher: Researcher, source: DataSource | None = None
) -> Response:
    """One row per scan: provenance, raw and derived biomarkers, QC flags,
    the engine's output, the calibration and engine versions, the reference
    label, and the latest clinician rating. For evaluation; this is the
    only endpoint that returns the reference label.

    `source` limits the export to one data source (for example `OASIS`).
    Every export is recorded in the audit log.
    """
    scans = await study_repo.list_all_scans_for_export(db, source)
    ratings = await study_repo.all_ratings(db)
    columns, rows = export_service.build_rows(scans, ratings)

    await audit.record(
        db,
        actor=researcher,
        action=audit.ACTION_EXPORT_RESULTS,
        entity_type="export",
        detail={"rows": len(rows), "source": source.value if source else "all"},
    )
    return Response(
        content=export_service.render_csv(columns, rows),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="fade_results.csv"',
            # Holds reference labels and ratings: not for shared caches.
            "Cache-Control": "no-store",
        },
    )
