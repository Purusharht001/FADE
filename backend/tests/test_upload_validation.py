"""Phase 3: receiving a scan file. Type detection by content, the size
limit, and NIfTI sanitising (3-D only, sane voxels, clean header).
"""

import gzip
import io
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
from fastapi import UploadFile

from app.core.config import Settings
from app.core.exceptions import UnprocessableScanError
from app.services import preprocessing
from app.services.upload_validation import (
    FileKind,
    detect_kind,
    new_upload_name,
    sanitize_nifti,
    store_upload,
)
from tests.dicom_utils import make_series, zip_files


def _volume(shape=(16, 16, 8), dtype=np.int16) -> np.ndarray:
    return np.arange(int(np.prod(shape)), dtype=np.int64).reshape(shape).astype(dtype)


def _save(path: Path, data: np.ndarray, affine: np.ndarray | None = None, **header) -> Path:
    img = nib.Nifti1Image(data, np.diag([1.0, 1.0, 1.25, 1.0]) if affine is None else affine)
    for field, value in header.items():
        img.header[field] = value
    nib.save(img, str(path))
    return path


class TestDetectKind:
    def test_uncompressed_nifti(self, tmp_path):
        assert detect_kind(_save(tmp_path / "a.nii", _volume())) == FileKind.NIFTI

    def test_gzipped_nifti(self, tmp_path):
        assert detect_kind(_save(tmp_path / "a.nii.gz", _volume())) == FileKind.NIFTI_GZ

    def test_nifti2(self, tmp_path):
        path = tmp_path / "a.nii"
        nib.save(nib.Nifti2Image(_volume(), np.eye(4)), str(path))
        assert detect_kind(path) == FileKind.NIFTI

    def test_the_name_is_ignored_for_a_real_nifti(self, tmp_path):
        source = _save(tmp_path / "a.nii.gz", _volume())
        disguised = tmp_path / "holiday-photo.jpg"
        disguised.write_bytes(source.read_bytes())
        assert detect_kind(disguised) == FileKind.NIFTI_GZ

    def test_the_name_does_not_make_text_a_nifti(self, tmp_path):
        fake = tmp_path / "scan.nii.gz"
        fake.write_bytes(b"this is not an image, whatever it is called\n" * 20)
        assert detect_kind(fake) == FileKind.UNKNOWN

    def test_gzip_of_something_else(self, tmp_path):
        path = tmp_path / "scan.nii.gz"
        path.write_bytes(gzip.compress(b"plain text, compressed" * 50))
        assert detect_kind(path) == FileKind.GZIP

    def test_truncated_gzip(self, tmp_path):
        path = tmp_path / "scan.nii.gz"
        path.write_bytes(b"\x1f\x8b\x08\x00garbage")
        assert detect_kind(path) == FileKind.GZIP

    def test_zip(self, tmp_path):
        path = tmp_path / "upload.bin"
        path.write_bytes(zip_files({"a.txt": b"hello"}))
        assert detect_kind(path) == FileKind.ZIP

    def test_dicom(self, tmp_path):
        slice_path = make_series(tmp_path / "s")[0]
        assert detect_kind(slice_path) == FileKind.DICOM

    def test_header_half_of_a_pair_is_recognised_and_distinct(self, tmp_path):
        nib.save(nib.Nifti1Pair(_volume(), np.eye(4)), str(tmp_path / "a.hdr"))
        assert detect_kind(tmp_path / "a.hdr") == FileKind.NIFTI_PAIR

    def test_empty_and_tiny_files(self, tmp_path):
        tiny = tmp_path / "tiny"
        tiny.write_bytes(b"ab")
        assert detect_kind(tiny) == FileKind.UNKNOWN


class TestStoreUpload:
    async def test_stores_the_bytes_and_returns_the_size(self, tmp_path):
        dest = tmp_path / "sub" / "out.bin"
        size = await store_upload(UploadFile(io.BytesIO(b"x" * 5000)), dest, 10_000, 1)
        assert size == 5000
        assert dest.read_bytes() == b"x" * 5000

    async def test_over_the_limit_is_rejected_and_removed(self, tmp_path):
        dest = tmp_path / "out.bin"
        with pytest.raises(UnprocessableScanError, match="exceeds the 7MB upload limit"):
            await store_upload(UploadFile(io.BytesIO(b"x" * 5000)), dest, 4999, 7)
        assert not dest.exists()

    async def test_exactly_at_the_limit_is_accepted(self, tmp_path):
        dest = tmp_path / "out.bin"
        assert await store_upload(UploadFile(io.BytesIO(b"x" * 5000)), dest, 5000, 1) == 5000

    async def test_empty_upload_is_rejected(self, tmp_path):
        dest = tmp_path / "out.bin"
        with pytest.raises(UnprocessableScanError, match="empty"):
            await store_upload(UploadFile(io.BytesIO(b"")), dest, 100, 1)
        assert not dest.exists()

    def test_upload_names_are_server_generated(self):
        a, b = new_upload_name(".nii.gz"), new_upload_name(".nii.gz")
        assert a != b
        assert a.endswith(".nii.gz")
        assert len(a) == 32 + len(".nii.gz")
        assert set(a[:32]) <= set("0123456789abcdef")


class TestSanitizeNifti:
    def test_plain_3d_volume_round_trips(self, tmp_path):
        data = _volume()
        affine = np.array(
            [[0.0, -1.0, 0.0, 90.0], [1.0, 0.0, 0.0, -126.0], [0.0, 0.0, 1.25, -72.0], [0, 0, 0, 1]]
        )
        src = _save(tmp_path / "in.nii.gz", data, affine)
        dest = tmp_path / "out.nii.gz"

        info = sanitize_nifti(src, dest)

        assert info.shape == (16, 16, 8)
        assert info.squeezed is False
        assert info.voxel_dims_mm == pytest.approx((1.0, 1.0, 1.25))
        out = nib.load(str(dest))
        assert np.array_equal(np.asanyarray(out.dataobj), data)
        assert np.allclose(out.affine, affine)
        assert out.get_data_dtype() == np.int16

    def test_trailing_singleton_dimension_is_squeezed(self, tmp_path):
        """OASIS volumes converted from Analyze are (256, 256, 128, 1)."""
        data = _volume((16, 16, 8)).reshape(16, 16, 8, 1)
        src = _save(tmp_path / "in.nii", data)
        dest = tmp_path / "out.nii.gz"

        info = sanitize_nifti(src, dest)

        assert info.squeezed is True
        assert info.shape == (16, 16, 8)
        out = nib.load(str(dest))
        assert out.shape == (16, 16, 8)
        assert out.header["dim"][0] == 3
        assert np.array_equal(np.asanyarray(out.dataobj), data[..., 0])

    def test_several_trailing_singletons_are_squeezed(self, tmp_path):
        src = _save(tmp_path / "in.nii", _volume((16, 16, 8)).reshape(16, 16, 8, 1, 1))
        dest = tmp_path / "out.nii.gz"
        assert sanitize_nifti(src, dest).shape == (16, 16, 8)
        assert nib.load(str(dest)).shape == (16, 16, 8)

    def test_the_output_is_readable_by_the_pipeline_loader(self, tmp_path):
        src = _save(tmp_path / "in.nii", _volume((16, 16, 8)).reshape(16, 16, 8, 1))
        dest = tmp_path / "out.nii.gz"
        sanitize_nifti(src, dest)
        data, _affine = preprocessing.load_nifti(dest)
        assert data.ndim == 3

    def test_a_time_series_is_rejected(self, tmp_path):
        src = _save(tmp_path / "in.nii", _volume((16, 16, 8, 3)))
        with pytest.raises(UnprocessableScanError, match=r"has 4 dimensions \[16, 16, 8, 3\]"):
            sanitize_nifti(src, tmp_path / "out.nii.gz")
        assert not (tmp_path / "out.nii.gz").exists()

    def test_a_singleton_in_the_middle_is_not_squeezed(self, tmp_path):
        src = _save(tmp_path / "in.nii", _volume((16, 16, 1, 8)))
        with pytest.raises(UnprocessableScanError, match="has 4 dimensions"):
            sanitize_nifti(src, tmp_path / "out.nii.gz")

    def test_a_2d_image_is_rejected(self, tmp_path):
        src = _save(tmp_path / "in.nii", _volume((16, 16)))
        with pytest.raises(UnprocessableScanError, match="has 2 dimensions"):
            sanitize_nifti(src, tmp_path / "out.nii.gz")

    def test_a_single_slice_is_rejected(self, tmp_path):
        src = _save(tmp_path / "in.nii", _volume((16, 16, 1)))
        with pytest.raises(UnprocessableScanError, match="not a 3-D image"):
            sanitize_nifti(src, tmp_path / "out.nii.gz")

    @pytest.mark.parametrize("voxel", [0.01, 40.0])
    def test_implausible_voxel_size_is_rejected(self, tmp_path, voxel):
        src = _save(tmp_path / "in.nii", _volume(), np.diag([1.0, 1.0, voxel, 1.0]))
        with pytest.raises(UnprocessableScanError, match="outside the accepted 0.1-10 mm range"):
            sanitize_nifti(src, tmp_path / "out.nii.gz")

    def test_voxel_range_comes_from_settings(self, tmp_path):
        src = _save(tmp_path / "in.nii", _volume(), np.diag([1.0, 1.0, 1.25, 1.0]))
        strict = Settings(_env_file=None, nifti_voxel_mm_range=(0.5, 1.1))
        with pytest.raises(UnprocessableScanError, match="outside the accepted 0.5-1.1 mm range"):
            sanitize_nifti(src, tmp_path / "out.nii.gz", settings=strict)

    def test_orientation_matrix_disagreeing_with_pixdim_is_caught(self, tmp_path):
        img = nib.Nifti1Image(_volume(), np.diag([1.0, 1.0, 1.0, 1.0]))
        img.set_sform(np.diag([1.0, 1.0, 500.0, 1.0]), code=1)
        img.header.set_zooms((1.0, 1.0, 1.0))
        nib.save(img, str(tmp_path / "in.nii"))
        with pytest.raises(UnprocessableScanError, match="orientation matrix implies"):
            sanitize_nifti(tmp_path / "in.nii", tmp_path / "out.nii.gz")

    def test_non_millimetre_units_are_rejected(self, tmp_path):
        img = nib.Nifti1Image(_volume(), np.eye(4))
        img.header.set_xyzt_units("meter")
        nib.save(img, str(tmp_path / "in.nii"))
        with pytest.raises(UnprocessableScanError, match="millimetres are required"):
            sanitize_nifti(tmp_path / "in.nii", tmp_path / "out.nii.gz")

    def test_too_many_voxels_is_rejected_before_the_data_is_read(self, tmp_path):
        src = _save(tmp_path / "in.nii", _volume())
        small = Settings(_env_file=None, nifti_max_voxels=1000)
        with pytest.raises(UnprocessableScanError, match="2048 voxels, more than the 1000"):
            sanitize_nifti(src, tmp_path / "out.nii.gz", settings=small)

    def test_unreadable_file_is_rejected_without_detail(self, tmp_path):
        bad = tmp_path / "in.nii.gz"
        bad.write_bytes(b"SECRET-CONTENT not a nifti" * 30)
        with pytest.raises(UnprocessableScanError) as excinfo:
            sanitize_nifti(bad, tmp_path / "out.nii.gz")
        assert excinfo.value.message == "The file could not be read as a NIfTI volume."
        assert "SECRET" not in excinfo.value.message

    def test_truncated_voxel_data_is_rejected(self, tmp_path):
        src = _save(tmp_path / "full.nii", _volume((32, 32, 16)))
        truncated = tmp_path / "in.nii"
        truncated.write_bytes(src.read_bytes()[:2000])
        with pytest.raises(UnprocessableScanError, match="voxel data could not be read"):
            sanitize_nifti(truncated, tmp_path / "out.nii.gz")
        assert not (tmp_path / "out.nii.gz").exists()

    def test_float64_is_stored_as_float32(self, tmp_path):
        src = _save(tmp_path / "in.nii", _volume(dtype=np.float64))
        sanitize_nifti(src, tmp_path / "out.nii.gz")
        assert nib.load(str(tmp_path / "out.nii.gz")).get_data_dtype() == np.float32

    def test_intensity_scaling_is_applied_not_dropped(self, tmp_path):
        img = nib.Nifti1Image(_volume(), np.eye(4))
        img.header.set_slope_inter(2.0, 5.0)
        nib.save(img, str(tmp_path / "in.nii"))
        sanitize_nifti(tmp_path / "in.nii", tmp_path / "out.nii.gz")
        out = np.asanyarray(nib.load(str(tmp_path / "out.nii.gz")).dataobj)
        assert np.allclose(out, np.asanyarray(nib.load(str(tmp_path / "in.nii")).dataobj))


class TestHeaderScrubbing:
    """A NIfTI header has free-text fields that converters fill with
    whatever the source DICOM said. None of it may be stored.
    """

    def _identifying_nifti(self, path: Path) -> Path:
        img = nib.Nifti1Image(_volume(), np.eye(4))
        img.header["descrip"] = b"DOE^JANE 1940-01-01 MRN12345"
        img.header["aux_file"] = b"jane_doe_scan.par"
        img.header["db_name"] = b"DOE JANE"
        img.header["intent_name"] = b"DOE^JANE"
        img.header.extensions.append(
            nib.nifti1.Nifti1Extension("comment", b"PatientName=DOE^JANE; PatientID=MRN12345")
        )
        nib.save(img, str(path))
        return path

    def test_fixture_really_carries_the_identifying_text(self, tmp_path):
        raw = self._identifying_nifti(tmp_path / "in.nii").read_bytes()
        assert b"DOE^JANE" in raw and b"MRN12345" in raw

    def test_text_fields_are_blank_in_the_stored_file(self, tmp_path):
        src = self._identifying_nifti(tmp_path / "in.nii")
        dest = tmp_path / "out.nii.gz"
        sanitize_nifti(src, dest)

        header = nib.load(str(dest)).header
        for field in ("descrip", "aux_file", "db_name", "intent_name"):
            assert header[field].tobytes().strip(b"\x00") == b"", field

    def test_header_extensions_are_dropped(self, tmp_path):
        src = self._identifying_nifti(tmp_path / "in.nii")
        dest = tmp_path / "out.nii.gz"
        sanitize_nifti(src, dest)
        assert len(nib.load(str(dest)).header.extensions) == 0

    def test_no_identifying_bytes_remain_anywhere_in_the_stored_file(self, tmp_path):
        src = self._identifying_nifti(tmp_path / "in.nii")
        dest = tmp_path / "out.nii.gz"
        sanitize_nifti(src, dest)

        stored = gzip.decompress(dest.read_bytes())
        for needle in (b"DOE", b"JANE", b"MRN12345", b"1940", b"jane_doe"):
            assert needle not in stored
