"""Phase 3: DICOM ingestion. Safe unpacking, the de-identification gate,
conversion to NIfTI, and never echoing a header value.

Every DICOM here is synthesised in a tmp directory by tests/dicom_utils.py.
"""

import stat
import subprocess
import zipfile
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

from app.core.config import Settings
from app.core.exceptions import UnprocessableScanError
from app.services import dicom_ingest
from app.services.dicom_ingest import (
    IdentifiableDicomError,
    check_deidentified,
    convert_dicom_zip,
    convert_with_dcm2niix,
    convert_with_pydicom,
    discard,
    extract_dicom_zip,
    find_identifying_tags,
)
from tests.dicom_utils import (
    IDENTIFYING_VALUES,
    SHAPE,
    make_series,
    synthetic_volume,
    write_zip,
    zip_files,
    zip_series,
)

SETTINGS = Settings(_env_file=None)
TAGS = SETTINGS.dicom_identifying_tags


def _all_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


class TestExtractDicomZip:
    def test_extracts_dicom_under_server_chosen_names(self, tmp_path):
        series = make_series(tmp_path / "src")
        archive = write_zip(tmp_path / "u.zip", zip_series(series, folder="DOE_JANE_19400101"))

        extracted = extract_dicom_zip(archive, tmp_path / "work", SETTINGS)

        assert len(extracted) == SHAPE[2]
        assert [p.name for p in extracted] == [f"{i:06d}.dcm" for i in range(1, SHAPE[2] + 1)]
        # The patient-named folder inside the archive is not reproduced.
        assert all("DOE" not in str(p) for p in _all_files(tmp_path / "work"))

    def test_non_dicom_entries_are_skipped(self, tmp_path):
        series = make_series(tmp_path / "src")
        files = {f"s/{p.name}": p.read_bytes() for p in series}
        files["s/README.txt"] = b"notes about the patient"
        files["s/thumbs.db"] = b"\x00" * 500
        archive = write_zip(tmp_path / "u.zip", zip_files(files))

        extracted = extract_dicom_zip(archive, tmp_path / "work", SETTINGS)
        assert len(extracted) == SHAPE[2]
        assert len(_all_files(tmp_path / "work")) == SHAPE[2]

    @pytest.mark.parametrize(
        "evil_name",
        [
            "../evil.dcm",
            "series/../../evil.dcm",
            "/etc/evil.dcm",
            "\\\\server\\share\\evil.dcm",
            "C:\\Windows\\evil.dcm",
            "C:/Windows/evil.dcm",
            "..\\evil.dcm",
            "series\\..\\..\\evil.dcm",
        ],
    )
    def test_path_traversal_rejects_the_whole_archive(self, tmp_path, evil_name):
        series = make_series(tmp_path / "src")
        files = {f"s/{p.name}": p.read_bytes() for p in series}
        files[evil_name] = series[0].read_bytes()
        archive = write_zip(tmp_path / "u.zip", zip_files(files))
        work = tmp_path / "jail" / "work"

        with pytest.raises(UnprocessableScanError, match="absolute path or a '..' segment"):
            extract_dicom_zip(archive, work, SETTINGS)

        # Nothing was written anywhere, inside the work directory or out of it.
        assert not work.exists()
        assert [p.name for p in _all_files(tmp_path) if p.name == "evil.dcm"] == []

    def test_symlink_entry_is_rejected(self, tmp_path):
        archive = tmp_path / "u.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            link = zipfile.ZipInfo("series/link.dcm")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(link, "/etc/passwd")
        with pytest.raises(UnprocessableScanError, match="symbolic link"):
            extract_dicom_zip(archive, tmp_path / "work", SETTINGS)

    def test_no_dicom_inside(self, tmp_path):
        archive = write_zip(tmp_path / "u.zip", zip_files({"a.txt": b"hello", "b.bin": b"x" * 300}))
        with pytest.raises(UnprocessableScanError, match="contains no DICOM files"):
            extract_dicom_zip(archive, tmp_path / "work", SETTINGS)

    def test_not_a_zip(self, tmp_path):
        archive = write_zip(tmp_path / "u.zip", b"PK\x03\x04 but not really a zip")
        with pytest.raises(UnprocessableScanError, match="not a readable .zip"):
            extract_dicom_zip(archive, tmp_path / "work", SETTINGS)

    def test_file_count_limit(self, tmp_path):
        series = make_series(tmp_path / "src")
        archive = write_zip(tmp_path / "u.zip", zip_series(series))
        limited = Settings(_env_file=None, dicom_zip_max_files=2)
        with pytest.raises(UnprocessableScanError, match="more than the 2 allowed"):
            extract_dicom_zip(archive, tmp_path / "work", limited)

    def test_unpacked_size_limit_stops_a_zip_bomb(self, tmp_path):
        header = make_series(tmp_path / "src")[0].read_bytes()
        bomb = header + b"\x00" * (3 * 1024 * 1024)  # compresses to almost nothing
        archive = write_zip(tmp_path / "u.zip", zip_files({"bomb.dcm": bomb}))
        assert archive.stat().st_size < 50_000
        limited = Settings(_env_file=None, dicom_zip_max_unpacked_mb=1)
        with pytest.raises(UnprocessableScanError, match="unpacks to more than the 1MB"):
            extract_dicom_zip(archive, tmp_path / "work", limited)

    @pytest.mark.parametrize("name", ["", "a\x00b.dcm"])
    def test_unsafe_name_helper_edge_cases(self, name):
        assert dicom_ingest._is_unsafe_entry_name(name)

    @pytest.mark.parametrize("name", ["a.dcm", "series/IM0001.dcm", "a/b/c/..d.dcm", "x..y/z"])
    def test_ordinary_names_are_safe(self, name):
        assert not dicom_ingest._is_unsafe_entry_name(name)


class TestDeidentificationGate:
    def test_anonymised_series_passes(self, tmp_path):
        series = make_series(tmp_path / "s")
        assert find_identifying_tags(series, TAGS) == []
        assert check_deidentified(series, SETTINGS) == []

    def test_default_tag_list(self):
        assert TAGS == [
            "PatientName",
            "PatientID",
            "PatientBirthDate",
            "OtherPatientIDs",
            "InstitutionName",
            "ReferringPhysicianName",
            "AccessionNumber",
            "StudyDate",
        ]
        assert SETTINGS.require_anonymised_dicom is True

    @pytest.mark.parametrize("tag", list(IDENTIFYING_VALUES))
    def test_each_identifying_tag_is_caught_on_its_own(self, tmp_path, tag):
        series = make_series(tmp_path / "s", identifying={tag: IDENTIFYING_VALUES[tag]})
        assert find_identifying_tags(series, TAGS) == [tag]
        with pytest.raises(IdentifiableDicomError) as excinfo:
            check_deidentified(series, SETTINGS)
        assert tag in excinfo.value.message
        assert IDENTIFYING_VALUES[tag] not in excinfo.value.message

    def test_error_lists_tag_names_and_no_values(self, tmp_path):
        series = make_series(tmp_path / "s", identifying=IDENTIFYING_VALUES)
        with pytest.raises(IdentifiableDicomError) as excinfo:
            check_deidentified(series, SETTINGS)

        message = excinfo.value.message
        for name, value in IDENTIFYING_VALUES.items():
            assert name in message
            assert value not in message
        for fragment in ("ZZTEST", "LEAK", "Leaktown", "1940", "2026"):
            assert fragment not in message
        assert excinfo.value.status_code == 422
        assert excinfo.value.code == "dicom_not_anonymised"

    def test_names_are_reported_in_configured_order(self, tmp_path):
        series = make_series(
            tmp_path / "s", identifying={"StudyDate": "20260101", "PatientID": "MRN-LEAK-0001"}
        )
        assert find_identifying_tags(series, TAGS) == ["PatientID", "StudyDate"]

    def test_a_tag_set_in_only_one_slice_is_still_caught(self, tmp_path):
        from pydicom import dcmread

        series = make_series(tmp_path / "s")
        last = dcmread(series[-1])
        last.PatientName = "ZZTEST^LEAKED"
        last.save_as(str(series[-1]), enforce_file_format=True)
        assert find_identifying_tags(series, TAGS) == ["PatientName"]

    def test_whitespace_only_values_count_as_blank(self, tmp_path):
        series = make_series(tmp_path / "s", identifying={"InstitutionName": "   "})
        assert find_identifying_tags(series, TAGS) == []

    def test_gate_can_be_turned_off_and_still_logs_no_values(self, tmp_path, capsys, caplog):
        series = make_series(tmp_path / "s", identifying=IDENTIFYING_VALUES)
        relaxed = Settings(_env_file=None, require_anonymised_dicom=False)

        with caplog.at_level("WARNING"):
            populated = check_deidentified(series, relaxed)

        assert populated == TAGS
        logged = caplog.text + capsys.readouterr().out
        for value in IDENTIFYING_VALUES.values():
            assert value not in logged

    def test_tag_list_is_configurable(self, tmp_path):
        series = make_series(tmp_path / "s", identifying={"StudyDate": "20260101"})
        without_dates = Settings(
            _env_file=None, dicom_identifying_tags=["PatientName", "PatientID"]
        )
        assert check_deidentified(series, without_dates) == []

    @pytest.mark.filterwarnings("ignore::UserWarning")  # pydicom warns about the damage
    def test_unreadable_dicom_is_a_clean_error(self, tmp_path):
        bad = tmp_path / "bad.dcm"
        bad.write_bytes(b"\x00" * 128 + b"DICM" + b"\xff" * 64)
        with pytest.raises(UnprocessableScanError, match="not readable DICOM"):
            find_identifying_tags([bad], TAGS)


class TestPydicomConversion:
    def test_geometry_and_voxels_are_converted_faithfully(self, tmp_path):
        series = make_series(tmp_path / "s", slice_spacing=2.0, origin=(-10.0, -20.0, 30.0))
        out = convert_with_pydicom(series, tmp_path / "out.nii.gz")

        img = nib.load(str(out))
        assert img.shape == SHAPE
        # [column, row, slice], each voxel still encoding its own position.
        assert np.array_equal(np.asanyarray(img.dataobj), synthetic_volume().astype(np.float32))
        # LPS -> RAS flips the first two axes and their origin.
        expected = np.array(
            [[-1.0, 0.0, 0.0, 10.0], [0.0, -1.0, 0.0, 20.0], [0.0, 0.0, 2.0, 30.0], [0, 0, 0, 1]]
        )
        assert np.allclose(img.affine, expected)

    def test_pixel_spacing_is_row_then_column(self, tmp_path):
        series = make_series(tmp_path / "s", pixel_spacing=(0.8, 1.5), slice_spacing=3.0)
        img = nib.load(str(convert_with_pydicom(series, tmp_path / "out.nii.gz")))
        # First NIfTI axis runs along columns, so it takes the column spacing.
        assert np.allclose(np.abs(np.diag(img.affine)[:3]), [1.5, 0.8, 3.0])

    def test_slices_are_ordered_by_position_not_filename(self, tmp_path):
        """make_series names files in reverse slice order on purpose."""
        series = make_series(tmp_path / "s")
        by_name = sorted(series, key=lambda p: p.name)
        img = nib.load(str(convert_with_pydicom(by_name, tmp_path / "out.nii.gz")))
        assert np.array_equal(np.asanyarray(img.dataobj), synthetic_volume().astype(np.float32))

    def test_rescale_slope_and_intercept_are_applied(self, tmp_path):
        series = make_series(tmp_path / "s", rescale=(2.0, -5.0))
        img = nib.load(str(convert_with_pydicom(series, tmp_path / "out.nii.gz")))
        assert np.allclose(np.asanyarray(img.dataobj), synthetic_volume() * 2.0 - 5.0)

    def test_missing_slice_is_refused(self, tmp_path):
        series = make_series(tmp_path / "s", skip_slices=(1,))
        with pytest.raises(UnprocessableScanError, match="missing or unevenly spaced slices"):
            convert_with_pydicom(series, tmp_path / "out.nii.gz")

    def test_two_series_are_refused(self, tmp_path):
        a = make_series(tmp_path / "a", name_prefix="A")
        b = make_series(tmp_path / "b", name_prefix="B", origin=(0.0, 0.0, 500.0))
        with pytest.raises(UnprocessableScanError, match="contains 2 series"):
            convert_with_pydicom(a + b, tmp_path / "out.nii.gz")

    def test_single_slice_is_refused(self, tmp_path):
        series = make_series(tmp_path / "s")[:1]
        with pytest.raises(UnprocessableScanError, match="multi-slice image series"):
            convert_with_pydicom(series, tmp_path / "out.nii.gz")

    def test_errors_point_at_the_nifti_route(self, tmp_path):
        series = make_series(tmp_path / "s", skip_slices=(2,))
        with pytest.raises(UnprocessableScanError, match="upload the NIfTI file instead"):
            convert_with_pydicom(series, tmp_path / "out.nii.gz")


class TestConvertDicomZip:
    def test_end_to_end_with_the_fallback_converter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dicom_ingest, "dcm2niix_path", lambda: None)
        archive = write_zip(tmp_path / "u.zip", zip_series(make_series(tmp_path / "src")))

        out = convert_dicom_zip(archive, tmp_path / "work", settings=SETTINGS)

        assert out.is_relative_to(tmp_path / "work")
        assert nib.load(str(out)).shape == SHAPE

    def test_gate_runs_before_conversion(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(dicom_ingest, "dcm2niix_path", lambda: None)
        monkeypatch.setattr(
            dicom_ingest, "convert_with_pydicom", lambda *a, **k: calls.append("converted")
        )
        series = make_series(tmp_path / "src", identifying={"PatientName": "ZZTEST^LEAKED"})
        archive = write_zip(tmp_path / "u.zip", zip_series(series))

        with pytest.raises(IdentifiableDicomError):
            convert_dicom_zip(archive, tmp_path / "work", settings=SETTINGS)
        assert calls == []

    def test_discard_removes_the_dicom(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dicom_ingest, "dcm2niix_path", lambda: None)
        archive = write_zip(tmp_path / "u.zip", zip_series(make_series(tmp_path / "src")))
        work = tmp_path / "work"
        convert_dicom_zip(archive, work, settings=SETTINGS)
        assert any(p.suffix == ".dcm" for p in _all_files(work))

        discard(work)
        assert not work.exists()
        discard(work)  # already gone: not an error


class TestDcm2niixConversion:
    def _fake_run(self, *, outputs=("converted.nii.gz",), returncode=0, seen=None):
        def run(command, **kwargs):
            if seen is not None:
                seen.append((command, kwargs))
            out_dir = Path(command[command.index("-o") + 1])
            for name in outputs:
                (out_dir / name).write_bytes(b"nifti")
            return subprocess.CompletedProcess(command, returncode)

        return run

    def test_command_suppresses_sidecars_and_fixes_the_output_name(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(subprocess, "run", self._fake_run(seen=seen))
        out = convert_with_dcm2niix("dcm2niix", tmp_path / "dicom", tmp_path / "out", 60)

        command, kwargs = seen[0]
        assert out.name == "converted.nii.gz"
        assert command[0] == "dcm2niix"
        assert command[command.index("-b") + 1] == "n"  # no JSON sidecar with header fields
        assert command[command.index("-f") + 1] == "converted"  # no patient name in the filename
        assert command[command.index("-z") + 1] == "y"
        assert kwargs["timeout"] == 60
        # Its console output can name the patient, so it is thrown away.
        assert kwargs["stdout"] == subprocess.DEVNULL and kwargs["stderr"] == subprocess.DEVNULL

    def test_failure_exit_code(self, tmp_path, monkeypatch):
        monkeypatch.setattr(subprocess, "run", self._fake_run(outputs=(), returncode=1))
        with pytest.raises(UnprocessableScanError, match="dcm2niix could not convert"):
            convert_with_dcm2niix("dcm2niix", tmp_path / "dicom", tmp_path / "out", 60)

    def test_several_outputs_mean_several_series(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", self._fake_run(outputs=("converted.nii.gz", "converted_e2.nii.gz"))
        )
        with pytest.raises(UnprocessableScanError, match="converts to 2 volumes"):
            convert_with_dcm2niix("dcm2niix", tmp_path / "dicom", tmp_path / "out", 60)

    def test_timeout(self, tmp_path, monkeypatch):
        def run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        monkeypatch.setattr(subprocess, "run", run)
        with pytest.raises(UnprocessableScanError, match="timed out"):
            convert_with_dcm2niix("dcm2niix", tmp_path / "dicom", tmp_path / "out", 5)

    def test_dcm2niix_is_used_when_it_is_on_path(self, tmp_path, monkeypatch):
        used = []
        monkeypatch.setattr(dicom_ingest, "dcm2niix_path", lambda: "/usr/bin/dcm2niix")
        monkeypatch.setattr(
            dicom_ingest,
            "convert_with_dcm2niix",
            lambda exe, d, o, t: used.append(exe) or tmp_path / "x.nii.gz",
        )
        archive = write_zip(tmp_path / "u.zip", zip_series(make_series(tmp_path / "src")))
        convert_dicom_zip(archive, tmp_path / "work", settings=SETTINGS)
        assert used == ["/usr/bin/dcm2niix"]
