"""Builds tiny synthetic DICOM series for tests.

Nothing here is, or is derived from, a real scan: the pixels are a
deterministic ramp and every header value is supplied by the test. Series
are written to a pytest tmp_path, never committed.
"""

import io
import zipfile
from pathlib import Path

import numpy as np
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid

# The eight tags the de-identification gate checks, with values that are
# obviously fake and easy to search a response or a log for.
IDENTIFYING_VALUES = {
    "PatientName": "ZZTEST^LEAKED",
    "PatientID": "MRN-LEAK-0001",
    "PatientBirthDate": "19400101",
    "OtherPatientIDs": "ALT-LEAK-7",
    "InstitutionName": "Leaktown General",
    "ReferringPhysicianName": "DR^LEAKY",
    "AccessionNumber": "ACC-LEAK-9",
    "StudyDate": "20260101",
}

SHAPE = (6, 5, 4)  # (columns, rows, slices): deliberately all different


def synthetic_volume(shape: tuple[int, int, int] = SHAPE) -> np.ndarray:
    """A volume indexed [column, row, slice] whose value encodes its own
    position, so a transposed or reordered axis shows up as a wrong number.
    """
    cols, rows, slices = shape
    i, j, k = np.meshgrid(np.arange(cols), np.arange(rows), np.arange(slices), indexing="ij")
    return (i + 10 * j + 100 * k).astype(np.uint16)


def make_slice(
    path: Path,
    pixels_row_col: np.ndarray,
    *,
    position: tuple[float, float, float],
    series_uid: str,
    orientation: tuple[float, ...] = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0),
    pixel_spacing: tuple[float, float] = (1.0, 1.0),
    identifying: dict[str, str] | None = None,
    rescale: tuple[float, float] | None = None,
    extra: dict | None = None,
) -> Path:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = MRImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\x00" * 128)
    ds.SOPClassUID = MRImageStorage
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.Modality = "MR"
    ds.SeriesInstanceUID = series_uid
    ds.StudyInstanceUID = generate_uid()
    rows, cols = pixels_row_col.shape
    ds.Rows, ds.Columns = rows, cols
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = 16, 16, 15
    ds.PixelRepresentation = 0
    ds.PixelSpacing = [pixel_spacing[0], pixel_spacing[1]]
    ds.ImageOrientationPatient = list(orientation)
    ds.ImagePositionPatient = list(position)
    if rescale is not None:
        ds.RescaleSlope, ds.RescaleIntercept = rescale

    # Anonymised by default: every identifying tag present but empty.
    for name in IDENTIFYING_VALUES:
        setattr(ds, name, "")
    for name, value in (identifying or {}).items():
        setattr(ds, name, value)
    for name, value in (extra or {}).items():
        setattr(ds, name, value)

    ds.PixelData = np.ascontiguousarray(pixels_row_col.astype(np.uint16)).tobytes()
    ds.save_as(str(path), enforce_file_format=True)
    return path


def make_series(
    directory: Path,
    *,
    volume: np.ndarray | None = None,
    slice_spacing: float = 2.0,
    pixel_spacing: tuple[float, float] = (1.0, 1.0),
    origin: tuple[float, float, float] = (-10.0, -20.0, 30.0),
    identifying: dict[str, str] | None = None,
    series_uid: str | None = None,
    skip_slices: tuple[int, ...] = (),
    rescale: tuple[float, float] | None = None,
    name_prefix: str = "IM",
) -> list[Path]:
    """Writes an axial series, one file per slice. Files are named in
    reverse slice order so a converter that trusts filename order gets the
    volume upside down.
    """
    directory.mkdir(parents=True, exist_ok=True)
    volume = synthetic_volume() if volume is None else volume
    series_uid = series_uid or generate_uid()
    n_slices = volume.shape[2]
    paths = []
    for k in range(n_slices):
        if k in skip_slices:
            continue
        paths.append(
            make_slice(
                directory / f"{name_prefix}{n_slices - k:04d}.dcm",
                volume[:, :, k].T,  # DICOM stores [row, column]
                position=(origin[0], origin[1], origin[2] + k * slice_spacing),
                series_uid=series_uid,
                pixel_spacing=pixel_spacing,
                identifying=identifying,
                rescale=rescale,
            )
        )
    return paths


def zip_files(files: dict[str, bytes]) -> bytes:
    """A zip archive of {entry name: content}, as bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def zip_series(paths: list[Path], *, folder: str = "series") -> bytes:
    return zip_files({f"{folder}/{p.name}": p.read_bytes() for p in paths})


def write_zip(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path
