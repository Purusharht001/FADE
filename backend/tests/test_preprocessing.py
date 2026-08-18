"""Unit tests for app/services/preprocessing.py: skull-stripping, bias-field
correction, and intensity normalization — including the corrupted-input and
degenerate-contrast edge cases a real clinical pipeline has to reject
cleanly rather than silently mishandle.
"""

import nibabel as nib
import numpy as np
import pytest

from app.core.exceptions import UnprocessableScanError
from app.services import preprocessing
from app.services.synthetic_mri import VOXEL_SIZE_MM, generate_synthetic_scan


@pytest.fixture
def synthetic_volume():
    """A real, well-formed synthetic scan — the baseline "this should just work" fixture."""
    intensity, _labels, _truth = generate_synthetic_scan(0.4, seed=11)
    affine = np.diag([VOXEL_SIZE_MM, VOXEL_SIZE_MM, VOXEL_SIZE_MM, 1.0])
    return intensity, affine


class TestLoadNifti:
    def test_missing_file_raises_unprocessable(self, tmp_path):
        with pytest.raises(UnprocessableScanError, match="not found"):
            preprocessing.load_nifti(tmp_path / "does-not-exist.nii.gz")

    def test_corrupted_file_raises_unprocessable(self, tmp_path):
        """A file with a valid .nii.gz extension but garbage bytes — the
        realistic shape of "corrupted NIfTI header" a bad upload produces.
        """
        bad_file = tmp_path / "corrupt.nii.gz"
        bad_file.write_bytes(b"not a real nifti file, just garbage bytes" * 10)

        with pytest.raises(UnprocessableScanError, match="Could not read NIfTI volume"):
            preprocessing.load_nifti(bad_file)

    def test_empty_file_raises_unprocessable(self, tmp_path):
        empty_file = tmp_path / "empty.nii.gz"
        empty_file.write_bytes(b"")

        with pytest.raises(UnprocessableScanError):
            preprocessing.load_nifti(empty_file)

    def test_valid_file_loads_correctly(self, tmp_path, synthetic_volume):
        intensity, affine = synthetic_volume
        path = tmp_path / "scan.nii.gz"
        img = nib.Nifti1Image(intensity.astype(np.float32), affine)
        nib.save(img, str(path))

        data, loaded_affine = preprocessing.load_nifti(path)
        assert data.shape == intensity.shape
        assert np.allclose(loaded_affine, affine)

    def test_4d_volume_is_rejected(self, tmp_path):
        """A multi-channel/4D NIfTI (e.g. fMRI time series, or a
        multi-contrast export) isn't the single-channel T1w volume this
        pipeline expects.
        """
        path = tmp_path / "4d.nii.gz"
        data = np.random.default_rng(0).uniform(0, 1, size=(10, 10, 10, 5)).astype(np.float32)
        nib.save(nib.Nifti1Image(data, np.eye(4)), str(path))

        with pytest.raises(UnprocessableScanError, match="single-channel 3D"):
            preprocessing.load_nifti(path)

    def test_2d_slice_is_rejected(self, tmp_path):
        path = tmp_path / "2d.nii.gz"
        data = np.random.default_rng(0).uniform(0, 1, size=(64, 64)).astype(np.float32)
        nib.save(nib.Nifti1Image(data, np.eye(4)), str(path))

        with pytest.raises(UnprocessableScanError, match="single-channel 3D"):
            preprocessing.load_nifti(path)


class TestSkullStrip:
    def test_finds_the_brain_in_a_synthetic_volume(self, synthetic_volume):
        intensity, _affine = synthetic_volume
        mask = preprocessing.skull_strip(intensity)
        assert mask.any()
        assert mask.dtype == bool

    def test_all_zero_volume_raises_unprocessable(self):
        data = np.zeros((20, 20, 20), dtype=np.float32)
        with pytest.raises(UnprocessableScanError, match="empty"):
            preprocessing.skull_strip(data)

    def test_uniform_intensity_volume_raises_unprocessable(self):
        """No contrast at all (a flat gray volume) — Otsu has nothing to
        threshold between two classes and must fail loudly, not guess.
        """
        data = np.full((20, 20, 20), 0.5, dtype=np.float32)
        with pytest.raises(UnprocessableScanError):
            preprocessing.skull_strip(data)

    def test_picks_the_largest_connected_component(self):
        """Two disconnected bright blobs (e.g. a real skull-strip artifact,
        or noise) — the larger one should win, not an arbitrary one. The
        large blob is sized well above the compactness floor (a solid cube
        needs to be thick enough that eroding it by a few voxels doesn't
        eat the whole thing — see `_MIN_COMPACTNESS`), and the background
        is a nonzero baseline so Otsu sees genuine bimodal contrast rather
        than a single uniform "nonzero" value.
        """
        data = np.full((40, 40, 40), 0.1, dtype=np.float32)
        data[2:5, 2:5, 2:5] = 1.0  # small blob: 27 voxels
        data[10:30, 10:30, 10:30] = 1.0  # large blob: 8000 voxels, compact
        mask = preprocessing.skull_strip(data)
        assert mask.sum() >= 8000
        assert not mask[3, 3, 3]  # the small blob should be excluded

    def test_fills_interior_holes(self):
        """A hollow shell (e.g. a ventricle-like cavity inside otherwise
        bright tissue) should be filled in as part of the brain mask, not
        left as a hole — real brain masks are solid volumes.
        """
        data = np.full((40, 40, 40), 0.1, dtype=np.float32)
        data[8:32, 8:32, 8:32] = 1.0
        data[18:22, 18:22, 18:22] = 0.1  # interior cavity, back to background level
        mask = preprocessing.skull_strip(data)
        assert mask[20, 20, 20]  # the cavity is filled


class TestCorrectBiasField:
    def test_does_not_spike_at_mask_boundary(self, synthetic_volume):
        """Regression test: a naive gaussian_filter-based correction picks
        up the implicit zero-padding just outside the mask near its
        boundary, producing a corrected-intensity spike right at the edge
        that wrecks downstream min-max normalization. See the function's
        own docstring for the fix (normalized convolution).
        """
        intensity, _affine = synthetic_volume
        mask = preprocessing.skull_strip(intensity)
        corrected = preprocessing.correct_bias_field(intensity, mask)

        from scipy import ndimage as ndi

        eroded = ndi.binary_erosion(mask, iterations=3)
        boundary_shell = mask & ~eroded
        assert corrected[boundary_shell].mean() < corrected[eroded].mean() * 1.5

    def test_preserves_mean_brain_intensity_scale(self, synthetic_volume):
        intensity, _affine = synthetic_volume
        mask = preprocessing.skull_strip(intensity)
        corrected = preprocessing.correct_bias_field(intensity, mask)
        # Rescaling is designed to preserve the mean, not exactly but within a reasonable band.
        assert corrected[mask].mean() == pytest.approx(intensity[mask].mean(), rel=0.2)

    def test_empty_mask_does_not_crash(self):
        data = np.random.default_rng(0).uniform(0, 1, size=(10, 10, 10)).astype(np.float32)
        mask = np.zeros((10, 10, 10), dtype=bool)
        corrected = preprocessing.correct_bias_field(data, mask)
        assert corrected.shape == data.shape


class TestNormalizeIntensity:
    def test_spans_zero_to_one_within_mask(self, synthetic_volume):
        intensity, _affine = synthetic_volume
        mask = preprocessing.skull_strip(intensity)
        corrected = preprocessing.correct_bias_field(intensity, mask)
        normalized = preprocessing.normalize_intensity(corrected, mask)
        assert normalized[mask].min() == pytest.approx(0.0, abs=1e-6)
        assert normalized[mask].max() == pytest.approx(1.0, abs=1e-6)

    def test_empty_mask_raises_unprocessable(self):
        data = np.random.default_rng(0).uniform(0, 1, size=(10, 10, 10)).astype(np.float32)
        mask = np.zeros((10, 10, 10), dtype=bool)
        with pytest.raises(UnprocessableScanError, match="empty"):
            preprocessing.normalize_intensity(data, mask)

    def test_zero_contrast_within_mask_raises_unprocessable(self):
        """An abnormal-contrast scan: every masked voxel reads the same
        intensity (e.g. a saturated/clipped acquisition) — nothing to
        normalize against.
        """
        data = np.full((10, 10, 10), 0.42, dtype=np.float32)
        mask = np.ones((10, 10, 10), dtype=bool)
        with pytest.raises(UnprocessableScanError, match="no intensity contrast"):
            preprocessing.normalize_intensity(data, mask)


class TestPreprocess:
    def test_end_to_end_on_a_synthetic_volume(self, synthetic_volume):
        intensity, affine = synthetic_volume
        result = preprocessing.preprocess(intensity, affine)
        assert result.data.shape == intensity.shape
        assert result.brain_mask.any()
        assert result.voxel_dims_mm == pytest.approx((VOXEL_SIZE_MM,) * 3)
        assert result.voxel_volume_mm3 == pytest.approx(VOXEL_SIZE_MM**3)

    def test_nan_values_are_rejected(self, synthetic_volume):
        """Regression test: NaN/Inf voxels compare False against every
        threshold in skull-strip, so without an explicit check they were
        silently excluded from the brain mask instead of failing the
        scan — corrupted data would read as a slightly smaller, otherwise
        normal-looking brain rather than an outright rejected input.
        """
        intensity, affine = synthetic_volume
        corrupted = intensity.copy()
        corrupted[10:14, 10:14, 10:14] = np.nan
        with pytest.raises(UnprocessableScanError, match="NaN or infinite"):
            preprocessing.preprocess(corrupted, affine)

    def test_infinite_values_are_rejected(self, synthetic_volume):
        intensity, affine = synthetic_volume
        corrupted = intensity.copy()
        corrupted[10, 10, 10] = np.inf
        with pytest.raises(UnprocessableScanError, match="NaN or infinite"):
            preprocessing.preprocess(corrupted, affine)

    def test_non_isotropic_affine_computes_correct_voxel_dims(self, synthetic_volume):
        intensity, _affine = synthetic_volume
        anisotropic_affine = np.diag([1.0, 1.5, 3.0, 1.0])
        result = preprocessing.preprocess(intensity, anisotropic_affine)
        assert result.voxel_dims_mm == pytest.approx((1.0, 1.5, 3.0))
        assert result.voxel_volume_mm3 == pytest.approx(1.0 * 1.5 * 3.0)

    def test_rotated_affine_still_computes_positive_voxel_dims(self, synthetic_volume):
        """A rotation matrix has negative/mixed-sign components; voxel
        *size* must still come out positive (it's derived from the column
        norms, not the raw signed entries).
        """
        intensity, _affine = synthetic_volume
        theta = np.pi / 6
        rotation = np.array(
            [
                [np.cos(theta), -np.sin(theta), 0, 0],
                [np.sin(theta), np.cos(theta), 0, 0],
                [0, 0, 1, 0],
                [0, 0, 0, 1],
            ]
        )
        scaled = rotation @ np.diag([2.0, 2.0, 2.0, 1.0])
        result = preprocessing.preprocess(intensity, scaled)
        assert all(d > 0 for d in result.voxel_dims_mm)
        assert result.voxel_dims_mm == pytest.approx((2.0, 2.0, 2.0), abs=1e-6)


class TestPreprocessFile:
    def test_missing_file_propagates_clean_error(self, tmp_path):
        with pytest.raises(UnprocessableScanError):
            preprocessing.preprocess_file(tmp_path / "missing.nii.gz")

    def test_valid_file_end_to_end(self, tmp_path, synthetic_volume):
        intensity, affine = synthetic_volume
        path = tmp_path / "scan.nii.gz"
        nib.save(nib.Nifti1Image(intensity.astype(np.float32), affine), str(path))

        result = preprocessing.preprocess_file(path)
        assert result.brain_mask.any()
        assert result.data.shape == intensity.shape
