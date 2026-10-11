"""Phase 3: the scan-file upload endpoints over HTTP, for each volumetry
provider, including what is and is not left on disk afterwards.
"""

import gzip
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.services import dicom_ingest
from app.services import volumetry_providers as vp
from app.services.synthetic_mri import generate_synthetic_scan, save_nifti
from tests.dicom_utils import IDENTIFYING_VALUES, SHAPE, make_series, zip_files, zip_series

FIXTURES = Path(__file__).parent / "fixtures"
FREESURFER = FIXTURES / "freesurfer"
FASTSURFER_STATS = FIXTURES / "fastsurfer" / "stats"

INHOUSE_MESSAGE = (
    "in-house volumetry is validated only on the synthetic phantom; "
    "use external or fastsurfer_docker"
)


@pytest.fixture
def upload_dir(tmp_path, monkeypatch) -> Path:
    """A private upload directory per test, so the files a request leaves
    behind (or does not) can be listed exactly.
    """
    path = tmp_path / "uploads"
    path.mkdir()
    monkeypatch.setattr(get_settings(), "upload_dir", str(path))
    return path


@pytest.fixture
def provider(monkeypatch):
    def _set(name: str) -> None:
        monkeypatch.setattr(get_settings(), "volumetry_provider", name)

    return _set


def _files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def _nifti_bytes(tmp_path: Path, data: np.ndarray, *, name="scan.nii.gz", **header) -> bytes:
    img = nib.Nifti1Image(data, np.eye(4))
    for field, value in header.items():
        img.header[field] = value
    path = tmp_path / name
    nib.save(img, str(path))
    return path.read_bytes()


def _brain_like(shape=(24, 24, 16)) -> np.ndarray:
    return np.arange(int(np.prod(shape)), dtype=np.int64).reshape(shape).astype(np.int16)


def _phantom_bytes(tmp_path: Path) -> bytes:
    intensity, _labels, _truth = generate_synthetic_scan(0.6, seed=3)
    path = tmp_path / "phantom.nii.gz"
    save_nifti(intensity, path)
    return path.read_bytes()


async def _patient(client: AsyncClient, source: str = "Clinic") -> dict:
    resp = await client.post("/api/v1/patients", json={"age": 68, "sex": "M", "source": source})
    assert resp.status_code == 201
    return resp.json()


async def _upload(client, patient, content: bytes, filename="scan.nii.gz", **params):
    return await client.post(
        f"/api/v1/patients/{patient['id']}/scans/upload",
        files={"file": (filename, content, "application/octet-stream")},
        params=params,
    )


async def _upload_dicom(client, patient, content: bytes, filename="series.zip", **params):
    return await client.post(
        f"/api/v1/patients/{patient['id']}/scans/upload-dicom",
        files={"file": (filename, content, "application/zip")},
        params=params,
    )


def _stats_files() -> dict:
    return {
        field: (name, (FREESURFER / name).read_bytes(), "text/plain")
        for field, name in (
            ("aseg", "aseg.stats"),
            ("lh_aparc", "lh.aparc.stats"),
            ("rh_aparc", "rh.aparc.stats"),
        )
    }


class TestExternalProvider:
    async def test_upload_is_stored_and_awaits_volumetry(
        self, client: AsyncClient, upload_dir, tmp_path
    ):
        patient = await _patient(client)
        resp = await _upload(
            client, patient, _nifti_bytes(tmp_path, _brain_like()), externalSubjectId="CLINIC-0001"
        )
        assert resp.status_code == 201
        scan = resp.json()

        assert scan["status"] == "awaiting_volumetry"
        assert scan["fisResult"] is None
        assert scan["biomarkers"] == []
        assert scan["extractionMethod"] is None
        assert scan["externalSubjectId"] == "CLINIC-0001"

    async def test_clinic_flow_upload_then_attach_stats(
        self, client: AsyncClient, upload_dir, tmp_path
    ):
        """The whole external path: store the scan, run FreeSurfer
        elsewhere, attach its stats, get a scored scan.
        """
        patient = await _patient(client)
        scan = (await _upload(client, patient, _nifti_bytes(tmp_path, _brain_like()))).json()
        scan_url = f"/api/v1/patients/{patient['id']}/scans/{scan['id']}"

        assert (await client.get(scan_url)).json()["status"] == "awaiting_volumetry"

        attached = await client.post(f"{scan_url}/stats", files=_stats_files())
        assert attached.status_code == 200
        assert attached.json()["status"] == "completed"

        final = (await client.get(scan_url)).json()
        assert final["status"] == "completed"
        assert final["extractionMethod"] == "freesurfer_stats"
        assert len(final["biomarkers"]) == 3
        assert final["fisResult"]["determinate"] is False  # phantom calibration only

    async def test_file_is_stored_under_a_server_generated_name(
        self, client: AsyncClient, upload_dir, tmp_path
    ):
        patient = await _patient(client)
        resp = await _upload(
            client,
            patient,
            _nifti_bytes(tmp_path, _brain_like()),
            filename="DOE_JANE_1940-01-01_T1.nii.gz",
        )
        assert resp.status_code == 201

        stored = _files_under(upload_dir)
        assert len(stored) == 1
        name = stored[0].name
        assert name.endswith(".nii.gz")
        assert len(name) == 32 + len(".nii.gz")
        assert set(name[:32]) <= set("0123456789abcdef")
        assert "DOE" not in name and "JANE" not in name
        # Nothing is left in the scratch area.
        assert _files_under(upload_dir / "incoming") == []

    @pytest.mark.parametrize(
        "filename",
        ["../../evil.nii.gz", "..\\..\\evil.nii.gz", "/etc/evil.nii.gz", "C:\\evil.nii.gz"],
    )
    async def test_path_traversal_in_the_filename_has_no_effect(
        self, client: AsyncClient, upload_dir, tmp_path, filename
    ):
        patient = await _patient(client)
        resp = await _upload(
            client, patient, _nifti_bytes(tmp_path, _brain_like()), filename=filename
        )
        assert resp.status_code == 201

        stored = _files_under(upload_dir)
        assert len(stored) == 1
        assert stored[0].parent == upload_dir
        assert [p for p in _files_under(tmp_path) if "evil" in p.name] == []

    async def test_trailing_singleton_dimension_is_squeezed_on_the_way_in(
        self, client: AsyncClient, upload_dir, tmp_path
    ):
        data = _brain_like().reshape(24, 24, 16, 1)
        patient = await _patient(client)
        resp = await _upload(client, patient, _nifti_bytes(tmp_path, data))
        assert resp.status_code == 201

        (stored,) = _files_under(upload_dir)
        assert nib.load(str(stored)).shape == (24, 24, 16)

    async def test_identifying_header_text_is_not_stored(
        self, client: AsyncClient, upload_dir, tmp_path
    ):
        content = _nifti_bytes(
            tmp_path,
            _brain_like(),
            descrip=b"DOE^JANE MRN12345",
            aux_file=b"jane_doe.par",
            db_name=b"DOE JANE",
        )
        assert b"DOE^JANE" in gzip.decompress(content)

        patient = await _patient(client)
        assert (await _upload(client, patient, content)).status_code == 201

        (stored,) = _files_under(upload_dir)
        raw = gzip.decompress(stored.read_bytes())
        for needle in (b"DOE", b"JANE", b"MRN12345", b"jane_doe"):
            assert needle not in raw

    async def test_phantom_patient_is_refused(self, client: AsyncClient, upload_dir, tmp_path):
        patient = await _patient(client, source="PHANTOM")
        resp = await _upload(client, patient, _nifti_bytes(tmp_path, _brain_like()))
        assert resp.status_code == 422
        assert "PHANTOM" in resp.json()["error"]["message"]
        assert _files_under(upload_dir) == []

    async def test_invalid_subject_id_is_refused_before_the_file_is_stored(
        self, client: AsyncClient, upload_dir, tmp_path
    ):
        patient = await _patient(client)
        resp = await _upload(
            client, patient, _nifti_bytes(tmp_path, _brain_like()), externalSubjectId="Jane Doe"
        )
        assert resp.status_code == 422
        assert _files_under(upload_dir) == []


class TestFileTypeByContent:
    async def test_text_called_nii_gz_is_rejected(self, client: AsyncClient, upload_dir):
        patient = await _patient(client)
        resp = await _upload(client, patient, b"definitely not an image\n" * 50, "scan.nii.gz")
        assert resp.status_code == 422
        assert "Only NIfTI volumes" in resp.json()["error"]["message"]
        assert _files_under(upload_dir) == []

    async def test_nifti_with_a_misleading_name_is_accepted(
        self, client: AsyncClient, upload_dir, tmp_path
    ):
        patient = await _patient(client)
        resp = await _upload(client, patient, _nifti_bytes(tmp_path, _brain_like()), "notes.txt")
        assert resp.status_code == 201

    async def test_uncompressed_nifti_is_accepted(self, client: AsyncClient, upload_dir, tmp_path):
        patient = await _patient(client)
        content = _nifti_bytes(tmp_path, _brain_like(), name="scan.nii")
        resp = await _upload(client, patient, content, "scan.nii")
        assert resp.status_code == 201
        (stored,) = _files_under(upload_dir)
        assert stored.name.endswith(".nii.gz")  # always stored compressed

    async def test_zip_sent_to_the_nifti_endpoint_is_redirected(
        self, client: AsyncClient, upload_dir
    ):
        patient = await _patient(client)
        resp = await _upload(client, patient, zip_files({"a.dcm": b"x"}), "scan.nii.gz")
        assert resp.status_code == 422
        assert "/scans/upload-dicom" in resp.json()["error"]["message"]

    async def test_single_dicom_file_is_redirected(self, client: AsyncClient, upload_dir, tmp_path):
        patient = await _patient(client)
        one_slice = make_series(tmp_path / "s")[0].read_bytes()
        resp = await _upload(client, patient, one_slice, "IM0001.dcm")
        assert resp.status_code == 422
        assert "single DICOM file" in resp.json()["error"]["message"]

    async def test_time_series_is_rejected(self, client: AsyncClient, upload_dir, tmp_path):
        patient = await _patient(client)
        resp = await _upload(client, patient, _nifti_bytes(tmp_path, np.zeros((8, 8, 8, 4))))
        assert resp.status_code == 422
        assert "has 4 dimensions" in resp.json()["error"]["message"]
        assert _files_under(upload_dir) == []

    async def test_implausible_voxel_size_is_rejected(
        self, client: AsyncClient, upload_dir, tmp_path
    ):
        img = nib.Nifti1Image(_brain_like(), np.diag([1.0, 1.0, 60.0, 1.0]))
        path = tmp_path / "thick.nii.gz"
        nib.save(img, str(path))
        patient = await _patient(client)
        resp = await _upload(client, patient, path.read_bytes())
        assert resp.status_code == 422
        assert "outside the accepted" in resp.json()["error"]["message"]

    async def test_error_does_not_echo_the_file(self, client: AsyncClient, upload_dir):
        patient = await _patient(client)
        resp = await _upload(client, patient, b"PRIVATE-CONTENT-MARKER " * 40, "DOE_JANE.nii.gz")
        assert resp.status_code == 422
        assert "PRIVATE-CONTENT-MARKER" not in resp.text
        assert "DOE_JANE" not in resp.text


class TestSizeLimit:
    async def test_max_upload_mb_is_enforced(self, client: AsyncClient, upload_dir, monkeypatch):
        monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
        patient = await _patient(client)
        resp = await _upload(client, patient, b"\x00" * (1024 * 1024 + 1))
        assert resp.status_code == 422
        assert resp.json()["error"]["message"] == "File exceeds the 1MB upload limit."
        assert _files_under(upload_dir) == []

    async def test_limit_applies_to_dicom_archives_too(
        self, client: AsyncClient, upload_dir, monkeypatch
    ):
        monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
        patient = await _patient(client)
        resp = await _upload_dicom(client, patient, b"PK\x03\x04" + b"\x00" * (1024 * 1024))
        assert resp.status_code == 422
        assert "exceeds the 1MB upload limit" in resp.json()["error"]["message"]
        assert _files_under(upload_dir) == []

    async def test_empty_upload(self, client: AsyncClient, upload_dir):
        patient = await _patient(client)
        resp = await _upload(client, patient, b"")
        assert resp.status_code == 422
        assert "empty" in resp.json()["error"]["message"]


class TestInhouseProvider:
    async def test_phantom_patient_upload_is_scored_in_the_request(
        self, client: AsyncClient, upload_dir, provider, tmp_path
    ):
        provider("inhouse")
        patient = await _patient(client, source="PHANTOM")
        resp = await _upload(client, patient, _phantom_bytes(tmp_path))
        assert resp.status_code == 201
        scan = resp.json()

        assert scan["status"] == "completed"
        assert scan["extractionMethod"] == "inhouse_phantom"
        assert len(scan["biomarkers"]) == 3
        assert scan["fisResult"]["determinate"] is True
        assert scan["fisResult"]["stage"] in ("CN", "MCI", "AD")
        assert len(_files_under(upload_dir)) == 1

    @pytest.mark.parametrize("source", ["Clinic", "OASIS", "ADNI"])
    async def test_any_other_source_is_refused_with_the_documented_message(
        self, client: AsyncClient, upload_dir, provider, tmp_path, source
    ):
        provider("inhouse")
        patient = await _patient(client, source=source)
        resp = await _upload(client, patient, _phantom_bytes(tmp_path))

        assert resp.status_code == 422
        assert resp.json()["error"]["message"] == INHOUSE_MESSAGE
        # Refused before the body was stored.
        assert _files_under(upload_dir) == []
        fetched = (await client.get(f"/api/v1/patients/{patient['id']}")).json()
        assert fetched["scans"] == []

    async def test_failed_processing_leaves_no_file_behind(
        self, client: AsyncClient, upload_dir, provider, tmp_path
    ):
        provider("inhouse")
        patient = await _patient(client, source="PHANTOM")
        # A valid NIfTI, but not the phantom's 64x64x64 grid.
        resp = await _upload(client, patient, _nifti_bytes(tmp_path, _brain_like()))
        assert resp.status_code == 422
        assert _files_under(upload_dir) == []

    async def test_dicom_is_refused(self, client: AsyncClient, upload_dir, provider, tmp_path):
        provider("inhouse")
        patient = await _patient(client, source="PHANTOM")
        resp = await _upload_dicom(client, patient, zip_series(make_series(tmp_path / "s")))
        assert resp.status_code == 422
        assert resp.json()["error"]["message"] == INHOUSE_MESSAGE


class TestFastSurferProvider:
    async def test_upload_returns_immediately_and_the_scan_is_polled_to_completion(
        self, client: AsyncClient, app_db, upload_dir, provider, tmp_path, monkeypatch
    ):
        provider("fastsurfer_docker")
        launched = []
        monkeypatch.setattr(vp, "launch_fastsurfer_job", launched.append)

        patient = await _patient(client)
        resp = await _upload(client, patient, _nifti_bytes(tmp_path, _brain_like()))
        assert resp.status_code == 201
        scan = resp.json()
        scan_url = f"/api/v1/patients/{patient['id']}/scans/{scan['id']}"

        # The request did not wait for segmentation.
        assert scan["status"] == "segmenting"
        assert scan["extractionMethod"] == "fastsurfer"
        assert scan["fisResult"] is None
        assert [str(scan_id) for scan_id in launched] == [scan["id"]]
        assert (await client.get(scan_url)).json()["status"] == "segmenting"

        # The background job, run here with a stand-in for the container.
        await vp.run_fastsurfer_job(
            launched[0], session_factory=app_db, runner=lambda *args: FASTSURFER_STATS
        )

        polled = (await client.get(scan_url)).json()
        assert polled["status"] == "completed"
        assert polled["failureReason"] is None
        assert len(polled["biomarkers"]) == 3
        assert polled["fisResult"]["needsReview"] is True

    async def test_failed_segmentation_is_visible_when_polling_and_recoverable(
        self, client: AsyncClient, app_db, upload_dir, provider, tmp_path, monkeypatch
    ):
        provider("fastsurfer_docker")
        launched = []
        monkeypatch.setattr(vp, "launch_fastsurfer_job", launched.append)
        patient = await _patient(client)
        scan = (await _upload(client, patient, _nifti_bytes(tmp_path, _brain_like()))).json()
        scan_url = f"/api/v1/patients/{patient['id']}/scans/{scan['id']}"

        def runner(*args):
            raise vp.FastSurferError("FastSurfer did not finish within 10800 seconds.")

        await vp.run_fastsurfer_job(launched[0], session_factory=app_db, runner=runner)

        polled = (await client.get(scan_url)).json()
        assert polled["status"] == "failed"
        assert polled["failureReason"] == "FastSurfer did not finish within 10800 seconds."

        # The stored scan is not lost: stats produced by hand finish it.
        attached = await client.post(f"{scan_url}/stats", files=_stats_files())
        assert attached.status_code == 200
        assert attached.json()["status"] == "completed"

    async def test_phantom_patient_is_refused(
        self, client: AsyncClient, upload_dir, provider, tmp_path
    ):
        provider("fastsurfer_docker")
        patient = await _patient(client, source="PHANTOM")
        resp = await _upload(client, patient, _nifti_bytes(tmp_path, _brain_like()))
        assert resp.status_code == 422


@pytest.fixture
def no_dcm2niix(monkeypatch):
    """Use the built-in converter, whether or not dcm2niix is installed."""
    monkeypatch.setattr(dicom_ingest, "dcm2niix_path", lambda: None)


@pytest.mark.usefixtures("no_dcm2niix")
class TestDicomUpload:
    async def test_anonymised_series_is_converted_and_stored_as_nifti(
        self, client: AsyncClient, upload_dir, tmp_path
    ):
        patient = await _patient(client)
        archive = zip_series(make_series(tmp_path / "s"), folder="study/series1")
        resp = await _upload_dicom(client, patient, archive, externalSubjectId="CLINIC-0002")
        assert resp.status_code == 201
        scan = resp.json()

        assert scan["status"] == "awaiting_volumetry"
        assert scan["externalSubjectId"] == "CLINIC-0002"
        (stored,) = _files_under(upload_dir)
        assert stored.name.endswith(".nii.gz")
        assert nib.load(str(stored)).shape == SHAPE

    async def test_original_dicom_is_not_kept(self, client: AsyncClient, upload_dir, tmp_path):
        patient = await _patient(client)
        resp = await _upload_dicom(client, patient, zip_series(make_series(tmp_path / "s")))
        assert resp.status_code == 201

        remaining = _files_under(upload_dir)
        assert [p.suffix for p in remaining] == [".gz"]
        assert not any(p.suffix in (".dcm", ".zip") for p in remaining)
        assert list((upload_dir / "incoming").iterdir()) == []

    async def test_non_anonymised_series_is_rejected_listing_tag_names_only(
        self, client: AsyncClient, upload_dir, tmp_path
    ):
        patient = await _patient(client)
        series = make_series(tmp_path / "s", identifying=IDENTIFYING_VALUES)
        resp = await _upload_dicom(client, patient, zip_series(series))

        assert resp.status_code == 422
        error = resp.json()["error"]
        assert error["code"] == "dicom_not_anonymised"
        for name, value in IDENTIFYING_VALUES.items():
            assert name in error["message"]
            assert value not in resp.text
        for fragment in ("ZZTEST", "LEAK", "Leaktown", "19400101"):
            assert fragment not in resp.text

    async def test_rejected_dicom_is_deleted_too(self, client: AsyncClient, upload_dir, tmp_path):
        patient = await _patient(client)
        series = make_series(tmp_path / "s", identifying={"PatientName": "ZZTEST^LEAKED"})
        resp = await _upload_dicom(client, patient, zip_series(series))
        assert resp.status_code == 422

        assert _files_under(upload_dir) == []
        fetched = (await client.get(f"/api/v1/patients/{patient['id']}")).json()
        assert fetched["scans"] == []

    async def test_gate_is_on_by_default_and_can_be_turned_off(
        self, client: AsyncClient, upload_dir, tmp_path, monkeypatch
    ):
        assert get_settings().require_anonymised_dicom is True
        monkeypatch.setattr(get_settings(), "require_anonymised_dicom", False)
        patient = await _patient(client)
        series = make_series(tmp_path / "s", identifying=IDENTIFYING_VALUES)

        resp = await _upload_dicom(client, patient, zip_series(series))
        assert resp.status_code == 201
        # Even then, no header value reaches the stored file or the response.
        (stored,) = _files_under(upload_dir)
        raw = gzip.decompress(stored.read_bytes())
        for value in IDENTIFYING_VALUES.values():
            assert value.encode() not in raw
            assert value not in resp.text

    async def test_path_traversal_entry_rejects_the_archive(
        self, client: AsyncClient, upload_dir, tmp_path
    ):
        series = make_series(tmp_path / "s")
        files = {f"s/{p.name}": p.read_bytes() for p in series}
        files["../../outside.dcm"] = series[0].read_bytes()
        patient = await _patient(client)

        resp = await _upload_dicom(client, patient, zip_files(files))
        assert resp.status_code == 422
        assert "'..' segment" in resp.json()["error"]["message"]
        assert _files_under(upload_dir) == []
        assert [p for p in _files_under(tmp_path) if p.name == "outside.dcm"] == []

    async def test_nifti_sent_to_the_dicom_endpoint_is_redirected(
        self, client: AsyncClient, upload_dir, tmp_path
    ):
        patient = await _patient(client)
        resp = await _upload_dicom(client, patient, _nifti_bytes(tmp_path, _brain_like()))
        assert resp.status_code == 422
        assert "/scans/upload" in resp.json()["error"]["message"]
        assert _files_under(upload_dir) == []

    async def test_archive_without_dicom(self, client: AsyncClient, upload_dir):
        patient = await _patient(client)
        resp = await _upload_dicom(client, patient, zip_files({"readme.txt": b"nothing here"}))
        assert resp.status_code == 422
        assert "no DICOM files" in resp.json()["error"]["message"]

    async def test_unconvertible_series_tells_the_user_to_upload_nifti(
        self, client: AsyncClient, upload_dir, tmp_path
    ):
        patient = await _patient(client)
        series = make_series(tmp_path / "s", skip_slices=(1,))
        resp = await _upload_dicom(client, patient, zip_series(series))
        assert resp.status_code == 422
        assert "upload the NIfTI file instead" in resp.json()["error"]["message"]
        assert _files_under(upload_dir) == []

    async def test_phantom_patient_is_refused(self, client: AsyncClient, upload_dir, tmp_path):
        patient = await _patient(client, source="PHANTOM")
        resp = await _upload_dicom(client, patient, zip_series(make_series(tmp_path / "s")))
        assert resp.status_code == 422
        assert _files_under(upload_dir) == []

    async def test_unknown_patient(self, client: AsyncClient, upload_dir, tmp_path):
        resp = await client.post(
            "/api/v1/patients/00000000-0000-0000-0000-000000000000/scans/upload-dicom",
            files={"file": ("s.zip", zip_series(make_series(tmp_path / "s")), "application/zip")},
        )
        assert resp.status_code == 404
