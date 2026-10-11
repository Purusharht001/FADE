"""Re-scoring stored scans after the calibration file changes.

This is the path by which the ML teammate's first real calibration reaches
data that is already in the database.
"""

import logging
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.cli import rescore as cli
from app.core.config import get_settings
from app.models.audit import AuditLog
from app.models.enums import DataSource
from app.models.scan import Scan
from app.repositories import patient_repo
from app.services.calibration import CalibrationError, load_calibration
from app.services.cohort_import import ImportOptions, import_csv
from app.services.rescore import RescoreSummary, rescore_all
from tests.calibration_utils import (
    TEST_REAL_VERSION,
    phantom_calibration_dict,
    write_calibration,
    write_test_real_calibration,
)

COHORT_CSV = Path(__file__).parent / "fixtures" / "cohort" / "cohort.csv"

FULL = {
    "hippocampalVolume": {"value": 5.0, "unit": "permille_ICV"},
    "ventricleBrainRatio": {"value": 0.05, "unit": "ratio"},
    "corticalThickness": {"value": 2.8, "unit": "mm"},
}


@pytest.fixture
def use_real_calibration(tmp_path, monkeypatch):
    def _activate() -> None:
        monkeypatch.setattr(
            get_settings(), "calibration_file", str(write_test_real_calibration(tmp_path))
        )

    return _activate


async def _real_scan(client: AsyncClient, derived=FULL) -> dict:
    patient = (
        await client.post("/api/v1/patients", json={"age": 72, "sex": "F", "source": "OASIS"})
    ).json()
    resp = await client.post(
        f"/api/v1/patients/{patient['id']}/scans/biomarkers", json={"derived": derived}
    )
    assert resp.status_code == 201
    return {**resp.json(), "patientId": patient["id"]}


async def _get(client: AsyncClient, scan: dict) -> dict:
    return (await client.get(f"/api/v1/patients/{scan['patientId']}/scans/{scan['id']}")).json()


class TestRescore:
    async def test_real_scan_is_staged_once_a_real_calibration_is_active(
        self, client: AsyncClient, app_db, use_real_calibration
    ):
        scan = await _real_scan(client)
        assert scan["fisResult"]["determinate"] is False
        assert scan["fisResult"]["calibrationVersion"] == "phantom.v0"
        assert [b["abnormality"] for b in scan["biomarkers"]] == [None, None, None]

        use_real_calibration()
        # Changing the file alone does not touch what is stored.
        assert (await _get(client, scan))["fisResult"]["determinate"] is False

        async with app_db() as session:
            summary = await rescore_all(session)

        after = await _get(client, scan)
        assert after["fisResult"]["determinate"] is True
        assert after["fisResult"]["stage"] is not None
        assert after["fisResult"]["severity"] is not None
        assert after["fisResult"]["indeterminateReason"] is None
        assert after["fisResult"]["calibrationVersion"] == TEST_REAL_VERSION
        assert after["calibrationVersion"] == TEST_REAL_VERSION
        assert all(b["abnormality"] is not None for b in after["biomarkers"])

        assert summary.calibration_version == TEST_REAL_VERSION
        assert (summary.scanned, summary.determinate_before, summary.determinate_after) == (1, 0, 1)
        assert summary.stage_changed == 1

    async def test_measurements_are_not_altered(
        self, client: AsyncClient, app_db, use_real_calibration
    ):
        scan = await _real_scan(client)
        before = {b["key"]: (b["value"], b["unit"]) for b in scan["biomarkers"]}
        use_real_calibration()
        async with app_db() as session:
            await rescore_all(session)

        after = await _get(client, scan)
        assert {b["key"]: (b["value"], b["unit"]) for b in after["biomarkers"]} == before
        assert after["rawVolumes"] == scan["rawVolumes"]
        assert after["extractionMethod"] == scan["extractionMethod"]
        assert after["status"] == "completed"

    async def test_a_missing_biomarker_is_still_missing(
        self, client: AsyncClient, app_db, use_real_calibration
    ):
        """A calibration cannot supply a measurement that was never taken."""
        partial = {k: v for k, v in FULL.items() if k != "corticalThickness"}
        scan = await _real_scan(client, partial)
        use_real_calibration()
        async with app_db() as session:
            summary = await rescore_all(session)

        after = await _get(client, scan)
        assert after["fisResult"]["determinate"] is False
        assert after["fisResult"]["indeterminateReason"] == "missing_biomarker:cortical_thickness"
        assert len(after["biomarkers"]) == 2
        assert summary.indeterminate_reasons == {"missing_biomarker": 1}

    async def test_qc_flags_still_block_staging(
        self, client: AsyncClient, app_db, use_real_calibration
    ):
        flagged = {**FULL, "corticalThickness": {"value": 6.0, "unit": "mm"}}
        scan = await _real_scan(client, flagged)
        use_real_calibration()
        async with app_db() as session:
            summary = await rescore_all(session)

        after = await _get(client, scan)
        assert after["fisResult"]["determinate"] is False
        assert after["fisResult"]["indeterminateReason"] == "qc_flag:thickness_out_of_range"
        assert [f["code"] for f in after["qcFlags"]] == ["thickness_out_of_range"]
        assert summary.indeterminate_reasons == {"qc_flag": 1}

    async def test_imported_cohort_is_rescored_without_reimporting(
        self, app_db, use_real_calibration
    ):
        async with app_db() as session:
            await import_csv(session, COHORT_CSV, ImportOptions())
        use_real_calibration()
        async with app_db() as session:
            summary = await rescore_all(session, source=DataSource.OASIS)

        assert summary.scanned == 5
        assert summary.determinate_after == 0  # the fixture CSV has no thickness column
        assert summary.indeterminate_reasons["missing_biomarker"] == 5
        async with app_db() as session:
            scans = (await session.execute(select(Scan))).scalars().all()
        assert {s.calibration_version for s in scans} == {TEST_REAL_VERSION}
        # Labels and identifiers survive.
        assert sorted(s.reference_label for s in scans) == [0.0, 0.0, 0.5, 0.5, 1.0]

    async def test_phantom_scans_become_indeterminate_under_a_real_calibration(
        self, client: AsyncClient, app_db, use_real_calibration
    ):
        """One calibration is active at a time. Once it is a real one, the
        synthetic demo cases are no longer staged; that is the honest answer.
        """
        patient = (await client.post("/api/v1/patients", json={"age": 70, "sex": "M"})).json()
        scan = (
            await client.post(
                f"/api/v1/patients/{patient['id']}/scans/synthetic",
                json={"severity": 0.9, "seed": 5},
            )
        ).json()
        assert scan["fisResult"]["determinate"] is True

        use_real_calibration()
        async with app_db() as session:
            summary = await rescore_all(session)

        after = (await client.get(f"/api/v1/patients/{patient['id']}/scans/{scan['id']}")).json()
        assert after["fisResult"]["determinate"] is False
        assert after["fisResult"]["indeterminateReason"].startswith("calibration_domain_mismatch")
        assert (summary.determinate_before, summary.determinate_after) == (1, 0)

    async def test_source_filter_leaves_other_sources_alone(
        self, client: AsyncClient, app_db, use_real_calibration
    ):
        real = await _real_scan(client)
        patient = (await client.post("/api/v1/patients", json={"age": 70, "sex": "M"})).json()
        phantom = (
            await client.post(
                f"/api/v1/patients/{patient['id']}/scans/synthetic",
                json={"severity": 0.9, "seed": 5},
            )
        ).json()
        use_real_calibration()
        async with app_db() as session:
            summary = await rescore_all(session, source=DataSource.OASIS)

        assert summary.scanned == 1
        assert (await _get(client, real))["fisResult"]["determinate"] is True
        untouched = (
            await client.get(f"/api/v1/patients/{patient['id']}/scans/{phantom['id']}")
        ).json()
        assert untouched["fisResult"]["calibrationVersion"] == "phantom.v0"
        assert untouched["fisResult"]["determinate"] is True

    async def test_dry_run_reports_and_changes_nothing(
        self, client: AsyncClient, app_db, use_real_calibration
    ):
        scan = await _real_scan(client)
        use_real_calibration()
        async with app_db() as session:
            summary = await rescore_all(session, dry_run=True)

        assert summary.dry_run is True
        assert (summary.determinate_before, summary.determinate_after) == (0, 1)
        after = await _get(client, scan)
        assert after["fisResult"]["determinate"] is False
        assert after["fisResult"]["calibrationVersion"] == "phantom.v0"
        async with app_db() as session:
            entries = (await session.execute(select(AuditLog))).scalars().all()
        assert [e for e in entries if e.action == "scans.rescore"] == []

    async def test_review_sign_off_is_cleared_with_the_result_it_approved(
        self, client: AsyncClient, app_db, use_real_calibration
    ):
        scan = await _real_scan(client)
        reviewed = await client.post(
            f"/api/v1/patients/{scan['patientId']}/scans/{scan['id']}/review",
            json={"reviewed": True},
        )
        assert reviewed.json()["fisResult"]["reviewed"] is True

        use_real_calibration()
        async with app_db() as session:
            await rescore_all(session)

        assert (await _get(client, scan))["fisResult"]["reviewed"] is False
        async with app_db() as session:
            entries = (await session.execute(select(AuditLog))).scalars().all()
        assert "scan.review" in {e.action for e in entries}  # the record of it remains

    async def test_rescore_is_audited(self, client: AsyncClient, app_db, use_real_calibration):
        await _real_scan(client)
        use_real_calibration()
        async with app_db() as session:
            await rescore_all(session)

        async with app_db() as session:
            entries = (await session.execute(select(AuditLog))).scalars().all()
        (entry,) = [e for e in entries if e.action == "scans.rescore"]
        assert entry.entity_type == "calibration"
        assert entry.entity_id == TEST_REAL_VERSION
        assert entry.detail == {
            "source": "all",
            "scanned": 1,
            "determinate_before": 0,
            "determinate_after": 1,
            "stage_changed": 1,
        }

    async def test_rescoring_twice_is_stable(
        self, client: AsyncClient, app_db, use_real_calibration
    ):
        scan = await _real_scan(client)
        use_real_calibration()
        async with app_db() as session:
            await rescore_all(session)
        first = (await _get(client, scan))["fisResult"]
        async with app_db() as session:
            second_summary = await rescore_all(session)
        second = (await _get(client, scan))["fisResult"]

        assert second_summary.stage_changed == 0
        assert (second["stage"], second["confidence"], second["severity"]) == (
            first["stage"],
            first["confidence"],
            first["severity"],
        )
        async with app_db() as session:
            stored = await patient_repo.get_scan(session, uuid.UUID(scan["id"]))
        assert len(stored.biomarkers) == 3  # replaced, not accumulated


class TestCalibrationVersionLabel:
    @pytest.mark.parametrize("version", ["real v1", "real/v1", "final (reviewed)", "", "x" * 65])
    def test_version_must_be_a_code_like_token(self, tmp_path, version):
        raw = phantom_calibration_dict()
        raw["version"] = version
        with pytest.raises(CalibrationError, match="`version` must be 1-64 characters"):
            load_calibration(write_calibration(tmp_path, raw))

    @pytest.mark.parametrize("version", ["real.v1", "real-2026-10", "oasis1:v3"])
    def test_ordinary_version_labels_are_accepted(self, tmp_path, version):
        raw = phantom_calibration_dict()
        raw["version"] = version
        assert load_calibration(write_calibration(tmp_path, raw)).version == version


class TestCommandLine:
    @pytest.fixture(autouse=True)
    def _restore_log_levels(self):
        loggers = [logging.getLogger(), logging.getLogger("sqlalchemy.engine")]
        levels = [logger.level for logger in loggers]
        yield
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)

    def test_prints_the_summary(self, monkeypatch, capsys):
        seen = {}

        async def _fake_run(args):
            seen["args"] = args
            summary = RescoreSummary(calibration_version="real.v1", dry_run=args.dry_run)
            summary.scanned, summary.determinate_after = 4, 3
            summary.indeterminate_reasons.update(["missing_biomarker"])
            return summary

        monkeypatch.setattr(cli, "_run", _fake_run)
        monkeypatch.setattr(cli, "configure_logging", lambda: None)

        code = cli.main(["--dry-run", "--source", "OASIS"])
        out = capsys.readouterr().out

        assert code == 0
        assert "Re-scored with calibration real.v1 (DRY RUN: nothing was written)" in out
        assert "staged after:        3" in out
        assert "still indeterminate: 1" in out
        assert "missing_biomarker" in out
        assert (seen["args"].dry_run, seen["args"].source) == (True, "OASIS")
        out.encode("ascii")

    def test_bad_calibration_file_is_a_clean_failure(self, monkeypatch, capsys):
        async def _failing(args):
            raise CalibrationError("Calibration file real.v1.json: is not valid JSON")

        monkeypatch.setattr(cli, "_run", _failing)
        monkeypatch.setattr(cli, "configure_logging", lambda: None)
        assert cli.main([]) == cli.EXIT_ERROR
        assert "Re-score failed: Calibration file real.v1.json" in capsys.readouterr().err
