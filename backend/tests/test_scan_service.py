"""Tests for app/services/scan_service.py — the seam between the pure
preprocessing/volumetry/FIS pipeline and persistence. The API integration
tests (test_api_patients.py) only exercise this indirectly through HTTP;
these tests drive it directly against a real (in-memory) database so
persistence details — status transitions, retry/replace semantics, failure
bookkeeping — are actually checked rather than assumed.
"""

import datetime

import nibabel as nib
import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import UnprocessableScanError
from app.models.enums import BiomarkerKey, DataSource, ScanStatus, Sex
from app.repositories import patient_repo
from app.services import scan_service
from app.services.biomarkers import BIOMARKER_DEFS
from app.services.synthetic_mri import generate_synthetic_scan, save_nifti

# Upload-dir isolation (so seeded synthetic-scan tests don't write into the
# real data/uploads/) is handled session-wide by conftest.py's
# _isolate_upload_dir fixture.


async def _make_scan(db: AsyncSession, *, file_path: str | None = None):
    patient = await patient_repo.create(db, age=70, sex=Sex.F, source=DataSource.CLINIC)
    scan = await patient_repo.create_scan(
        db, patient_id=patient.id, scan_date=datetime.date.today(), file_path=file_path
    )
    await db.flush()
    return patient, scan


class TestProcessSyntheticScan:
    async def test_successful_run_persists_biomarkers_and_fis_result(self, db_session):
        _patient, scan = await _make_scan(db_session)

        result = await scan_service.process_synthetic_scan(db_session, scan, 0.9, seed=42)

        assert result.status == ScanStatus.COMPLETED
        assert result.failure_reason is None
        assert len(result.biomarkers) == 3
        assert result.fis_result is not None
        assert result.fis_result.stage in ("CN", "MCI", "AD")
        assert 0 <= result.fis_result.confidence <= 100

    async def test_biomarker_units_match_definitions(self, db_session):
        _patient, scan = await _make_scan(db_session)
        result = await scan_service.process_synthetic_scan(db_session, scan, 0.5, seed=1)

        for reading in result.biomarkers:
            assert reading.unit == BIOMARKER_DEFS[BiomarkerKey(reading.key)].unit

    async def test_seeded_run_saves_a_real_nifti_file(self, db_session):
        _patient, scan = await _make_scan(db_session)
        result = await scan_service.process_synthetic_scan(db_session, scan, 0.3, seed=7)

        assert result.file_path is not None
        img = nib.load(result.file_path)
        assert img.shape == (64, 64, 64)

    async def test_pipeline_failure_marks_scan_failed_and_reraises(self, db_session, monkeypatch):
        _patient, scan = await _make_scan(db_session)

        def _boom(*args, **kwargs):
            raise UnprocessableScanError("synthetic failure for testing")

        monkeypatch.setattr(scan_service, "run_pipeline_synthetic", _boom)

        with pytest.raises(UnprocessableScanError, match="synthetic failure for testing"):
            await scan_service.process_synthetic_scan(db_session, scan, 0.5, seed=1)

        assert scan.status == ScanStatus.FAILED
        assert scan.failure_reason == "synthetic failure for testing"
        # A failed run must not leave a half-written FIS result behind.
        assert scan.fis_result is None

    async def test_rerunning_a_scan_replaces_rather_than_duplicates_results(self, db_session):
        """A clinician re-running a scan (e.g. after a pipeline update)
        should end up with exactly one current result, not an
        accumulating history of stale biomarker rows — traceability means
        the record reflects what's true *now*, not every attempt ever made.
        """
        _patient, scan = await _make_scan(db_session)

        first = await scan_service.process_synthetic_scan(db_session, scan, 0.1, seed=1)
        first_fis_id = first.fis_result.id
        assert len(first.biomarkers) == 3

        second = await scan_service.process_synthetic_scan(db_session, scan, 0.9, seed=2)
        assert len(second.biomarkers) == 3
        assert second.fis_result.id != first_fis_id  # old result was replaced, not kept alongside
        assert second.status == ScanStatus.COMPLETED


class TestProcessUploadedScan:
    async def test_successful_run_on_a_real_file(self, db_session, tmp_path):
        intensity, _labels, _truth = generate_synthetic_scan(0.6, seed=3)
        path = tmp_path / "scan.nii.gz"
        save_nifti(intensity, path)

        _patient, scan = await _make_scan(db_session, file_path=str(path))
        result = await scan_service.process_uploaded_scan(db_session, scan, str(path))

        assert result.status == ScanStatus.COMPLETED
        assert len(result.biomarkers) == 3
        assert result.fis_result is not None

    async def test_missing_file_marks_scan_failed(self, db_session, tmp_path):
        missing_path = str(tmp_path / "does-not-exist.nii.gz")
        _patient, scan = await _make_scan(db_session, file_path=missing_path)

        with pytest.raises(UnprocessableScanError, match="not found"):
            await scan_service.process_uploaded_scan(db_session, scan, missing_path)

        assert scan.status == ScanStatus.FAILED
        assert "not found" in scan.failure_reason

    async def test_corrupted_file_marks_scan_failed(self, db_session, tmp_path):
        corrupt_path = tmp_path / "corrupt.nii.gz"
        corrupt_path.write_bytes(b"garbage, not a real nifti file" * 5)

        _patient, scan = await _make_scan(db_session, file_path=str(corrupt_path))

        with pytest.raises(UnprocessableScanError):
            await scan_service.process_uploaded_scan(db_session, scan, str(corrupt_path))

        assert scan.status == ScanStatus.FAILED
        assert scan.failure_reason is not None

    async def test_intermediate_status_is_preprocessing_before_completion(
        self, db_session, monkeypatch, tmp_path
    ):
        """Verifies the status is actually set to PREPROCESSING (and
        flushed) before the pipeline runs, not just jumped straight to
        COMPLETED/FAILED — this is what lets a concurrent read see
        "in progress" rather than a stale PENDING while a slow scan runs.
        """
        intensity, _labels, _truth = generate_synthetic_scan(0.5, seed=9)
        path = tmp_path / "scan.nii.gz"
        save_nifti(intensity, path)
        _patient, scan = await _make_scan(db_session, file_path=str(path))

        seen_status = []
        original = scan_service.run_pipeline_from_file

        def _spy(*args, **kwargs):
            seen_status.append(scan.status)
            return original(*args, **kwargs)

        monkeypatch.setattr(scan_service, "run_pipeline_from_file", _spy)
        await scan_service.process_uploaded_scan(db_session, scan, str(path))

        assert seen_status == [ScanStatus.PREPROCESSING]


class TestAbnormalContrastInputs:
    """Volumes with pathological intensity distributions — the kind of
    thing a real bad acquisition (clipped dynamic range, coil dropout,
    motion artifact wipeout) could produce — should fail the scan cleanly
    through the same service layer, not just at the raw preprocessing
    function level.
    """

    async def test_uniform_intensity_volume_fails_cleanly(self, db_session, tmp_path):
        flat = np.full((64, 64, 64), 0.5, dtype=np.float32)
        path = tmp_path / "flat.nii.gz"
        save_nifti(flat, path)

        _patient, scan = await _make_scan(db_session, file_path=str(path))

        with pytest.raises(UnprocessableScanError):
            await scan_service.process_uploaded_scan(db_session, scan, str(path))

        assert scan.status == ScanStatus.FAILED

    async def test_extreme_noise_does_not_crash_the_service(self, db_session, tmp_path):
        """Pure white noise won't have a coherent brain to segment, but the
        service layer must still resolve to a clean FAILED status — not an
        unhandled exception bubbling out as a 500.
        """
        rng = np.random.default_rng(0)
        noise = rng.uniform(0, 1, size=(64, 64, 64)).astype(np.float32)
        path = tmp_path / "noise.nii.gz"
        save_nifti(noise, path)

        _patient, scan = await _make_scan(db_session, file_path=str(path))

        with pytest.raises(UnprocessableScanError):
            await scan_service.process_uploaded_scan(db_session, scan, str(path))
        assert scan.status == ScanStatus.FAILED
