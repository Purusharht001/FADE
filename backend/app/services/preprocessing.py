"""MRI preprocessing: skull-strip, bias field correction, intensity
normalization.

This operates on real NIfTI files via nibabel/numpy/scipy/scikit-image —
no external neuroimaging toolkit required for the synthetic pipeline this
project currently runs end-to-end on. It is intentionally the swap-in
point for real tools once Phase 1/2 data access lands: replace
`skull_strip` with an FSL BET / HD-BET call, replace `correct_bias_field`
with N4ITK (via SimpleITK or ANTs), and spatial normalization (registration
to an MNI template) would be added here as a fourth step once multi-subject
data makes template registration meaningful.
"""

from dataclasses import dataclass
from pathlib import Path

import nibabel as nib
import numpy as np
from scipy import ndimage
from skimage.filters import threshold_otsu

from app.core.exceptions import UnprocessableScanError


@dataclass(slots=True)
class PreprocessedVolume:
    data: np.ndarray  # bias-corrected, intensity-normalized, masked to brain
    brain_mask: np.ndarray  # boolean array, same shape as data
    affine: np.ndarray
    voxel_dims_mm: tuple[float, float, float]
    voxel_volume_mm3: float


def load_nifti(path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    path = Path(path)
    if not path.exists():
        raise UnprocessableScanError(f"Scan file not found: {path}")
    try:
        img = nib.nifti1.Nifti1Image.from_filename(str(path))
        data = np.asarray(img.get_fdata(), dtype=np.float32)
        affine = np.asarray(img.affine, dtype=np.float64)
    except Exception as exc:  # noqa: BLE001 - any nibabel/format error becomes a domain error
        raise UnprocessableScanError(f"Could not read NIfTI volume: {exc}") from exc

    if data.ndim != 3 or data.size == 0:
        raise UnprocessableScanError("Expected a single-channel 3D volume.")
    return data, affine


# A genuine brain mask is a solid, roughly-ellipsoidal blob: eroding it by a
# few voxels shrinks it proportionally and it retains most of its volume.
# Pure noise thresholded at ~50% occupancy sits above 3D site percolation
# threshold (~0.31) and forms one giant *connected* component too — so the
# component-count/size checks above don't catch it — but that component is a
# sparse filamentary web, not a solid blob, and erosion collapses it almost
# to nothing. Verified empirically across this project's synthetic phantom
# (0.719 retained, stable across every severity/seed) versus pure noise
# (0.0 retained) — see tests/test_preprocessing.py.
_COMPACTNESS_EROSION_ITERATIONS = 3
_MIN_COMPACTNESS = 0.3


def skull_strip(data: np.ndarray) -> np.ndarray:
    """Otsu-threshold foreground extraction + largest-connected-component +
    hole filling. A lightweight stand-in for FSL BET/HD-BET, adequate for
    the synthetic volumes this pipeline currently runs on.
    """
    nonzero = data[data > 0]
    if nonzero.size == 0:
        raise UnprocessableScanError("Volume is empty after intensity thresholding.")

    try:
        threshold = threshold_otsu(nonzero)
    except ValueError as exc:
        raise UnprocessableScanError(f"Otsu thresholding failed: {exc}") from exc

    rough_mask = data > threshold
    labeled, n_components = ndimage.label(rough_mask)
    if n_components == 0:
        raise UnprocessableScanError("Skull-strip found no foreground tissue.")

    sizes = ndimage.sum(rough_mask, labeled, index=range(1, n_components + 1))
    largest_label = int(np.argmax(sizes)) + 1
    mask = labeled == largest_label
    mask = ndimage.binary_fill_holes(mask)

    eroded = ndimage.binary_erosion(mask, iterations=_COMPACTNESS_EROSION_ITERATIONS)
    compactness = eroded.sum() / mask.sum()
    if compactness < _MIN_COMPACTNESS:
        raise UnprocessableScanError(
            "Segmented region is not brain-shaped (too sparse/filamentary) — the scan is likely "
            "noise, an acquisition artifact, or not a brain volume."
        )

    return mask


def correct_bias_field(data: np.ndarray, mask: np.ndarray, *, sigma: float = 8.0) -> np.ndarray:
    """Homomorphic bias-field correction: divide by a heavily-smoothed
    version of the image (a low-pass estimate of the multiplicative bias
    field), restricted to the brain mask. A lightweight stand-in for N4ITK.

    Uses normalized convolution (smooth the masked data and the mask itself,
    then divide) rather than a plain `gaussian_filter` on zero-padded data —
    a plain filter's kernel picks up the implicit zeros just outside the
    mask near the boundary, which drags the smoothed estimate down right at
    the edge and spikes `data / smooth` there. Normalizing by the smoothed
    mask cancels that edge bias.
    """
    mask_f = mask.astype(np.float32)
    smooth_num = ndimage.gaussian_filter(np.where(mask, data, 0.0), sigma=sigma)
    smooth_den = ndimage.gaussian_filter(mask_f, sigma=sigma)
    smooth = np.divide(
        smooth_num, smooth_den, out=np.ones_like(smooth_num), where=smooth_den > 1e-3
    )
    corrected = np.where(mask, data / smooth, 0.0)
    # Rescale so mean brain intensity is preserved post-correction.
    if mask.any():
        corrected *= data[mask].mean() / max(corrected[mask].mean(), 1e-6)
    return corrected.astype(np.float32)


def normalize_intensity(data: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Min-max normalize intensities within the brain mask to [0, 1]."""
    brain_values = data[mask]
    if brain_values.size == 0:
        raise UnprocessableScanError("Brain mask is empty; cannot normalize intensity.")
    lo, hi = float(brain_values.min()), float(brain_values.max())
    if hi - lo < 1e-6:
        raise UnprocessableScanError("Brain volume has no intensity contrast.")
    normalized = np.where(mask, (data - lo) / (hi - lo), 0.0)
    return normalized.astype(np.float32)


def preprocess(data: np.ndarray, affine: np.ndarray) -> PreprocessedVolume:
    if not np.isfinite(data).all():
        # NaN/Inf voxels compare False against every threshold, so without
        # this check they'd silently fall outside the skull-strip mask
        # instead of failing the scan — a corrupted region would just be
        # quietly excluded from the brain volume rather than rejected,
        # which is exactly the wrong failure mode for a diagnostic
        # pipeline: it should never look like a normal-sized, slightly
        # smaller brain when the truth is "this scan is corrupted".
        raise UnprocessableScanError(
            "Volume contains NaN or infinite values — the scan file appears corrupted."
        )
    mask = skull_strip(data)
    corrected = correct_bias_field(data, mask)
    normalized = normalize_intensity(corrected, mask)

    voxel_dims = np.sqrt((affine[:3, :3] ** 2).sum(axis=0))
    voxel_volume_mm3 = float(np.prod(voxel_dims))

    return PreprocessedVolume(
        data=normalized,
        brain_mask=mask,
        affine=affine,
        voxel_dims_mm=(float(voxel_dims[0]), float(voxel_dims[1]), float(voxel_dims[2])),
        voxel_volume_mm3=voxel_volume_mm3,
    )


def preprocess_file(path: str | Path) -> PreprocessedVolume:
    data, affine = load_nifti(path)
    return preprocess(data, affine)
