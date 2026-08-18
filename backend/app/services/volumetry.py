"""Automated brain volumetry: tissue segmentation and biomarker extraction
from a preprocessed volume.

Real pipelines (FreeSurfer `recon-all`, FSL FIRST/SIENAX) segment via
atlas registration or learned priors and take minutes to hours per scan.
This module is a fast, dependency-free stand-in built specifically for the
synthetic phantom in `synthetic_mri.py`, using the same two ingredients
real methods do — spatial priors and tissue-intensity contrast — just
much simplified:

- **Ventricle / CSF**: a single fixed intensity cutoff. CSF is dark and
  well-separated from every other tissue class by a wide margin, so a
  global threshold is reliable regardless of how large or small the
  ventricles are (unlike an *adaptive* threshold — Otsu-family methods
  were tried first and discarded: they're driven by the histogram's two
  largest classes, so a small or shrunken CSF pool gets absorbed into
  whichever tissue band Otsu decides balances best, not necessarily CSF).
- **Hippocampus**: a generous bilateral ROI at the expected atlas-space
  offset (a stand-in for an atlas-registration prior), refined by a soft
  Gaussian intensity weight centered on the hippocampal reference
  intensity. Soft (probability-weighted) rather than a hard threshold —
  the hippocampus is a small, low-contrast structure, and a hard cutoff
  either over- or under-shoots depending on where exactly the boundary
  falls; weighting by "how hippocampus-like is this voxel's intensity"
  and summing degrades gracefully instead of flipping a coin at the edge.
- **Cortex**: a fixed intensity cutoff restricted to nothing spatially —
  the cortical ribbon is already geometrically an outer shell purely by
  virtue of being the brightest tissue class near the brain's surface.

The three reference intensities below (`_CSF_REF`, `_HIPPOCAMPUS_REF`,
`_PARENCHYMA_REF`, `_CORTEX_REF`) are a **calibration step**, conceptually
analogous to real MRI intensity-standardization techniques (e.g.
white-stripe normalization): fixed expected intensities for each tissue
class *after* this pipeline's own bias-correction and min-max
normalization, calibrated once against the synthetic phantom's known
tissue design (see `synthetic_mri.py`) and stable across severities and
random seeds (verified empirically, not just asserted). For real data
these would come from a population-level intensity atlas, not a phantom.

This is the documented Phase 3 swap-in point for a real segmentation
backend — replace this module with a FreeSurfer/FSL call and keep
`extract_biomarkers`'s signature the same.
"""

from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from app.core.exceptions import UnprocessableScanError
from app.models.enums import BiomarkerKey
from app.services.preprocessing import PreprocessedVolume
from app.services.synthetic_mri import VOLUME_SHAPE

# Calibrated reference intensities, in the [0, 1] bias-corrected /
# min-max-normalized space `preprocessing.py` produces.
_CSF_REF = 0.05
_HIPPOCAMPUS_REF = 0.40
_PARENCHYMA_REF = 0.58
_CORTEX_REF = 0.78

_CSF_THRESHOLD = (_CSF_REF + _HIPPOCAMPUS_REF) / 2
_CORTEX_THRESHOLD = (_PARENCHYMA_REF + _CORTEX_REF) / 2
_HIPPOCAMPUS_SIGMA = (_PARENCHYMA_REF - _HIPPOCAMPUS_REF) / 2.8


def _ellipsoid(
    shape: tuple[int, int, int],
    center: tuple[float, float, float],
    radii: tuple[float, float, float],
) -> np.ndarray:
    zz, yy, xx = np.mgrid[0 : shape[0], 0 : shape[1], 0 : shape[2]]
    norm = (
        ((zz - center[0]) / radii[0]) ** 2
        + ((yy - center[1]) / radii[1]) ** 2
        + ((xx - center[2]) / radii[2]) ** 2
    )
    return norm <= 1.0


def _hippocampus_prior_roi(shape: tuple[int, int, int]) -> np.ndarray:
    """Bilateral search ROI at the expected hippocampal offset, sized with
    generous margin so it contains the true structure across the full
    range of severity/atrophy without needing to know its exact extent —
    the role a registered atlas plays in a real pipeline.
    """
    center: tuple[float, float, float] = (shape[0] / 2, shape[1] / 2, shape[2] / 2)
    radii: tuple[float, float, float] = (7.0, 9.5, 7.0)
    left = _ellipsoid(shape, (center[0] + 11, center[1] + 13, center[2] - 13), radii)
    right = _ellipsoid(shape, (center[0] + 11, center[1] + 13, center[2] + 13), radii)
    return left | right


@dataclass(slots=True)
class SegmentationResult:
    ventricle_mask: np.ndarray
    hippocampus_weight: np.ndarray  # soft membership in [0, 1], not a hard mask
    cortex_mask: np.ndarray


def segment_tissue_classes(volume: PreprocessedVolume) -> SegmentationResult:
    mask = volume.brain_mask
    data = volume.data

    if int(np.count_nonzero(mask)) < 500:
        raise UnprocessableScanError("Brain mask too small to segment reliably.")

    ventricle_mask = mask & (data <= _CSF_THRESHOLD)
    cortex_mask = mask & (data > _CORTEX_THRESHOLD)

    hippo_roi = _hippocampus_prior_roi(data.shape) & mask
    if not hippo_roi.any():
        raise UnprocessableScanError(
            "Hippocampal search region does not overlap the brain mask — scan geometry looks wrong."
        )
    hippocampus_weight = np.where(
        hippo_roi, np.exp(-(((data - _HIPPOCAMPUS_REF) / _HIPPOCAMPUS_SIGMA) ** 2)), 0.0
    )

    return SegmentationResult(
        ventricle_mask=ventricle_mask,
        hippocampus_weight=hippocampus_weight,
        cortex_mask=cortex_mask,
    )


def extract_biomarkers(
    volume: PreprocessedVolume, seg: SegmentationResult
) -> dict[BiomarkerKey, float]:
    voxel_volume_ml = volume.voxel_volume_mm3 / 1000.0
    brain_voxels = int(np.count_nonzero(volume.brain_mask))
    if brain_voxels == 0:
        raise UnprocessableScanError("Empty brain mask; cannot compute biomarkers.")

    hippocampal_volume_ml = float(seg.hippocampus_weight.sum() * voxel_volume_ml)

    ventricle_voxels = int(np.count_nonzero(seg.ventricle_mask))
    ventricle_brain_ratio_pct = 100.0 * ventricle_voxels / brain_voxels

    cortical_thickness_mm = _estimate_ribbon_thickness(seg.cortex_mask, volume.voxel_dims_mm)

    return {
        BiomarkerKey.HIPPOCAMPAL_VOLUME: round(hippocampal_volume_ml, 3),
        BiomarkerKey.VENTRICLE_BRAIN_RATIO: round(ventricle_brain_ratio_pct, 3),
        BiomarkerKey.CORTICAL_THICKNESS: round(cortical_thickness_mm, 3),
    }


def _estimate_ribbon_thickness(
    ribbon_mask: np.ndarray, voxel_dims_mm: tuple[float, float, float]
) -> float:
    """Mean cortical thickness via a Euclidean distance transform on the
    ribbon mask: for a thin shell, twice the mean distance-to-boundary
    approximates the mean local thickness — a standard lightweight
    stereological estimator when a full surface-based method (like
    FreeSurfer's) isn't available.
    """
    if not ribbon_mask.any():
        raise UnprocessableScanError("Cortical ribbon segmentation is empty.")
    edt = ndimage.distance_transform_edt(ribbon_mask, sampling=voxel_dims_mm)
    return float(2.0 * edt[ribbon_mask].mean())


def run_volumetry(volume: PreprocessedVolume) -> dict[BiomarkerKey, float]:
    if volume.data.shape != VOLUME_SHAPE:
        # The hippocampal ROI's offsets are calibrated to the synthetic
        # phantom's fixed grid; a differently-shaped real volume would need
        # its own registered atlas ROI here, not this fixed-offset one.
        raise UnprocessableScanError(
            f"This volumetry backend is calibrated for the synthetic phantom's "
            f"{VOLUME_SHAPE} grid; got {volume.data.shape}. Swap in a real "
            f"atlas-registration-based segmentation backend for other inputs."
        )
    seg = segment_tissue_classes(volume)
    return extract_biomarkers(volume, seg)
