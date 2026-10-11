"""Phase 1: provenance. Phantom data is labelled as phantom (C1), every
scan records how its biomarkers were measured, and the research-only
reference label never reaches a triage response.
"""

import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.exceptions import UnprocessableScanError
from app.db import seed as seed_module
from app.models.enums import DataSource, ExtractionMethod, ScanStatus, Sex
from app.models.patient import Patient
from app.models.scan import Scan
from app.repositories import patient_repo
from app.services import scan_service


class TestEnums:
    def test_phantom_is_a_data_source(self):
        assert DataSource.PHANTOM.value == "PHANTOM"

    def test_new_scan_statuses_exist(self):
        assert ScanStatus.AWAITING_VOLUMETRY.value == "awaiting_volumetry"
        assert ScanStatus.SEGMENTING.value == "segmenting"

    def test_extraction_methods(self):
        assert {m.value for m in ExtractionMethod} == {
            "inhouse_phantom",
            "freesurfer_stats",
            "fastsurfer",
            "manual_biomarkers",
        }

    def test_enum_names_fit_their_columns(self):
        """Enum columns store the member NAME in a fixed-width VARCHAR."""
        assert max(len(m.name) for m in DataSource) <= 10
        assert max(len(m.name) for m in ScanStatus) <= 30
        assert max(len(m.name) for m in ExtractionMethod) <= 30


class TestSeedLabelsPhantom:
    async def test_every_seeded_patient_is_phantom(self, db_session, monkeypatch):
        monkeypatch.setattr(seed_module, "COHORT_SIZE", 3)
        await seed_module.seed(db_session)

        patients = (await db_session.execute(select(Patient))).scalars().all()
        assert len(patients) == 3
        assert {p.source for p in patients} == {DataSource.PHANTOM}

        scans = (await db_session.execute(select(Scan))).scalars().all()
        assert {s.extraction_method for s in scans} == {ExtractionMethod.INHOUSE_PHANTOM}


class TestSyntheticScanProvenance:
    async def test_synthetic_scan_relabels_its_patient_as_phantom(self, client: AsyncClient):
        patient = (await client.post("/api/v1/patients", json={"age": 70, "sex": "F"})).json()
        assert patient["source"] == "Clinic"

        scan = (
            await client.post(
                f"/api/v1/patients/{patient['id']}/scans/synthetic",
                json={"severity": 0.4, "seed": 2},
            )
        ).json()
        assert scan["extractionMethod"] == "inhouse_phantom"
        assert scan["calibrationVersion"] == "phantom.v0"
        assert scan["engineVersion"]
        assert scan["qcFlags"] == []

        fetched = (await client.get(f"/api/v1/patients/{patient['id']}")).json()
        assert fetched["source"] == "PHANTOM"

        listed = (await client.get("/api/v1/patients")).json()
        assert [p["source"] for p in listed] == ["PHANTOM"]
        assert listed[0]["latestScan"]["extractionMethod"] == "inhouse_phantom"

    async def test_patient_can_be_created_as_phantom(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/patients", json={"age": 70, "sex": "F", "source": "PHANTOM"}
        )
        assert resp.status_code == 201
        assert resp.json()["source"] == "PHANTOM"

    async def test_synthetic_scan_is_refused_on_a_patient_with_real_data(self, db_session):
        patient = await patient_repo.create(db_session, age=70, sex=Sex.F, source=DataSource.OASIS)
        await patient_repo.create_scan(
            db_session,
            patient_id=patient.id,
            scan_date=datetime.date.today(),
            file_path=None,
            extraction_method=ExtractionMethod.FREESURFER_STATS,
            external_subject_id="OAS1_0001_MR1",
        )
        phantom_scan = await patient_repo.create_scan(
            db_session, patient_id=patient.id, scan_date=datetime.date.today(), file_path=None
        )

        with pytest.raises(UnprocessableScanError, match="real scan data"):
            await scan_service.process_synthetic_scan(db_session, phantom_scan, 0.5, seed=1)

        assert patient.source == DataSource.OASIS
        assert phantom_scan.fis_result is None

    async def test_a_stored_file_awaiting_volumetry_counts_as_real_data(self, db_session):
        patient = await patient_repo.create(db_session, age=70, sex=Sex.F, source=DataSource.CLINIC)
        waiting = await patient_repo.create_scan(
            db_session, patient_id=patient.id, scan_date=datetime.date.today(), file_path=None
        )
        waiting.status = ScanStatus.AWAITING_VOLUMETRY
        await db_session.flush()

        assert await patient_repo.count_non_phantom_scans(db_session, patient.id) == 1
        assert await patient_repo.count_phantom_scans(db_session, patient.id) == 0


class TestReferenceLabelIsResearchOnly:
    async def test_reference_label_is_absent_from_triage_responses(self, client: AsyncClient):
        """The ground-truth CDR is stored on the scan but must not appear
        in any default endpoint, under either naming convention.
        """
        patient = (await client.post("/api/v1/patients", json={"age": 70, "sex": "F"})).json()
        scan = (
            await client.post(
                f"/api/v1/patients/{patient['id']}/scans/synthetic",
                json={"severity": 0.4, "seed": 2},
            )
        ).json()

        responses = [
            await client.get("/api/v1/patients"),
            await client.get(f"/api/v1/patients/{patient['id']}"),
            await client.get(f"/api/v1/patients/{patient['id']}/scans/{scan['id']}"),
            await client.get("/api/v1/cohort/stats"),
        ]
        for resp in responses:
            assert resp.status_code == 200
            assert "referenceLabel" not in resp.text
            assert "reference_label" not in resp.text

    async def test_reference_label_round_trips_in_the_database(self, db_session):
        patient = await patient_repo.create(db_session, age=70, sex=Sex.F, source=DataSource.OASIS)
        scan = await patient_repo.create_scan(
            db_session,
            patient_id=patient.id,
            scan_date=datetime.date.today(),
            file_path=None,
            reference_label=0.5,
        )
        await db_session.commit()
        reloaded = await patient_repo.get_scan(db_session, scan.id)
        assert reloaded is not None
        assert reloaded.reference_label == 0.5
