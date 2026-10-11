"""Phase 5: the audit log. Entries are written for review sign-off,
ratings, exports and stats attachment, and the log cannot hold free text.
"""

import datetime
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.enums import DataSource, ScanStatus, Sex, UserRole
from app.repositories import patient_repo, user_repo
from app.services import audit
from app.services.audit import UnsafeAuditDetailError, validate_detail
from app.services.cohort_import import ImportOptions, import_csv

FIXTURES = Path(__file__).parent / "fixtures"
FREESURFER = FIXTURES / "freesurfer"
COHORT_CSV = FIXTURES / "cohort" / "cohort.csv"


async def _entries(app_db, action: str | None = None) -> list[AuditLog]:
    async with app_db() as session:
        stmt = select(AuditLog).order_by(AuditLog.created_at)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        return list((await session.execute(stmt)).scalars().all())


class TestDetailValidation:
    def test_numbers_booleans_nulls_and_tokens_are_accepted(self):
        detail = {
            "rows": 12,
            "severity": 0.42,
            "reviewed": True,
            "stage": "MCI",
            "previous_stage": None,
            "scan": "0f8fad5b-d9cb-469f-a165-70867728950e",
            "calibration": "phantom.v0",
            "codes": ["icv_out_of_range", "thickness_out_of_range"],
        }
        assert validate_detail(detail) == detail

    def test_none_detail(self):
        assert validate_detail(None) is None

    @pytest.mark.parametrize(
        "value",
        [
            "Jane Doe",
            "Doe, Jane",
            "born 1940/01/01",
            "patient reports memory loss since March",
            "x" * 65,
            "email@example.com",
        ],
    )
    def test_free_text_is_refused(self, value):
        with pytest.raises(UnsafeAuditDetailError) as excinfo:
            validate_detail({"notes": value})
        # The refusal names the key and does not repeat the value.
        assert "notes" in str(excinfo.value)
        assert value not in str(excinfo.value)

    def test_free_text_inside_a_list_is_refused(self):
        with pytest.raises(UnsafeAuditDetailError):
            validate_detail({"codes": ["ok_code", "Jane Doe"]})

    @pytest.mark.parametrize("value", [{"nested": "x"}, [["a"]], [{"a": 1}], b"bytes", 1 + 2j])
    def test_nested_and_unsupported_values_are_refused(self, value):
        with pytest.raises(UnsafeAuditDetailError):
            validate_detail({"field": value})

    def test_long_list_is_refused(self):
        with pytest.raises(UnsafeAuditDetailError, match="too long"):
            validate_detail({"codes": ["a"] * 33})

    def test_free_text_key_is_refused(self):
        with pytest.raises(UnsafeAuditDetailError, match="keys"):
            validate_detail({"Jane Doe": 1})


class TestRecord:
    async def test_entry_captures_actor_role_action_and_entity(self, db_session):
        from app.core.security import hash_password

        user = await user_repo.create(
            db_session,
            email="r@test.dev",
            hashed_password=hash_password("x" * 12),
            full_name="R",
            role=UserRole.RESEARCHER,
        )
        entry = await audit.record(
            db_session,
            actor=user,
            action=audit.ACTION_EXPORT_RESULTS,
            entity_type="export",
            detail={"rows": 3},
        )
        await db_session.commit()

        assert entry.actor_id == user.id
        assert entry.actor_role == "researcher"
        assert entry.action == "export.results_csv"
        assert entry.entity_type == "export"
        assert entry.entity_id is None
        assert entry.detail == {"rows": 3}
        assert entry.created_at is not None

    async def test_unsafe_detail_writes_nothing(self, db_session):
        with pytest.raises(UnsafeAuditDetailError):
            await audit.record(
                db_session, actor=None, action="x.y", detail={"notes": "Jane Doe, 1940"}
            )
        await db_session.commit()
        assert (await db_session.execute(select(AuditLog))).scalars().all() == []

    def test_table_has_no_free_text_column(self):
        """Every string column is bounded and code-sized."""
        lengths = {c.name: getattr(c.type, "length", None) for c in AuditLog.__table__.columns}
        assert lengths["action"] == 64
        assert lengths["entity_id"] == 64
        assert lengths["entity_type"] == 32
        assert lengths["actor_role"] == 20


class TestAuditedActions:
    async def test_review_sign_off_is_audited(self, client: AsyncClient, app_db):
        patient = (await client.post("/api/v1/patients", json={"age": 70, "sex": "F"})).json()
        scan = (
            await client.post(
                f"/api/v1/patients/{patient['id']}/scans/synthetic",
                json={"severity": 0.9, "seed": 5},
            )
        ).json()
        url = f"/api/v1/patients/{patient['id']}/scans/{scan['id']}/review"

        await client.post(url, json={"reviewed": True})
        await client.post(url, json={"reviewed": False})

        entries = await _entries(app_db, "scan.review")
        assert [e.detail["reviewed"] for e in entries] == [True, False]
        assert {e.entity_id for e in entries} == {scan["id"]}
        assert {e.entity_type for e in entries} == {"scan"}
        assert entries[0].detail["stage"] == scan["fisResult"]["stage"]
        assert entries[0].actor_id is not None
        assert entries[0].actor_role == "clinician"

    async def test_stats_attachment_is_audited(self, client: AsyncClient, app_db):
        async with app_db() as session:
            patient = await patient_repo.create(
                session, age=70, sex=Sex.M, source=DataSource.CLINIC
            )
            scan = await patient_repo.create_scan(
                session, patient_id=patient.id, scan_date=datetime.date.today(), file_path=None
            )
            scan.status = ScanStatus.AWAITING_VOLUMETRY
            await session.commit()
            patient_id, scan_id = str(patient.id), str(scan.id)

        resp = await client.post(
            f"/api/v1/patients/{patient_id}/scans/{scan_id}/stats",
            files={"aseg": ("aseg.stats", (FREESURFER / "aseg.stats").read_bytes(), "text/plain")},
        )
        assert resp.status_code == 200

        (entry,) = await _entries(app_db, "scan.stats_attached")
        assert entry.entity_id == scan_id
        assert entry.detail == {
            "extraction_method": "freesurfer_stats",
            "previous_status": "awaiting_volumetry",
            "determinate": False,
            "qc_flags": ["cortical_thickness_unavailable"],
        }

    async def test_a_rejected_attachment_is_not_audited(self, client: AsyncClient, app_db):
        async with app_db() as session:
            patient = await patient_repo.create(
                session, age=70, sex=Sex.M, source=DataSource.CLINIC
            )
            scan = await patient_repo.create_scan(
                session, patient_id=patient.id, scan_date=datetime.date.today(), file_path=None
            )
            scan.status = ScanStatus.AWAITING_VOLUMETRY
            await session.commit()
            patient_id, scan_id = str(patient.id), str(scan.id)

        resp = await client.post(
            f"/api/v1/patients/{patient_id}/scans/{scan_id}/stats",
            files={"aseg": ("aseg.stats", b"garbage", "text/plain")},
        )
        assert resp.status_code == 422
        assert await _entries(app_db, "scan.stats_attached") == []

    async def test_cohort_import_is_audited_with_counts_only(self, db_session):
        await import_csv(db_session, COHORT_CSV, ImportOptions())
        (entry,) = (await db_session.execute(select(AuditLog))).scalars().all()

        assert entry.action == "cohort.import"
        assert entry.actor_id is None
        assert entry.detail == {
            "source": "OASIS",
            "imported": 5,
            "skipped_no_label": 2,
            "skipped_mr2": 1,
            "skipped_existing": 0,
            "skipped_invalid": 1,
            "qc_flagged": 2,
        }

    async def test_dry_run_import_leaves_no_audit_entry(self, db_session):
        await import_csv(db_session, COHORT_CSV, ImportOptions(dry_run=True))
        assert (await db_session.execute(select(AuditLog))).scalars().all() == []
