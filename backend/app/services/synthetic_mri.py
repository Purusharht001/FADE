"""Synthetic T1-weighted MRI generator.

There is no real OASIS/ADNI/clinic imaging data available in this
environment (Phase 1 dataset access and Phase 1 clinic consent are still
open items — see PHASES.md). To keep the preprocessing → volumetry → fuzzy
inference pipeline genuinely *runnable end-to-end* rather than just typed
out, this module synthesizes a parametric NIfTI volume with a labeled brain,
hippocampus, ventricles, and cortex ribbon, driven by a single 0-1
"severity" knob — shrinking the hippocampus and cortex and enlarging the
ventricles as severity increases, plus a synthetic bias field and noise so
the preprocessing step has something real to correct.

This is explicitly a placeholder for real MRI input. `preprocessing.py` and
`volumetry.py` consume the *intensity image* it produces, not the ground-
truth label array, so the rest of the pipeline exercises the same code path
it would on a real scan.
"""

from dataclasses import dataclass
from pathlib import Path

import nibabel as nib
import numpy as np

VOXEL_SIZE_MM = 2.0  # isotropic
VOLUME_SHAPE = (64, 64, 64)

LABEL_BACKGROUND = 0
LABEL_BRAIN = 1
LABEL_HIPPOCAMPUS = 2
LABEL_VENTRICLE = 3
LABEL_CORTEX = 4

# Approximate signal intensity per tissue class on a T1w scan (arbitrary units).
# Kept well-separated (rather than the ~10% gaps real tissue contrast would
# give) so a lightweight global-threshold segmenter — the whole point of
# this synthetic phantom is to exercise the pipeline without requiring
# FreeSurfer/FSL — can actually resolve hippocampus from generic parenchyma;
# see volumetry.py's docstring for the real-segmentation swap-in point.
_INTENSITY_BY_LABEL = {
    LABEL_BACKGROUND: 0.02,
    LABEL_BRAIN: 0.58,
    LABEL_HIPPOCAMPUS: 0.42,
    LABEL_VENTRICLE: 0.12,  # CSF is dark on T1
    LABEL_CORTEX: 0.78,
}


@dataclass(frozen=True, slots=True)
class SyntheticGroundTruth:
    """The parameters used to generate the volume — useful for tests that
    check the pipeline recovers something close to what was synthesized.
    """

    severity: float
    hippocampal_volume_ml: float
    ventricle_brain_ratio_pct: float
    cortical_thickness_mm: float


def _ellipsoid_mask(
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


def generate_labels(
    severity: float, *, rng: np.random.Generator | None = None
) -> tuple[np.ndarray, SyntheticGroundTruth]:
    """Builds the ground-truth label volume for a given severity in [0, 1]."""
    severity = float(np.clip(severity, 0.0, 1.0))
    rng = rng or np.random.default_rng()
    shape = VOLUME_SHAPE
    center: tuple[float, float, float] = (shape[0] / 2, shape[1] / 2, shape[2] / 2)

    brain_radii: tuple[float, float, float] = (24.0, 27.0, 23.0)
    brain_mask = _ellipsoid_mask(shape, center, brain_radii)

    # Cortex ribbon = brain shell, thinning with severity.
    shell_thickness = 4.5 - 1.6 * severity
    inner_radii: tuple[float, float, float] = (
        brain_radii[0] - shell_thickness,
        brain_radii[1] - shell_thickness,
        brain_radii[2] - shell_thickness,
    )
    inner_mask = _ellipsoid_mask(shape, center, inner_radii)
    cortex_mask = brain_mask & ~inner_mask

    # Ventricles: paired blobs near the centroid, enlarging with severity.
    # Radius capped so the ventricle never grows into the hippocampus's
    # offset position (checked against the hippocampus geometry below).
    ventricle_radius = 2.5 + 5.0 * severity
    ventricle_mask = _ellipsoid_mask(
        shape,
        (center[0], center[1] - 3, center[2]),
        (ventricle_radius * 0.7, ventricle_radius, ventricle_radius * 0.6),
    )

    # Hippocampus: bilateral blobs, shrinking with severity. Offset well out
    # toward the temporal lobes (and away from the ventricle's max reach)
    # so the two structures never spatially overlap even at extreme severity.
    hippo_radius = 4.2 - 2.0 * severity
    left = _ellipsoid_mask(
        shape,
        (center[0] + 11, center[1] + 13, center[2] - 13),
        (hippo_radius, hippo_radius * 1.4, hippo_radius),
    )
    right = _ellipsoid_mask(
        shape,
        (center[0] + 11, center[1] + 13, center[2] + 13),
        (hippo_radius, hippo_radius * 1.4, hippo_radius),
    )
    hippocampus_mask = left | right

    labels = np.full(shape, LABEL_BACKGROUND, dtype=np.uint8)
    labels[brain_mask] = LABEL_BRAIN
    labels[cortex_mask] = LABEL_CORTEX
    labels[hippocampus_mask & brain_mask] = LABEL_HIPPOCAMPUS
    labels[ventricle_mask & brain_mask] = LABEL_VENTRICLE

    voxel_volume_ml = (VOXEL_SIZE_MM**3) / 1000.0
    hippocampal_volume_ml = float(np.count_nonzero(labels == LABEL_HIPPOCAMPUS) * voxel_volume_ml)
    brain_voxels = np.count_nonzero(labels != LABEL_BACKGROUND)
    ventricle_voxels = np.count_nonzero(labels == LABEL_VENTRICLE)
    ventricle_brain_ratio_pct = (
        float(100.0 * ventricle_voxels / brain_voxels) if brain_voxels else 0.0
    )
    cortical_thickness_mm = shell_thickness * VOXEL_SIZE_MM

    truth = SyntheticGroundTruth(
        severity=severity,
        hippocampal_volume_ml=round(hippocampal_volume_ml, 3),
        ventricle_brain_ratio_pct=round(ventricle_brain_ratio_pct, 3),
        cortical_thickness_mm=round(cortical_thickness_mm, 3),
    )
    return labels, truth


def labels_to_intensity_volume(
    labels: np.ndarray, *, rng: np.random.Generator | None = None
) -> np.ndarray:
    """Renders a plausible T1-weighted intensity volume from a label map:
    per-tissue base intensity + Gaussian noise + a smooth multiplicative
    bias field, so `preprocessing.py` has real bias/noise to correct.
    """
    rng = rng or np.random.default_rng()
    intensity = np.zeros(labels.shape, dtype=np.float32)
    for label_value, base_intensity in _INTENSITY_BY_LABEL.items():
        mask = labels == label_value
        intensity[mask] = base_intensity

    intensity += rng.normal(0.0, 0.015, size=labels.shape).astype(np.float32)

    # Smooth low-frequency multiplicative bias field (stand-in for MRI coil inhomogeneity).
    coarse = rng.normal(1.0, 0.10, size=(6, 6, 6)).astype(np.float32)
    from scipy.ndimage import zoom

    bias_field = zoom(coarse, np.array(labels.shape) / np.array(coarse.shape), order=3)
    intensity *= bias_field

    return np.clip(intensity, 0.0, None)


def generate_synthetic_scan(
    severity: float, *, seed: int | None = None
) -> tuple[np.ndarray, np.ndarray, SyntheticGroundTruth]:
    """Returns (intensity_volume, ground_truth_labels, ground_truth_metrics)."""
    rng = np.random.default_rng(seed)
    labels, truth = generate_labels(severity, rng=rng)
    intensity = labels_to_intensity_volume(labels, rng=rng)
    return intensity, labels, truth


def save_nifti(volume: np.ndarray, path: str | Path, *, dtype: type = np.float32) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    affine = np.diag([VOXEL_SIZE_MM, VOXEL_SIZE_MM, VOXEL_SIZE_MM, 1.0])
    img = nib.Nifti1Image(volume.astype(dtype), affine)
    nib.save(img, str(path))
    return path
