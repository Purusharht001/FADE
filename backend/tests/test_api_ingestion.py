"""Phase 2c: the three ingestion endpoints for biomarkers measured outside
this service, all of which run through `run_pipeline_from_biomarkers`.
"""

import datetime
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.models.enums import DataSource, ExtractionMethod, ScanStatus, Sex
from app.repositories import patient_repo
from app.services.biomarker_derivation import derive_from_stats, derive_from_values
from app.services.calibration import DataDomain
from app.services.freesurfer_stats import parse_stats_dir
from app.services.pipeline import Provenance, run_pipeline_from_biomarkers
from tests.calibration_utils import TEST_REAL_VERSION, write_test_real_calibration

FIXTURES = Path(__file__).parent / "fixtures"
FREESURFER = FIXTURES / "freesurfer"

RAW_VOLUMES = {
    "icvMm3": 1_500_000.0,
    "brainSegVolMm3": 1_100_000.0,
    "leftHippocampusMm3": 3900.0,
    "rightHippocampusMm3": 4100.0,
    "leftLateralVentricleMm3": 12000.0,
    "rightLateralVentricleMm3": 11000.0,
    "leftInfLatVentMm3": 400.0,
    "rightInfLatVentMm3": 600.0,
}


def _stats_files(*names: str) -> dict:
    field_for = {"aseg.stats": "aseg", "lh.aparc.stats": "lh_aparc", "rh.aparc.stats": "rh_aparc"}
    return {
        field_for[name]: (name, (FREESURFER / name).read_bytes(), "text/plain") for name in names
    }


ALL_STATS = ("aseg.stats", "lh.aparc.stats", "rh.aparc.stats")


async def _patient(client: AsyncClient, source: str = "OASIS") -> dict:
    resp = await client.post("/api/v1/patients", json={"age": 72, "sex": "F", "source": source})
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture
def real_calibration(tmp_path, monkeypatch) -> str:
    """Activates the test-only real calibration (see calibration_utils)."""
    monkeypatch.setattr(
        get_settings(), "calibration_file", str(write_test_real_calibration(tmp_path))
    )
    return TEST_REAL_VERSION


def _by_key(scan: dict) -> dict:
    return {b["key"]: b for b in scan["biomarkers"]}


class TestRunPipelineFromBiomarkers:
    def test_real_data_is_indeterminate_under_the_phantom_calibration(self):
        derived = derive_from_stats(parse_stats_dir(FREESURFER))
        result = run_pipeline_from_biomarkers(
            derived, None, Provenance(ExtractionMethod.FREESURFER_STATS, DataDomain.REAL)
        )
        assert result.fis_output.determinate is False
        assert result.fis_output.stage is None
        assert len(result.biomarkers) == 3
        assert result.raw_volumes == derived.raw_volumes
        assert result.qc_flags == []

    def test_explicit_raw_overrides_the_derived_raw_volumes(self):
        derived = derive_from_values(
            hippocampal_volume_norm=5.0, ventricle_brain_ratio=0.03, cortical_thickness_mm=2.8
        )
        result = run_pipeline_from_biomarkers(
            derived,
            {"source": "cohort_csv"},
            Provenance(ExtractionMethod.MANUAL_BIOMARKERS, DataDomain.REAL, "FAKE_0001_MR1"),
        )
        assert result.raw_volumes == {"source": "cohort_csv"}

    def test_qc_flags_travel_with_the_result(self):
        derived = derive_from_values(
            hippocampal_volume_norm=5.0, ventricle_brain_ratio=0.03, cortical_thickness_mm=9.0
        )
        result = run_pipeline_from_biomarkers(
            derived, None, Provenance(ExtractionMethod.MANUAL_BIOMARKERS, DataDomain.REAL)
        )
        assert [f["code"] for f in result.qc_flags] == ["thickness_out_of_range"]
        assert "qc_flag:thickness_out_of_range" in result.fis_output.indeterminate_reason


class TestFreeSurferEndpoint:
    async def test_full_stats_set_creates_a_scored_scan(self, client: AsyncClient):
        patient = await _patient(client)
        resp = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/freesurfer",
            files=_stats_files(*ALL_STATS),
            data={"external_subject_id": "FAKE_0001_MR1"},
        )
        assert resp.status_code == 201
        scan = resp.json()

        assert scan["status"] == "completed"
        assert scan["extractionMethod"] == "freesurfer_stats"
        assert scan["externalSubjectId"] == "FAKE_0001_MR1"
        assert scan["qcFlags"] == []

        readings = _by_key(scan)
        assert readings["hippocampal_volume"]["value"] == pytest.approx(5.333333, abs=1e-6)
        assert readings["hippocampal_volume"]["unit"] == "‰ICV"
        assert readings["ventricle_brain_ratio"]["value"] == pytest.approx(0.0218182, abs=1e-7)
        assert readings["ventricle_brain_ratio"]["unit"] == "ratio"
        assert readings["cortical_thickness"]["value"] == pytest.approx(2.86)
        assert readings["cortical_thickness"]["unit"] == "mm"

        assert scan["rawVolumes"]["icv_mm3"] == 1500000.0
        assert scan["rawVolumes"]["cortical_gm_volume_norm"] == pytest.approx(296.6667, abs=1e-4)

    async def test_real_data_gets_no_stage_while_only_the_phantom_calibration_exists(
        self, client: AsyncClient
    ):
        patient = await _patient(client)
        scan = (
            await client.post(
                f"/api/v1/patients/{patient['id']}/scans/freesurfer",
                files=_stats_files(*ALL_STATS),
            )
        ).json()

        fis = scan["fisResult"]
        assert fis["stage"] is None
        assert fis["determinate"] is False
        assert fis["needsReview"] is True
        assert fis["severity"] is None
        assert fis["calibrationVersion"] == "phantom.v0"
        assert fis["indeterminateReason"].startswith("calibration_not_validated_for_real_data")
        # No severity bar drawn from phantom breakpoints.
        assert [b["abnormality"] for b in scan["biomarkers"]] == [None, None, None]
        assert scan["calibrationVersion"] == "phantom.v0"

    async def test_real_calibration_stages_the_scan(self, client: AsyncClient, real_calibration):
        patient = await _patient(client)
        scan = (
            await client.post(
                f"/api/v1/patients/{patient['id']}/scans/freesurfer",
                files=_stats_files(*ALL_STATS),
            )
        ).json()

        fis = scan["fisResult"]
        assert fis["determinate"] is True
        assert fis["stage"] in ("CN", "MCI", "AD")
        assert fis["calibrationVersion"] == real_calibration
        assert all(b["abnormality"] is not None for b in scan["biomarkers"])

    async def test_without_aparc_files_the_result_is_indeterminate(
        self, client: AsyncClient, real_calibration
    ):
        """Even with a usable calibration, a missing biomarker is not staged."""
        patient = await _patient(client)
        resp = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/freesurfer",
            files=_stats_files("aseg.stats"),
        )
        assert resp.status_code == 201
        scan = resp.json()

        assert set(_by_key(scan)) == {"hippocampal_volume", "ventricle_brain_ratio"}
        assert scan["fisResult"]["stage"] is None
        assert scan["fisResult"]["determinate"] is False
        assert scan["fisResult"]["indeterminateReason"] == "missing_biomarker:cortical_thickness"
        assert [f["code"] for f in scan["qcFlags"]] == ["cortical_thickness_unavailable"]

    async def test_implausible_value_is_flagged_and_not_staged(
        self, client: AsyncClient, real_calibration
    ):
        aseg = (FREESURFER / "aseg.stats").read_text(encoding="utf-8")
        aseg = aseg.replace(
            "Estimated Total Intracranial Volume, 1500000.000000",
            "Estimated Total Intracranial Volume, 2600000.000000",
        )
        files = _stats_files(*ALL_STATS)
        files["aseg"] = ("aseg.stats", aseg.encode(), "text/plain")
        patient = await _patient(client)

        resp = await client.post(f"/api/v1/patients/{patient['id']}/scans/freesurfer", files=files)
        assert resp.status_code == 201
        scan = resp.json()
        assert [f["code"] for f in scan["qcFlags"]] == ["icv_out_of_range"]
        assert scan["qcFlags"][0]["kind"] == "plausibility"
        assert scan["fisResult"]["stage"] is None
        assert scan["fisResult"]["indeterminateReason"] == "qc_flag:icv_out_of_range"

    async def test_unparseable_stats_are_a_422_and_leave_no_scan(self, client: AsyncClient):
        patient = await _patient(client)
        resp = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/freesurfer",
            files={"aseg": ("aseg.stats", b"this is not a stats file\n", "text/plain")},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "stats_parse_error"

        fetched = (await client.get(f"/api/v1/patients/{patient['id']}")).json()
        assert fetched["scans"] == []

    async def test_error_does_not_echo_file_contents(self, client: AsyncClient):
        patient = await _patient(client)
        aseg = (FREESURFER / "aseg.stats").read_text(encoding="utf-8")
        aseg = aseg.replace("3900     3900.0  Left-Hippocampus", "3900  PRIVATE  Left-Hippocampus")
        resp = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/freesurfer",
            files={"aseg": ("aseg.stats", aseg.encode(), "text/plain")},
        )
        assert resp.status_code == 422
        for private in ("PRIVATE", "fixture-user", "fixture-host", "FIXTURE_0001"):
            assert private not in resp.text

    async def test_one_hemisphere_is_rejected(self, client: AsyncClient):
        patient = await _patient(client)
        resp = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/freesurfer",
            files=_stats_files("aseg.stats", "lh.aparc.stats"),
        )
        assert resp.status_code == 422
        assert "both lh_aparc and rh_aparc" in resp.json()["error"]["message"]

    async def test_empty_aseg_upload_is_rejected(self, client: AsyncClient):
        patient = await _patient(client)
        resp = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/freesurfer",
            files={"aseg": ("aseg+DKT.stats", b"", "text/plain")},
        )
        assert resp.status_code == 422
        assert "VINN" in resp.json()["error"]["message"]

    async def test_oversized_stats_file_is_rejected(self, client: AsyncClient, monkeypatch):
        monkeypatch.setattr(get_settings(), "max_stats_upload_kb", 1)
        patient = await _patient(client)
        resp = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/freesurfer",
            files=_stats_files("aseg.stats"),
        )
        assert resp.status_code == 422
        assert "exceeds the 1 KB limit" in resp.json()["error"]["message"]

    async def test_phantom_patient_is_refused(self, client: AsyncClient):
        patient = await _patient(client, source="PHANTOM")
        resp = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/freesurfer",
            files=_stats_files(*ALL_STATS),
        )
        assert resp.status_code == 422
        assert "PHANTOM" in resp.json()["error"]["message"]

    @pytest.mark.parametrize("subject_id", ["John Smith", "Smith, John", "MRN#12345", "x" * 65])
    async def test_subject_id_that_looks_like_phi_is_refused(self, client: AsyncClient, subject_id):
        patient = await _patient(client)
        resp = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/freesurfer",
            files=_stats_files(*ALL_STATS),
            data={"external_subject_id": subject_id},
        )
        assert resp.status_code == 422
        # The rejected value itself is not repeated back.
        assert subject_id not in resp.text

    async def test_unknown_patient(self, client: AsyncClient):
        resp = await client.post(
            f"/api/v1/patients/{uuid.uuid4()}/scans/freesurfer",
            files=_stats_files(*ALL_STATS),
        )
        assert resp.status_code == 404

    async def test_scan_date_is_recorded(self, client: AsyncClient):
        patient = await _patient(client)
        scan = (
            await client.post(
                f"/api/v1/patients/{patient['id']}/scans/freesurfer",
                params={"scanDate": "2026-03-04"},
                files=_stats_files(*ALL_STATS),
            )
        ).json()
        assert scan["scanDate"] == "2026-03-04"


class TestManualBiomarkersEndpoint:
    async def test_raw_volumes_are_derived_server_side(self, client: AsyncClient):
        patient = await _patient(client, source="Clinic")
        resp = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/biomarkers",
            json={
                "rawVolumes": {**RAW_VOLUMES, "corticalThicknessMm": 2.86},
                "externalSubjectId": "CLINIC-0007",
            },
        )
        assert resp.status_code == 201
        scan = resp.json()

        assert scan["extractionMethod"] == "manual_biomarkers"
        assert scan["externalSubjectId"] == "CLINIC-0007"
        readings = _by_key(scan)
        assert readings["hippocampal_volume"]["value"] == pytest.approx(5.333333, abs=1e-6)
        assert readings["hippocampal_volume"]["unit"] == "‰ICV"
        assert readings["ventricle_brain_ratio"]["value"] == pytest.approx(0.0218182, abs=1e-7)
        assert readings["cortical_thickness"]["value"] == 2.86
        assert scan["rawVolumes"]["supplied_as"] == "raw_volumes"
        assert scan["rawVolumes"]["hippocampus_total_mm3"] == 8000.0
        assert scan["fisResult"]["determinate"] is False

    async def test_raw_volumes_without_thickness_are_indeterminate(
        self, client: AsyncClient, real_calibration
    ):
        patient = await _patient(client)
        scan = (
            await client.post(
                f"/api/v1/patients/{patient['id']}/scans/biomarkers",
                json={"rawVolumes": RAW_VOLUMES},
            )
        ).json()
        assert set(_by_key(scan)) == {"hippocampal_volume", "ventricle_brain_ratio"}
        assert scan["fisResult"]["indeterminateReason"] == "missing_biomarker:cortical_thickness"

    async def test_derived_values_with_units(self, client: AsyncClient, real_calibration):
        patient = await _patient(client)
        resp = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/biomarkers",
            json={
                "derived": {
                    "hippocampalVolume": {"value": 5.0, "unit": "‰ICV"},
                    "ventricleBrainRatio": {"value": 0.05, "unit": "ratio"},
                    "corticalThickness": {"value": 2.8, "unit": "mm"},
                },
                "scanDate": "2026-02-01",
            },
        )
        assert resp.status_code == 201
        scan = resp.json()
        assert scan["scanDate"] == "2026-02-01"
        assert scan["extractionMethod"] == "manual_biomarkers"
        assert scan["rawVolumes"] == {"supplied_as": "derived_values"}
        assert scan["qcFlags"] == []
        assert scan["fisResult"]["determinate"] is True
        assert scan["fisResult"]["stage"] is not None
        assert scan["fisResult"]["calibrationVersion"] == real_calibration

    async def test_derived_value_outside_plausibility_is_not_staged(
        self, client: AsyncClient, real_calibration
    ):
        patient = await _patient(client)
        scan = (
            await client.post(
                f"/api/v1/patients/{patient['id']}/scans/biomarkers",
                json={
                    "derived": {
                        "hippocampalVolume": {"value": 5.0, "unit": "‰ICV"},
                        "ventricleBrainRatio": {"value": 0.05, "unit": "ratio"},
                        "corticalThickness": {"value": 6.0, "unit": "mm"},
                    }
                },
            )
        ).json()
        assert scan["fisResult"]["determinate"] is False
        assert scan["fisResult"]["indeterminateReason"] == "qc_flag:thickness_out_of_range"

    async def test_ascii_alias_for_the_per_mille_unit(self, client: AsyncClient):
        patient = await _patient(client)
        resp = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/biomarkers",
            json={"derived": {"hippocampalVolume": {"value": 5.0, "unit": "permille_ICV"}}},
        )
        assert resp.status_code == 201
        assert _by_key(resp.json())["hippocampal_volume"]["unit"] == "‰ICV"

    @pytest.mark.parametrize(
        ("field", "unit"),
        [
            ("hippocampalVolume", "mL"),
            ("hippocampalVolume", "mm3"),
            ("ventricleBrainRatio", "%"),
            ("corticalThickness", "cm"),
        ],
    )
    async def test_wrong_unit_is_rejected_not_converted(self, client: AsyncClient, field, unit):
        patient = await _patient(client)
        resp = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/biomarkers",
            json={"derived": {field: {"value": 3.0, "unit": unit}}},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_unit"
        fetched = (await client.get(f"/api/v1/patients/{patient['id']}")).json()
        assert fetched["scans"] == []

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {
                "rawVolumes": RAW_VOLUMES,
                "derived": {"corticalThickness": {"value": 2.8, "unit": "mm"}},
            },
            {"derived": {}},
            {"derived": {"corticalThickness": {"value": -1.0, "unit": "mm"}}},
            {"derived": {"corticalThickness": {"value": 2.8}}},
            {"rawVolumes": {**RAW_VOLUMES, "icvMm3": 0}},
            {"rawVolumes": {**RAW_VOLUMES, "leftHippocampusMm3": -5}},
            {"rawVolumes": {k: v for k, v in RAW_VOLUMES.items() if k != "brainSegVolMm3"}},
            {"rawVolumes": RAW_VOLUMES, "externalSubjectId": "John Smith"},
        ],
    )
    async def test_malformed_bodies_are_rejected(self, client: AsyncClient, body):
        patient = await _patient(client)
        resp = await client.post(f"/api/v1/patients/{patient['id']}/scans/biomarkers", json=body)
        assert resp.status_code == 422

    async def test_phantom_patient_is_refused(self, client: AsyncClient):
        patient = await _patient(client, source="PHANTOM")
        resp = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/biomarkers",
            json={"rawVolumes": RAW_VOLUMES},
        )
        assert resp.status_code == 422

    async def test_a_patient_with_real_data_cannot_take_a_synthetic_scan(self, client: AsyncClient):
        patient = await _patient(client, source="Clinic")
        await client.post(
            f"/api/v1/patients/{patient['id']}/scans/biomarkers", json={"rawVolumes": RAW_VOLUMES}
        )
        resp = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/synthetic", json={"severity": 0.5, "seed": 1}
        )
        assert resp.status_code == 422
        fetched = (await client.get(f"/api/v1/patients/{patient['id']}")).json()
        assert fetched["source"] == "Clinic"
        assert len(fetched["scans"]) == 1


class TestAttachStats:
    async def _waiting_scan(self, app_db, *, source=DataSource.CLINIC, **scan_fields) -> tuple:
        async with app_db() as session:
            patient = await patient_repo.create(session, age=70, sex=Sex.M, source=source)
            scan = await patient_repo.create_scan(
                session, patient_id=patient.id, scan_date=datetime.date.today(), file_path=None
            )
            scan.status = scan_fields.get("status", ScanStatus.AWAITING_VOLUMETRY)
            scan.extraction_method = scan_fields.get("extraction_method")
            scan.failure_reason = scan_fields.get("failure_reason")
            await session.commit()
            return str(patient.id), str(scan.id)

    async def test_attaching_stats_completes_a_waiting_scan(self, client: AsyncClient, app_db):
        patient_id, scan_id = await self._waiting_scan(app_db)

        before = (await client.get(f"/api/v1/patients/{patient_id}/scans/{scan_id}")).json()
        assert before["status"] == "awaiting_volumetry"
        assert before["fisResult"] is None

        resp = await client.post(
            f"/api/v1/patients/{patient_id}/scans/{scan_id}/stats",
            files=_stats_files(*ALL_STATS),
            data={"external_subject_id": "CLINIC-0001"},
        )
        assert resp.status_code == 200
        scan = resp.json()
        assert scan["id"] == scan_id
        assert scan["status"] == "completed"
        assert scan["extractionMethod"] == "freesurfer_stats"
        assert scan["externalSubjectId"] == "CLINIC-0001"
        assert len(scan["biomarkers"]) == 3
        assert scan["fisResult"]["determinate"] is False

    async def test_stats_from_fastsurfer_can_be_labelled_as_such(self, client: AsyncClient, app_db):
        patient_id, scan_id = await self._waiting_scan(app_db)
        resp = await client.post(
            f"/api/v1/patients/{patient_id}/scans/{scan_id}/stats",
            files=_stats_files(*ALL_STATS),
            data={"extraction_method": "fastsurfer"},
        )
        assert resp.status_code == 200
        assert resp.json()["extractionMethod"] == "fastsurfer"

    async def test_other_extraction_methods_cannot_be_claimed(self, client: AsyncClient, app_db):
        patient_id, scan_id = await self._waiting_scan(app_db)
        resp = await client.post(
            f"/api/v1/patients/{patient_id}/scans/{scan_id}/stats",
            files=_stats_files(*ALL_STATS),
            data={"extraction_method": "inhouse_phantom"},
        )
        assert resp.status_code == 422

    async def test_bad_stats_leave_the_scan_waiting(self, client: AsyncClient, app_db):
        patient_id, scan_id = await self._waiting_scan(app_db)
        resp = await client.post(
            f"/api/v1/patients/{patient_id}/scans/{scan_id}/stats",
            files={"aseg": ("aseg.stats", b"garbage\n", "text/plain")},
        )
        assert resp.status_code == 422

        after = (await client.get(f"/api/v1/patients/{patient_id}/scans/{scan_id}")).json()
        assert after["status"] == "awaiting_volumetry"
        assert after["fisResult"] is None

    async def test_a_completed_scan_cannot_take_stats(self, client: AsyncClient, app_db):
        patient_id, scan_id = await self._waiting_scan(app_db)
        url = f"/api/v1/patients/{patient_id}/scans/{scan_id}/stats"
        first = await client.post(url, files=_stats_files(*ALL_STATS))
        assert first.status_code == 200

        second = await client.post(url, files=_stats_files(*ALL_STATS))
        assert second.status_code == 409
        assert "awaiting volumetry" in second.json()["error"]["message"]

    async def test_a_failed_segmentation_can_be_recovered_with_stats(
        self, client: AsyncClient, app_db
    ):
        patient_id, scan_id = await self._waiting_scan(
            app_db,
            status=ScanStatus.FAILED,
            extraction_method=ExtractionMethod.FASTSURFER,
            failure_reason="FastSurfer timed out.",
        )
        resp = await client.post(
            f"/api/v1/patients/{patient_id}/scans/{scan_id}/stats",
            files=_stats_files(*ALL_STATS),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"
        assert resp.json()["failureReason"] is None

    async def test_a_failed_phantom_scan_cannot_take_stats(self, client: AsyncClient, app_db):
        patient_id, scan_id = await self._waiting_scan(
            app_db, status=ScanStatus.FAILED, extraction_method=ExtractionMethod.INHOUSE_PHANTOM
        )
        resp = await client.post(
            f"/api/v1/patients/{patient_id}/scans/{scan_id}/stats",
            files=_stats_files(*ALL_STATS),
        )
        assert resp.status_code == 409

    async def test_scan_of_another_patient_is_not_found(self, client: AsyncClient, app_db):
        _patient_id, scan_id = await self._waiting_scan(app_db)
        other = await _patient(client)
        resp = await client.post(
            f"/api/v1/patients/{other['id']}/scans/{scan_id}/stats",
            files=_stats_files(*ALL_STATS),
        )
        assert resp.status_code == 404
