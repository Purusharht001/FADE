"""Receiving a scan file safely.

Three jobs, in the order an upload goes through them:

1. `store_upload` streams the request body to a server-named file,
   stopping at the size limit.
2. `detect_kind` decides what the file is from its leading bytes. The
   client's filename and content type are never consulted.
3. `sanitize_nifti` rewrites a NIfTI volume into the form the rest of the
   pipeline accepts: 3-D, sane voxel size, and a header that carries
   nothing but geometry.

Files are only ever written under names generated here (a UUID), so
nothing the client sends can influence a path on disk.
"""

import gzip
import struct
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import nibabel as nib
import numpy as np
from fastapi import UploadFile

from app.core.config import Settings, get_settings
from app.core.exceptions import UnprocessableScanError

_CHUNK = 1024 * 1024
# Bytes needed to recognise a NIfTI-1 header (magic at offset 344).
_NIFTI_SNIFF_BYTES = 352


class FileKind(StrEnum):
    NIFTI = "nifti"  # single-file NIfTI-1 or NIfTI-2, uncompressed
    NIFTI_GZ = "nifti_gz"
    NIFTI_PAIR = "nifti_pair"  # .hdr half of an .hdr/.img pair
    ZIP = "zip"
    DICOM = "dicom"
    GZIP = "gzip"  # gzip of something that is not NIfTI
    UNKNOWN = "unknown"


def new_upload_name(suffix: str) -> str:
    """A server-generated file name. The only names files are stored under."""
    return f"{uuid.uuid4().hex}{suffix}"


def incoming_dir(settings: Settings | None = None) -> Path:
    """Scratch area for uploads that have not been validated yet."""
    settings = settings or get_settings()
    path = Path(settings.upload_dir) / "incoming"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def store_upload(file: UploadFile, dest: Path, max_bytes: int, limit_mb: int) -> int:
    """Streams `file` to `dest`, refusing to write more than `max_bytes`.
    A file over the limit is removed, not left truncated. Returns the size.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - idempotent mkdir
    size = 0
    try:
        with dest.open("wb") as handle:  # noqa: ASYNC230 - chunked local write, bounded by max_bytes
            while chunk := await file.read(_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise UnprocessableScanError(f"File exceeds the {limit_mb}MB upload limit.")
                handle.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)  # noqa: ASYNC240 - one-shot local unlink on the error path
        raise
    if size == 0:
        dest.unlink(missing_ok=True)  # noqa: ASYNC240 - one-shot local unlink on the error path
        raise UnprocessableScanError("The uploaded file is empty.")
    return size


def _nifti_kind(head: bytes) -> FileKind | None:
    """NIfTI-1 keeps `sizeof_hdr` = 348 at offset 0 and its magic at 344;
    NIfTI-2 keeps 540 at offset 0 and its magic at 4. Either byte order.
    """
    if len(head) >= 8:
        (little,) = struct.unpack("<i", head[:4])
        (big,) = struct.unpack(">i", head[:4])
        if 540 in (little, big) and head[4:8] in (b"n+2\x00", b"ni2\x00"):
            return FileKind.NIFTI if head[4:8] == b"n+2\x00" else FileKind.NIFTI_PAIR
    if len(head) >= 348:
        (little,) = struct.unpack("<i", head[:4])
        (big,) = struct.unpack(">i", head[:4])
        if 348 in (little, big):
            magic = head[344:348]
            if magic == b"n+1\x00":
                return FileKind.NIFTI
            if magic == b"ni1\x00":
                return FileKind.NIFTI_PAIR
    return None


def detect_kind(path: Path) -> FileKind:
    """What a stored file is, judged from its content alone."""
    with path.open("rb") as handle:
        head = handle.read(_NIFTI_SNIFF_BYTES)

    if head[:2] == b"\x1f\x8b":
        try:
            with gzip.open(path, "rb") as handle:
                inner = handle.read(_NIFTI_SNIFF_BYTES)
        except (OSError, EOFError):
            return FileKind.GZIP
        kind = _nifti_kind(inner)
        if kind == FileKind.NIFTI:
            return FileKind.NIFTI_GZ
        return kind or FileKind.GZIP

    kind = _nifti_kind(head)
    if kind is not None:
        return kind
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return FileKind.ZIP
    if len(head) >= 132 and head[128:132] == b"DICM":
        return FileKind.DICOM
    return FileKind.UNKNOWN


@dataclass(frozen=True, slots=True)
class NiftiInfo:
    shape: tuple[int, int, int]
    voxel_dims_mm: tuple[float, float, float]
    # True if a trailing singleton dimension was removed.
    squeezed: bool


def sanitize_nifti(src: Path, dest: Path, *, settings: Settings | None = None) -> NiftiInfo:
    """Rewrites the NIfTI at `src` to `dest` (always .nii.gz) as a plain 3-D
    volume with a clean header.

    * A trailing singleton 4th dimension is squeezed: OASIS volumes
      converted from Analyze have shape (256, 256, 128, 1), which makes
      FastSurfer fail.
    * Anything that is not 3-D after that is rejected.
    * The voxel size must be finite, positive and inside the configured
      sanity range.
    * The output header is built fresh from the geometry, so the free-text
      fields `descrip`, `aux_file`, `db_name` and `intent_name` are blank and
      no header extension survives. Converters routinely write a patient
      name, a study description or a full DICOM dump into those.

    `src` must be named with the suffix matching its kind (.nii / .nii.gz),
    since nibabel picks its reader from the name.
    """
    settings = settings or get_settings()
    try:
        img = nib.load(str(src))
        if not isinstance(img, nib.Nifti1Image):  # Nifti2Image is a subclass
            raise TypeError("not a NIfTI image")
        shape = tuple(int(d) for d in img.shape)
        header = img.header
        zooms = tuple(float(z) for z in header.get_zooms())
        spatial_unit = header.get_xyzt_units()[0]
    except Exception as exc:  # noqa: BLE001 - any nibabel/format error is the same answer
        raise UnprocessableScanError("The file could not be read as a NIfTI volume.") from exc

    squeezed = False
    while len(shape) > 3 and shape[-1] == 1:
        shape = shape[:-1]
        zooms = zooms[: len(shape)]
        squeezed = True
    if len(shape) != 3:
        raise UnprocessableScanError(
            f"Expected a single 3-D volume; this file has {len(shape)} dimensions "
            f"{list(shape)}. Upload one T1-weighted volume, not a time series."
        )
    if any(d < 2 for d in shape):
        raise UnprocessableScanError(f"Volume dimensions {list(shape)} are not a 3-D image.")
    n_voxels = int(np.prod(shape, dtype=np.int64))
    if n_voxels > settings.nifti_max_voxels:
        raise UnprocessableScanError(
            f"Volume has {n_voxels} voxels, more than the {settings.nifti_max_voxels} allowed."
        )

    if spatial_unit not in ("mm", "unknown"):
        raise UnprocessableScanError(
            f"The NIfTI header declares spatial units of {spatial_unit}; millimetres are required."
        )
    lo, hi = settings.nifti_voxel_mm_range
    voxel_dims = zooms[:3]
    if len(voxel_dims) != 3 or not all(np.isfinite(v) and lo <= v <= hi for v in voxel_dims):
        raise UnprocessableScanError(
            f"Voxel size {[round(v, 4) for v in voxel_dims]} mm is outside the accepted "
            f"{lo:g}-{hi:g} mm range; the header geometry looks wrong."
        )

    try:
        data = np.asanyarray(img.dataobj).reshape(shape)
        affine = np.asarray(img.affine, dtype=np.float64)
    except Exception as exc:  # noqa: BLE001 - truncated or corrupt voxel data
        raise UnprocessableScanError("The NIfTI voxel data could not be read.") from exc
    if not np.isfinite(affine).all():
        raise UnprocessableScanError("The NIfTI header has a non-finite orientation matrix.")
    # The voxel size the rest of the pipeline will actually use comes from
    # the orientation matrix, which a broken header can leave inconsistent
    # with the pixdim fields checked above.
    affine_dims = np.sqrt((affine[:3, :3] ** 2).sum(axis=0))
    if not all(lo <= float(v) <= hi for v in affine_dims):
        raise UnprocessableScanError(
            f"The NIfTI orientation matrix implies a voxel size of "
            f"{[round(float(v), 4) for v in affine_dims]} mm, outside the accepted "
            f"{lo:g}-{hi:g} mm range."
        )
    if data.dtype == np.float64:
        data = data.astype(np.float32)
    elif data.dtype.kind not in ("i", "u", "f"):
        raise UnprocessableScanError("The NIfTI volume is not a plain numeric image.")

    # A new header from geometry alone: a whitelist, not a blacklist.
    clean = nib.Nifti1Image(data, affine)
    clean.header.set_xyzt_units("mm")
    for text_field in ("descrip", "aux_file", "db_name", "intent_name"):
        clean.header[text_field] = b""
    clean.header.extensions.clear()

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        nib.save(clean, str(dest))
    except BaseException:
        dest.unlink(missing_ok=True)  # never leave a half-written volume behind
        raise
    return NiftiInfo(
        shape=(shape[0], shape[1], shape[2]),
        voxel_dims_mm=(float(affine_dims[0]), float(affine_dims[1]), float(affine_dims[2])),
        squeezed=squeezed,
    )
