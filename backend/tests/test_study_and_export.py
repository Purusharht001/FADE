"""Phase 5: the blind clinician-rating study and the research export."""

import csv
import io
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.models.audit import AuditLog
from app.models.enums import UserRole
from app.models.study import ClinicianRating
from app.services.cohort_import import ImportOptions, import_csv
from app.services.export_service import flatten_raw_volumes, render_csv
from tests.calibration_utils import TEST_REAL_VERSION, write_test_real_calibration
from tests.conftest import login_as

FIXTURES = Path(__file__).parent / "fixtures"
COHORT_CSV = FIXTURES / "cohort" / "cohort.csv"

# Words that would mean engine output, the reference label or a
# re-identifying handle had reached a rater.
BLINDED_TERMS = (
    "stage",
    "severity",
    "uncertainty",
    "membership",
    "firedRules",
    "abnormality",
    "needsReview",
    "determinate",
    "indeterminate",
    "referenceLabel",
    "reference_label",
    "externalSubjectId",
    "displayId",
    "patientId",
    "source",
    "qcFlags",
    "calibration",
    "scanDate",
)

DERIVED = {
    "hippocampalVolume": {"value": 5.0, "unit": "permille_ICV"},
    "ventricleBrainRatio": {"value": 0.05, "unit": "ratio"},
    "corticalThickness": {"value": 2.8, "unit": "mm"},
}


async def _real_scan(client: AsyncClient, *, subject: str | None = None, age: int = 72) -> dict:
    patient = (
        await client.post("/api/v1/patients", json={"age": age, "sex": "F", "source": "OASIS"})
    ).json()
    body: dict = {"derived": DERIVED}
    if subject:
        body["externalSubjectId"] = subject
    resp = await client.post(f"/api/v1/patients/{patient['id']}/scans/biomarkers", json=body)
    assert resp.status_code == 201
    return resp.json()


async def _phantom_scan(client: AsyncClient) -> dict:
    patient = (await client.post("/api/v1/patients", json={"age": 70, "sex": "M"})).json()
    return (
        await client.post(
            f"/api/v1/patients/{patient['id']}/scans/synthetic", json={"severity": 0.9, "seed": 5}
        )
    ).json()


async def _import_cohort(app_db) -> None:
    async with app_db() as session:
        await import_csv(session, COHORT_CSV, ImportOptions())


def _rating(stage="MCI", borderline=False, confidence=3, notes=None) -> dict:
    body = {"stageCalled": stage, "borderline": borderline, "confidence": confidence}
    if notes is not None:
        body["notes"] = notes
    return body


def _parse(resp) -> list[dict]:
    return list(csv.DictReader(io.StringIO(resp.text)))


class TestStudyCases:
    async def test_cases_are_real_completed_scans_shown_as_biomarkers(self, client: AsyncClient):
        scan = await _real_scan(client, subject="FAKE_0001_MR1", age=81)
        await _phantom_scan(client)

        resp = await client.get("/api/v1/study/cases")
        assert resp.status_code == 200
        (case,) = resp.json()

        assert case["scanId"] == scan["id"]
        assert case["caseCode"] == f"CASE-{scan['id'].replace('-', '')[:8].upper()}"
        assert case["age"] == 81
        assert case["sex"] == "F"
        assert case["myRating"] is None
        assert case["biomarkers"] == [
            {"key": "cortical_thickness", "value": 2.8, "unit": "mm"},
            {"key": "hippocampal_volume", "value": 5.0, "unit": "‰ICV"},
            {"key": "ventricle_brain_ratio", "value": 0.05, "unit": "ratio"},
        ]

    async def test_response_shape_is_exactly_the_blinded_fields(self, client: AsyncClient):
        await _real_scan(client, subject="FAKE_0001_MR1")
        (case,) = (await client.get("/api/v1/study/cases")).json()

        assert set(case) == {"scanId", "caseCode", "age", "sex", "biomarkers", "myRating"}
        assert all(set(b) == {"key", "value", "unit"} for b in case["biomarkers"])

    async def test_engine_output_and_reference_label_are_hidden(
        self, client: AsyncClient, app_db, tmp_path, monkeypatch
    ):
        """With a calibration that does stage real data, so there is engine
        output to leak, and with reference labels in the database.
        """
        monkeypatch.setattr(
            get_settings(), "calibration_file", str(write_test_real_calibration(tmp_path))
        )
        await _import_cohort(app_db)
        staged = await _real_scan(client, subject="FAKE_9001_MR1")
        assert staged["fisResult"]["stage"] is not None

        resp = await client.get("/api/v1/study/cases")
        assert len(resp.json()) == 6
        for term in BLINDED_TERMS:
            assert term not in resp.text, term
        # Not the identifiers, not the calibration name, not a stage value.
        for leak in ("FAKE_", "PT-", TEST_REAL_VERSION, '"AD"', '"MCI"', '"CN"', "OASIS"):
            assert leak not in resp.text, leak

    async def test_phantom_cases_only_on_request(self, client: AsyncClient):
        phantom = await _phantom_scan(client)
        assert (await client.get("/api/v1/study/cases")).json() == []

        with_phantom = (
            await client.get("/api/v1/study/cases", params={"includePhantom": "true"})
        ).json()
        assert [c["scanId"] for c in with_phantom] == [phantom["id"]]

    async def test_scans_without_a_result_are_not_cases(self, client: AsyncClient, tmp_path):
        import nibabel as nib
        import numpy as np

        patient = (
            await client.post("/api/v1/patients", json={"age": 70, "sex": "M", "source": "Clinic"})
        ).json()
        path = tmp_path / "s.nii.gz"
        nib.save(nib.Nifti1Image(np.ones((8, 8, 8), dtype=np.int16), np.eye(4)), str(path))
        upload = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/upload",
            files={"file": ("s.nii.gz", path.read_bytes(), "application/octet-stream")},
        )
        assert upload.json()["status"] == "awaiting_volumetry"
        assert (await client.get("/api/v1/study/cases")).json() == []

        rating = await client.post(
            f"/api/v1/study/cases/{upload.json()['id']}/rating", json=_rating()
        )
        assert rating.status_code == 404

    async def test_order_is_stable_per_rater_and_differs_between_raters(
        self, client: AsyncClient, app_db
    ):
        created = [(await _real_scan(client))["id"] for _ in range(8)]
        first = await login_as(client, app_db, UserRole.CLINICIAN, "one@test.dev")
        second = await login_as(client, app_db, UserRole.CLINICIAN, "two@test.dev")

        async def order(headers):
            cases = (await client.get("/api/v1/study/cases", headers=headers)).json()
            return [c["scanId"] for c in cases]

        order_one, order_two = await order(first), await order(second)
        assert order_one == await order(first)
        assert sorted(order_one) == sorted(order_two) == sorted(created)
        assert order_one != order_two
        assert order_one != created  # not creation (import) order


class TestRatings:
    async def test_first_rating_is_created_then_updates_replace_it(
        self, client: AsyncClient, app_db
    ):
        scan = await _real_scan(client)
        url = f"/api/v1/study/cases/{scan['id']}/rating"

        created = await client.post(url, json=_rating("MCI", True, 2, "between CN and MCI"))
        assert created.status_code == 201
        assert created.json() == {
            "scanId": scan["id"],
            "stageCalled": "MCI",
            "borderline": True,
            "confidence": 2,
            "notes": "between CN and MCI",
            "createdAt": created.json()["createdAt"],
            "updatedAt": created.json()["updatedAt"],
        }

        updated = await client.post(url, json=_rating("AD", False, 5))
        assert updated.status_code == 200
        assert updated.json()["stageCalled"] == "AD"
        assert updated.json()["confidence"] == 5
        assert updated.json()["notes"] is None
        assert updated.json()["createdAt"] == created.json()["createdAt"]

        async with app_db() as session:
            ratings = (await session.execute(select(ClinicianRating))).scalars().all()
        assert len(ratings) == 1  # one per rater per scan
        assert ratings[0].stage_called == "AD"

    async def test_rating_response_carries_no_engine_output(self, client: AsyncClient):
        scan = await _real_scan(client)
        resp = await client.post(f"/api/v1/study/cases/{scan['id']}/rating", json=_rating())
        assert set(resp.json()) == {
            "scanId",
            "stageCalled",
            "borderline",
            "confidence",
            "notes",
            "createdAt",
            "updatedAt",
        }

    async def test_own_rating_is_shown_back_and_unrated_only_filters(self, client: AsyncClient):
        rated = await _real_scan(client)
        unrated = await _real_scan(client)
        await client.post(f"/api/v1/study/cases/{rated['id']}/rating", json=_rating("CN", False, 4))

        cases = {c["scanId"]: c for c in (await client.get("/api/v1/study/cases")).json()}
        assert cases[rated["id"]]["myRating"]["stageCalled"] == "CN"
        assert cases[unrated["id"]]["myRating"] is None

        remaining = (await client.get("/api/v1/study/cases", params={"unratedOnly": "true"})).json()
        assert [c["scanId"] for c in remaining] == [unrated["id"]]

    async def test_each_rater_has_their_own_rating_and_cannot_see_others(
        self, client: AsyncClient, app_db
    ):
        scan = await _real_scan(client)
        url = f"/api/v1/study/cases/{scan['id']}/rating"
        first = await login_as(client, app_db, UserRole.CLINICIAN, "one@test.dev")
        second = await login_as(client, app_db, UserRole.CLINICIAN, "two@test.dev")

        assert (await client.post(url, json=_rating("CN"), headers=first)).status_code == 201
        assert (await client.post(url, json=_rating("AD"), headers=second)).status_code == 201

        async with app_db() as session:
            ratings = (await session.execute(select(ClinicianRating))).scalars().all()
        assert sorted(r.stage_called.value for r in ratings) == ["AD", "CN"]

        (mine,) = (await client.get("/api/v1/study/cases", headers=first)).json()
        assert mine["myRating"]["stageCalled"] == "CN"
        (theirs,) = (await client.get("/api/v1/study/cases", headers=second)).json()
        assert theirs["myRating"]["stageCalled"] == "AD"

    @pytest.mark.parametrize(
        "body",
        [
            _rating(confidence=0),
            _rating(confidence=6),
            _rating(confidence=2.5),
            _rating(stage="SEVERE"),
            {"borderline": False, "confidence": 3},
            {"stageCalled": "CN", "borderline": False},
            _rating(notes="x" * 2001),
        ],
    )
    async def test_invalid_ratings_are_rejected(self, client: AsyncClient, body):
        scan = await _real_scan(client)
        resp = await client.post(f"/api/v1/study/cases/{scan['id']}/rating", json=body)
        assert resp.status_code == 422

    async def test_unknown_case(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/study/cases/00000000-0000-0000-0000-000000000000/rating", json=_rating()
        )
        assert resp.status_code == 404

    async def test_rating_changes_are_audited_with_the_previous_call(
        self, client: AsyncClient, app_db
    ):
        scan = await _real_scan(client)
        url = f"/api/v1/study/cases/{scan['id']}/rating"
        note = "Jane's daughter says onset was last spring"
        await client.post(url, json=_rating("MCI", True, 2, note))
        await client.post(url, json=_rating("AD", False, 5, note))

        async with app_db() as session:
            entries = (
                (await session.execute(select(AuditLog).order_by(AuditLog.created_at)))
                .scalars()
                .all()
            )
        entries = [e for e in entries if e.action.startswith("rating.")]

        assert [e.action for e in entries] == ["rating.create", "rating.update"]
        assert {e.entity_id for e in entries} == {scan["id"]}
        assert entries[0].detail == {
            "stage_called": "MCI",
            "borderline": True,
            "confidence": 2,
            "has_notes": True,
        }
        assert entries[1].detail == {
            "stage_called": "AD",
            "borderline": False,
            "confidence": 5,
            "previous_stage_called": "MCI",
            "previous_borderline": True,
            "previous_confidence": 2,
            "notes_changed": False,
        }
        # The free-text note itself is nowhere in the audit log.
        assert all("Jane" not in str(e.detail) for e in entries)


class TestStudyRoles:
    @pytest.mark.parametrize("role", [UserRole.RESEARCHER, UserRole.ADMIN])
    async def test_only_clinicians_rate(self, client: AsyncClient, app_db, role):
        scan = await _real_scan(client)
        headers = await login_as(client, app_db, role)

        listing = await client.get("/api/v1/study/cases", headers=headers)
        rating = await client.post(
            f"/api/v1/study/cases/{scan['id']}/rating", json=_rating(), headers=headers
        )
        assert listing.status_code == 403
        assert rating.status_code == 403
        assert rating.json()["error"]["code"] == "forbidden"

    async def test_clinician_token_is_accepted(self, client: AsyncClient, app_db):
        await _real_scan(client)
        headers = await login_as(client, app_db, UserRole.CLINICIAN)
        assert (await client.get("/api/v1/study/cases", headers=headers)).status_code == 200


class TestExportAccess:
    async def test_clinicians_cannot_export(self, client: AsyncClient, app_db):
        anonymous = await client.get("/api/v1/export/results.csv")
        assert anonymous.status_code == 403  # the shared demo user is a clinician

        headers = await login_as(client, app_db, UserRole.CLINICIAN)
        resp = await client.get("/api/v1/export/results.csv", headers=headers)
        assert resp.status_code == 403
        assert "referenceLabel" not in resp.text and "reference_label" not in resp.text

    @pytest.mark.parametrize("role", [UserRole.RESEARCHER, UserRole.ADMIN])
    async def test_researchers_and_admins_can(self, client: AsyncClient, app_db, role):
        headers = await login_as(client, app_db, role)
        resp = await client.get("/api/v1/export/results.csv", headers=headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/csv; charset=utf-8"
        assert resp.headers["content-disposition"] == 'attachment; filename="fade_results.csv"'
        assert resp.headers["cache-control"] == "no-store"


class TestExportContent:
    async def test_one_row_per_scan_with_the_documented_columns(self, client: AsyncClient, app_db):
        await _import_cohort(app_db)
        await _phantom_scan(client)
        headers = await login_as(client, app_db, UserRole.RESEARCHER)

        rows = _parse(await client.get("/api/v1/export/results.csv", headers=headers))

        assert len(rows) == 6
        for column in (
            "external_subject_id",
            "source",
            "extraction_method",
            "hippocampal_volume",
            "hippocampal_volume_unit",
            "ventricle_brain_ratio",
            "cortical_thickness",
            "raw_icv_mm3",
            "raw_hippocampus_total_mm3",
            "raw_cortical_gm_volume_norm",
            "qc_flags",
            "determinate",
            "stage",
            "severity",
            "confidence",
            "uncertainty",
            "calibration_version",
            "engine_version",
            "reference_label",
            "n_ratings",
            "rating_stage_called",
            "rating_borderline",
            "rating_confidence",
            "rating_notes",
        ):
            assert column in rows[0], column

    async def test_real_rows_carry_provenance_values_and_the_reference_label(
        self, client: AsyncClient, app_db
    ):
        await _import_cohort(app_db)
        headers = await login_as(client, app_db, UserRole.RESEARCHER)
        rows = {
            r["external_subject_id"]: r
            for r in _parse(await client.get("/api/v1/export/results.csv", headers=headers))
        }

        row = rows["FAKE_0002_MR1"]
        assert row["source"] == "OASIS"
        assert row["extraction_method"] == "manual_biomarkers"
        assert row["status"] == "completed"
        assert (row["age"], row["sex"]) == ("81", "M")
        assert float(row["hippocampal_volume"]) == 4.0
        assert row["hippocampal_volume_unit"] == "‰ICV"
        assert float(row["ventricle_brain_ratio"]) == 0.041
        assert row["ventricle_brain_ratio_unit"] == "ratio"
        assert row["cortical_thickness"] == ""  # not measured: empty, not zero
        assert float(row["raw_icv_mm3"]) == 1600000.0
        assert float(row["raw_cortical_gm_volume_norm"]) == 270.1
        assert row["determinate"] == "false"
        assert row["stage"] == ""
        assert row["severity"] == ""
        assert float(row["uncertainty"]) == 100.0
        assert row["calibration_version"] == "phantom.v0"
        assert row["engine_version"]
        assert float(row["reference_label"]) == 0.5
        assert row["n_ratings"] == "0"
        assert row["rating_stage_called"] == ""

        assert rows["FAKE_0006_MR1"]["qc_flags"] == "icv_out_of_range"
        assert float(rows["FAKE_0001_MR1"]["reference_label"]) == 0.0
        # Unlabelled subjects were never imported, so they are not here as 0.
        assert "FAKE_0003_MR1" not in rows

    async def test_staged_result_is_exported(
        self, client: AsyncClient, app_db, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            get_settings(), "calibration_file", str(write_test_real_calibration(tmp_path))
        )
        scan = await _real_scan(client, subject="FAKE_9001_MR1")
        headers = await login_as(client, app_db, UserRole.RESEARCHER)
        (row,) = _parse(await client.get("/api/v1/export/results.csv", headers=headers))

        assert row["determinate"] == "true"
        assert row["stage"] == scan["fisResult"]["stage"]
        assert float(row["severity"]) == scan["fisResult"]["severity"]
        assert float(row["confidence"]) == scan["fisResult"]["confidence"]
        assert row["calibration_version"] == TEST_REAL_VERSION
        assert row["reference_label"] == ""  # none recorded: empty, not zero

    async def test_phantom_rows_are_labelled_as_phantom(self, client: AsyncClient, app_db):
        await _phantom_scan(client)
        headers = await login_as(client, app_db, UserRole.RESEARCHER)
        (row,) = _parse(await client.get("/api/v1/export/results.csv", headers=headers))
        assert row["source"] == "PHANTOM"
        assert row["extraction_method"] == "inhouse_phantom"
        assert row["hippocampal_volume_unit"] == "mL"

    async def test_source_filter(self, client: AsyncClient, app_db):
        await _import_cohort(app_db)
        await _phantom_scan(client)
        headers = await login_as(client, app_db, UserRole.RESEARCHER)

        oasis = _parse(
            await client.get(
                "/api/v1/export/results.csv", params={"source": "OASIS"}, headers=headers
            )
        )
        phantom = _parse(
            await client.get(
                "/api/v1/export/results.csv", params={"source": "PHANTOM"}, headers=headers
            )
        )
        assert (len(oasis), len(phantom)) == (5, 1)

    async def test_latest_rating_and_rating_count(self, client: AsyncClient, app_db):
        scan = await _real_scan(client, subject="FAKE_9001_MR1")
        url = f"/api/v1/study/cases/{scan['id']}/rating"
        first = await login_as(client, app_db, UserRole.CLINICIAN, "one@test.dev")
        second = await login_as(client, app_db, UserRole.CLINICIAN, "two@test.dev")
        researcher = await login_as(client, app_db, UserRole.RESEARCHER)

        async def exported() -> dict:
            (row,) = _parse(await client.get("/api/v1/export/results.csv", headers=researcher))
            return row

        await client.post(url, json=_rating("MCI", True, 2, "first read"), headers=first)
        await client.post(url, json=_rating("AD", False, 4), headers=second)
        row = await exported()
        assert row["n_ratings"] == "2"
        assert (row["rating_stage_called"], row["rating_confidence"]) == ("AD", "4")
        assert row["rating_borderline"] == "false"

        # The first rater revises their call: theirs is now the latest.
        await client.post(url, json=_rating("CN", True, 5, "second read"), headers=first)
        row = await exported()
        assert row["n_ratings"] == "2"
        assert (row["rating_stage_called"], row["rating_confidence"]) == ("CN", "5")
        assert row["rating_borderline"] == "true"
        assert row["rating_notes"] == "second read"
        assert row["rating_rater_id"]
        assert row["rating_at"]

    async def test_notes_cannot_inject_a_spreadsheet_formula(self, client: AsyncClient, app_db):
        scan = await _real_scan(client)
        await client.post(
            f"/api/v1/study/cases/{scan['id']}/rating",
            json=_rating(notes='=HYPERLINK("http://evil.example","click")'),
        )
        researcher = await login_as(client, app_db, UserRole.RESEARCHER)
        (row,) = _parse(await client.get("/api/v1/export/results.csv", headers=researcher))
        assert row["rating_notes"].startswith("'=")

    async def test_every_export_is_audited(self, client: AsyncClient, app_db):
        await _import_cohort(app_db)
        researcher = await login_as(client, app_db, UserRole.RESEARCHER)
        await client.get("/api/v1/export/results.csv", headers=researcher)
        await client.get(
            "/api/v1/export/results.csv", params={"source": "ADNI"}, headers=researcher
        )

        async with app_db() as session:
            entries = (
                (
                    await session.execute(
                        select(AuditLog)
                        .where(AuditLog.action == "export.results_csv")
                        .order_by(AuditLog.created_at)
                    )
                )
                .scalars()
                .all()
            )
        assert [e.detail for e in entries] == [
            {"rows": 5, "source": "all"},
            {"rows": 0, "source": "ADNI"},
        ]
        assert {e.actor_role for e in entries} == {"researcher"}

    async def test_a_refused_export_is_not_audited(self, client: AsyncClient, app_db):
        await client.get("/api/v1/export/results.csv")
        async with app_db() as session:
            entries = (await session.execute(select(AuditLog))).scalars().all()
        assert [e for e in entries if e.action == "export.results_csv"] == []


class TestExportHelpers:
    def test_flatten_raw_volumes(self):
        flat = flatten_raw_volumes(
            {
                "icv_mm3": 1.5e6,
                "icv_source": "eTIV",
                "thickness_mm": {"lh": {"entorhinal": 3.2}, "rh": {"entorhinal": 3.4}},
                "thickness_regions": ["entorhinal", "precuneus"],
                "third_ventricle_mm3": None,
            }
        )
        assert flat == {
            "raw_icv_mm3": 1.5e6,
            "raw_icv_source": "eTIV",
            "raw_thickness_mm.lh.entorhinal": 3.2,
            "raw_thickness_mm.rh.entorhinal": 3.4,
            "raw_thickness_regions": '["entorhinal", "precuneus"]',
            "raw_third_ventricle_mm3": None,
        }
        assert flatten_raw_volumes(None) == {}

    def test_csv_rendering_escapes_formulas_but_not_numbers(self):
        text = render_csv(
            ["a", "b", "c", "d", "e"],
            [{"a": "=1+1", "b": -3.5, "c": "@cmd", "d": None, "e": True}],
        )
        (row,) = list(csv.DictReader(io.StringIO(text)))
        assert row == {"a": "'=1+1", "b": "-3.5", "c": "'@cmd", "d": "", "e": "true"}
