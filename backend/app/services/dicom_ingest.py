"""DICOM uploads: unpack, check de-identification, convert to NIfTI, delete.

A clinic scan arrives as a `.zip` of DICOM files. The order of work is
fixed, and each step exists to keep identifying data out of this system:

1. **Unpack safely.** Entry names are checked for path traversal and then
   ignored: every file is written under a sequential name chosen here.
2. **De-identification gate.** Before anything is converted, the
   identifying header tags are read. If any is non-empty and
   `REQUIRE_ANONYMISED_DICOM` is on (the default), the upload is rejected.
   The error names the offending *tags*; no tag *value* is ever put in an
   error, a log line or the database.
3. **Convert** one series to NIfTI, with `dcm2niix` when it is on PATH and
   a pydicom + nibabel fallback otherwise.
4. **Delete the DICOM.** The whole work directory is removed whether the
   upload succeeded or failed. Only the NIfTI is kept, and its header is
   rebuilt by `upload_validation.sanitize_nifti` afterwards.
"""

import shutil
import stat
import subprocess
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath

import nibabel as nib
import numpy as np
import pydicom
from fastapi import status
from pydicom.errors import InvalidDicomError

from app.core.config import Settings, get_settings
from app.core.exceptions import AppError, UnprocessableScanError
from app.core.logging import get_logger

logger = get_logger(__name__)

_COPY_CHUNK = 1024 * 1024
# Tolerated relative jitter in slice spacing before a series is treated as
# having missing slices.
_SLICE_SPACING_REL_TOL = 0.05

_NIFTI_HINT = "Convert the series with dcm2niix and upload the NIfTI file instead."


class IdentifiableDicomError(AppError):
    """The DICOM still carries identifying header values."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "dicom_not_anonymised"


# --------------------------------------------------------------------------
# 1. Unpacking
# --------------------------------------------------------------------------


def _is_unsafe_entry_name(name: str) -> bool:
    """True for a zip entry whose name would escape the extraction directory
    if it were trusted: absolute paths, drive letters, `..` segments.
    """
    if not name or "\x00" in name:
        return True
    posix, windows = PurePosixPath(name), PureWindowsPath(name)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        return True
    if name.startswith(("/", "\\")):
        return True
    return ".." in posix.parts or ".." in windows.parts


def extract_dicom_zip(zip_path: Path, work_dir: Path, settings: Settings) -> list[Path]:
    """Extracts the DICOM files in `zip_path` into `work_dir` and returns
    their paths. Non-DICOM entries are skipped.

    The archive is rejected outright if any entry name attempts path
    traversal, is a symlink, or is encrypted, and if it holds more files
    or more unpacked bytes than the configured limits. Entry names are not
    used for anything else: files are written as 000001.dcm, 000002.dcm, ...
    """
    max_unpacked = settings.dicom_zip_max_unpacked_mb * 1024 * 1024
    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise UnprocessableScanError("The upload is not a readable .zip archive.") from exc

    extracted: list[Path] = []
    unpacked = 0
    with archive:
        entries = [e for e in archive.infolist() if not e.is_dir()]
        if len(entries) > settings.dicom_zip_max_files:
            raise UnprocessableScanError(
                f"The archive holds {len(entries)} files, more than the "
                f"{settings.dicom_zip_max_files} allowed."
            )
        for entry in entries:
            if _is_unsafe_entry_name(entry.filename):
                raise UnprocessableScanError(
                    "The archive contains an entry with an absolute path or a '..' segment. "
                    "Re-create the zip from the DICOM folder itself."
                )
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise UnprocessableScanError("The archive contains a symbolic link.")
            if entry.flag_bits & 0x1:
                raise UnprocessableScanError("The archive is password-protected.")

        work_dir.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            target = work_dir / f"{len(extracted) + 1:06d}.dcm"
            try:
                with archive.open(entry) as source, target.open("wb") as sink:
                    head = source.read(132)
                    if len(head) < 132 or head[128:132] != b"DICM":
                        is_dicom = False
                    else:
                        is_dicom = True
                        sink.write(head)
                        unpacked += len(head)
                        while chunk := source.read(_COPY_CHUNK):
                            unpacked += len(chunk)
                            if unpacked > max_unpacked:
                                raise UnprocessableScanError(
                                    "The archive unpacks to more than the "
                                    f"{settings.dicom_zip_max_unpacked_mb}MB allowed."
                                )
                            sink.write(chunk)
            except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError) as exc:
                raise UnprocessableScanError(
                    "The archive is corrupt or uses an unsupported compression method."
                ) from exc
            if is_dicom:
                extracted.append(target)
            else:
                target.unlink(missing_ok=True)

    if not extracted:
        raise UnprocessableScanError("The archive contains no DICOM files.")
    return extracted


# --------------------------------------------------------------------------
# 2. De-identification gate
# --------------------------------------------------------------------------


def _has_value(element_value: object) -> bool:
    if element_value is None:
        return False
    if isinstance(element_value, str | bytes):
        return bool(element_value.strip())
    if isinstance(element_value, Sequence):
        return any(_has_value(item) for item in element_value)
    return bool(str(element_value).strip())


def find_identifying_tags(paths: Sequence[Path], tag_names: Sequence[str]) -> list[str]:
    """The names, in the order configured, of the identifying tags that are
    non-empty in any of `paths`. Reads headers only. Returns names only:
    this function never returns, logs or raises a tag's value.
    """
    found: set[str] = set()
    wanted = [*tag_names, "SOPClassUID"]
    for path in paths:
        try:
            dataset = pydicom.dcmread(path, stop_before_pixels=True, specific_tags=wanted)
        except (InvalidDicomError, OSError, ValueError, KeyError) as exc:
            raise UnprocessableScanError("A file in the archive is not readable DICOM.") from exc
        # pydicom reads damaged files leniently and would report "no
        # identifying tags" for one it could not actually parse. Every real
        # DICOM object states its SOP class; a file that does not is
        # treated as unreadable rather than as anonymised.
        file_meta = getattr(dataset, "file_meta", None)
        has_sop_class = "SOPClassUID" in dataset or (
            file_meta is not None and "MediaStorageSOPClassUID" in file_meta
        )
        if not has_sop_class:
            raise UnprocessableScanError("A file in the archive is not readable DICOM.")
        for name in tag_names:
            if name in found:
                continue
            value = dataset.get(name)
            if value is not None and _has_value(value):
                found.add(name)
    return [name for name in tag_names if name in found]


def check_deidentified(paths: Sequence[Path], settings: Settings) -> list[str]:
    """Applies the gate. Returns the identifying tag names that are
    populated (empty if none); raises if the gate is on and any are.
    """
    populated = find_identifying_tags(paths, settings.dicom_identifying_tags)
    if not populated:
        return []
    if settings.require_anonymised_dicom:
        raise IdentifiableDicomError(
            "This DICOM is not anonymised. These header tags still have values and must be "
            f"blanked before upload: {', '.join(populated)}."
        )
    # Names only, never values.
    logger.warning("dicom_identifying_tags_present", tags=populated, gate="disabled")
    return populated


# --------------------------------------------------------------------------
# 3. Conversion
# --------------------------------------------------------------------------


def dcm2niix_path() -> str | None:
    return shutil.which("dcm2niix")


def convert_with_dcm2niix(executable: str, dicom_dir: Path, out_dir: Path, timeout: int) -> Path:
    """Runs dcm2niix. `-b n` writes no JSON sidecar and `-f` fixes the output
    name, since both default to embedding protocol and patient details.
    Its console output is discarded for the same reason.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    command = [
        executable,
        "-b",
        "n",
        "-z",
        "y",
        "-f",
        "converted",
        "-o",
        str(out_dir),
        str(dicom_dir),
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, server-chosen paths
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UnprocessableScanError(f"DICOM conversion timed out. {_NIFTI_HINT}") from exc
    except OSError as exc:
        raise UnprocessableScanError(
            f"DICOM conversion could not be started. {_NIFTI_HINT}"
        ) from exc

    outputs = sorted(out_dir.glob("*.nii.gz")) + sorted(out_dir.glob("*.nii"))
    if completed.returncode != 0 or not outputs:
        raise UnprocessableScanError(f"dcm2niix could not convert this series. {_NIFTI_HINT}")
    if len(outputs) > 1:
        raise UnprocessableScanError(
            f"The archive converts to {len(outputs)} volumes. Upload exactly one T1-weighted "
            "series."
        )
    return outputs[0]


def convert_with_pydicom(paths: Sequence[Path], out_path: Path) -> Path:
    """Fallback converter for a plain single-frame MR series, one slice per
    file. Anything it cannot convert faithfully is refused rather than
    guessed at.
    """
    slices = []
    for path in paths:
        try:
            dataset = pydicom.dcmread(path)
        except (InvalidDicomError, OSError, ValueError) as exc:
            raise UnprocessableScanError("A file in the archive is not readable DICOM.") from exc
        if "PixelData" not in dataset:
            continue  # DICOMDIR, presentation states, reports
        if int(getattr(dataset, "NumberOfFrames", 1) or 1) > 1:
            raise UnprocessableScanError(f"Multi-frame DICOM is not supported here. {_NIFTI_HINT}")
        if not all(
            hasattr(dataset, name)
            for name in ("ImagePositionPatient", "ImageOrientationPatient", "PixelSpacing")
        ):
            raise UnprocessableScanError(
                f"A DICOM slice has no position or orientation. {_NIFTI_HINT}"
            )
        slices.append(dataset)

    if len(slices) < 2:
        raise UnprocessableScanError("The archive does not contain a multi-slice image series.")
    series = {str(getattr(s, "SeriesInstanceUID", "")) for s in slices}
    if len(series) > 1:
        raise UnprocessableScanError(
            f"The archive contains {len(series)} series. Upload exactly one T1-weighted series."
        )

    first = slices[0]
    orientation = np.array([float(v) for v in first.ImageOrientationPatient])
    row_cosine, col_cosine = orientation[:3], orientation[3:]
    normal = np.cross(row_cosine, col_cosine)
    for s in slices:
        same_grid = (s.Rows, s.Columns) == (first.Rows, first.Columns)
        same_orientation = np.allclose(
            [float(v) for v in s.ImageOrientationPatient], orientation, atol=1e-4
        )
        if not (same_grid and same_orientation):
            raise UnprocessableScanError(
                f"The slices do not share one grid and orientation. {_NIFTI_HINT}"
            )

    def position(s: pydicom.Dataset) -> np.ndarray:
        return np.array([float(v) for v in s.ImagePositionPatient])

    slices.sort(key=lambda s: float(np.dot(position(s), normal)))
    locations = np.array([float(np.dot(position(s), normal)) for s in slices])
    gaps = np.diff(locations)
    spacing = float(np.median(gaps))
    if spacing <= 0 or np.any(np.abs(gaps - spacing) > _SLICE_SPACING_REL_TOL * spacing):
        raise UnprocessableScanError(
            f"The series has duplicate, missing or unevenly spaced slices. {_NIFTI_HINT}"
        )

    try:
        frames = []
        for s in slices:
            pixels = s.pixel_array.astype(np.float32)
            slope = float(getattr(s, "RescaleSlope", 1) or 1)
            intercept = float(getattr(s, "RescaleIntercept", 0) or 0)
            frames.append(pixels * slope + intercept)
    except Exception as exc:  # noqa: BLE001 - compressed transfer syntax without a decoder, etc.
        raise UnprocessableScanError(
            f"The pixel data could not be decoded (compressed transfer syntax?). {_NIFTI_HINT}"
        ) from exc

    # pixel_array is [row, column]; NIfTI's first axis runs along the row
    # cosine (increasing column index), so each slice is transposed.
    volume = np.stack([frame.T for frame in frames], axis=-1)

    row_spacing, col_spacing = (float(v) for v in first.PixelSpacing)
    step = (position(slices[-1]) - position(slices[0])) / (len(slices) - 1)
    affine_lps = np.eye(4)
    affine_lps[:3, 0] = row_cosine * col_spacing
    affine_lps[:3, 1] = col_cosine * row_spacing
    affine_lps[:3, 2] = step
    affine_lps[:3, 3] = position(slices[0])
    # DICOM patient coordinates are LPS; NIfTI is RAS.
    affine = np.diag([-1.0, -1.0, 1.0, 1.0]) @ affine_lps

    out_path.parent.mkdir(parents=True, exist_ok=True)
    nib.save(nib.Nifti1Image(volume, affine), str(out_path))
    return out_path


def convert_dicom_zip(zip_path: Path, work_dir: Path, *, settings: Settings | None = None) -> Path:
    """Runs steps 1-3 on an uploaded archive and returns the path of the
    converted NIfTI, which is inside `work_dir`.

    The caller owns `work_dir` and must remove it (see `discard`) once it
    has taken the NIfTI out, on success and on failure alike.
    """
    settings = settings or get_settings()
    dicom_dir = work_dir / "dicom"
    dicom_files = extract_dicom_zip(zip_path, dicom_dir, settings)
    check_deidentified(dicom_files, settings)

    executable = dcm2niix_path()
    if executable is not None:
        return convert_with_dcm2niix(
            executable, dicom_dir, work_dir / "nifti", settings.dicom_conversion_timeout_seconds
        )
    return convert_with_pydicom(dicom_files, work_dir / "nifti" / "converted.nii.gz")


def discard(path: Path) -> None:
    """Removes a file or directory tree. Used to make sure no DICOM outlives
    the request that uploaded it.
    """
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
