import numpy as np
import pytest

from app.core.exceptions import UnprocessableScanError
from app.models.enums import BiomarkerKey, Stage
from app.services import preprocessing, synthetic_mri, volumetry
from app.services.pipeline import run_pipeline_synthetic

HV = BiomarkerKey.HIPPOCAMPAL_VOLUME
VBR = BiomarkerKey.VENTRICLE_BRAIN_RATIO
CT = BiomarkerKey.CORTICAL_THICKNESS


class TestSyntheticGenerator:
    def test_severity_is_clamped_into_unit_range(self):
        _labels, truth = synthetic_mri.generate_labels(1.5)
        assert truth.severity == 1.0
        _labels, truth = synthetic_mri.generate_labels(-1.0)
        assert truth.severity == 0.0

    def test_higher_severity_shrinks_hippocampus_and_grows_ventricles(self):
        _labels_lo, truth_lo = synthetic_mri.generate_labels(0.0, rng=np.random.default_rng(1))
        _labels_hi, truth_hi = synthetic_mri.generate_labels(1.0, rng=np.random.default_rng(1))
        assert truth_hi.hippocampal_volume_ml < truth_lo.hippocampal_volume_ml
        assert truth_hi.ventricle_brain_ratio_pct > truth_lo.ventricle_brain_ratio_pct
        assert truth_hi.cortical_thickness_mm < truth_lo.cortical_thickness_mm


class TestPreprocessing:
    def test_bias_correction_does_not_spike_at_mask_boundary(self):
        """Regression test for a real bug: a naive gaussian_filter-based bias
        correction picks up the implicit zero-padding just outside the mask
        near its boundary, producing a corrected-intensity spike right at
        the edge that wrecks downstream min-max normalization.
        """
        intensity, _labels, _truth = synthetic_mri.generate_synthetic_scan(0.5, seed=42)
        mask = preprocessing.skull_strip(intensity)
        corrected = preprocessing.correct_bias_field(intensity, mask)

        from scipy import ndimage

        eroded = ndimage.binary_erosion(mask, iterations=3)
        boundary_shell = mask & ~eroded
        interior = eroded

        # The boundary shell must not read dramatically brighter than the
        # interior — that was exactly the symptom of the edge-bias bug.
        assert corrected[boundary_shell].mean() < corrected[interior].mean() * 1.5

    def test_skull_strip_recovers_roughly_the_true_brain_volume(self):
        intensity, labels, _truth = synthetic_mri.generate_synthetic_scan(0.5, seed=42)
        mask = preprocessing.skull_strip(intensity)
        true_brain = labels != synthetic_mri.LABEL_BACKGROUND
        dice = 2 * np.count_nonzero(mask & true_brain) / (mask.sum() + true_brain.sum())
        assert dice > 0.98

    def test_normalize_intensity_spans_zero_to_one(self):
        intensity, _labels, _truth = synthetic_mri.generate_synthetic_scan(0.5, seed=42)
        mask = preprocessing.skull_strip(intensity)
        corrected = preprocessing.correct_bias_field(intensity, mask)
        normalized = preprocessing.normalize_intensity(corrected, mask)
        assert normalized[mask].min() == pytest.approx(0.0, abs=1e-6)
        assert normalized[mask].max() == pytest.approx(1.0, abs=1e-6)


class TestVolumetry:
    def test_ventricle_ratio_tracks_ground_truth_closely(self):
        intensity, _labels, truth = synthetic_mri.generate_synthetic_scan(0.6, seed=7)
        affine = np.diag([synthetic_mri.VOXEL_SIZE_MM] * 3 + [1.0])
        volume = preprocessing.preprocess(intensity, affine)
        biomarkers = volumetry.run_volumetry(volume)
        assert biomarkers[VBR] == pytest.approx(truth.ventricle_brain_ratio_pct, abs=0.15)

    def test_hippocampal_volume_is_plausible_and_positive(self):
        intensity, _labels, _truth = synthetic_mri.generate_synthetic_scan(0.5, seed=7)
        affine = np.diag([synthetic_mri.VOXEL_SIZE_MM] * 3 + [1.0])
        volume = preprocessing.preprocess(intensity, affine)
        biomarkers = volumetry.run_volumetry(volume)
        assert 0 < biomarkers[HV] < 15


class TestPipelineEndToEnd:
    def test_severity_zero_is_cognitively_normal(self):
        result = run_pipeline_synthetic(0.0, seed=42)
        assert result.fis_output.stage == Stage.CN

    def test_severity_one_is_alzheimers_stage(self):
        result = run_pipeline_synthetic(1.0, seed=42)
        assert result.fis_output.stage == Stage.AD

    def test_confidence_is_high_at_the_extremes(self):
        healthy = run_pipeline_synthetic(0.0, seed=42)
        severe = run_pipeline_synthetic(1.0, seed=42)
        assert healthy.fis_output.confidence > 90
        assert severe.fis_output.confidence > 90

    @pytest.mark.parametrize("seed", [1, 2, 3])
    def test_hippocampal_volume_decreases_monotonically_with_severity(self, seed: int):
        severities = [0.0, 0.25, 0.5, 0.75, 1.0]
        volumes = [run_pipeline_synthetic(s, seed=seed).biomarkers[HV] for s in severities]
        assert volumes == sorted(volumes, reverse=True)

    def test_biomarker_values_are_deterministic_for_a_fixed_seed(self):
        a = run_pipeline_synthetic(0.5, seed=99)
        b = run_pipeline_synthetic(0.5, seed=99)
        assert a.biomarkers == b.biomarkers
        assert a.fis_output.stage == b.fis_output.stage

    def test_wrong_grid_shape_raises_unprocessable(self):
        volume = preprocessing.PreprocessedVolume(
            data=np.zeros((8, 8, 8), dtype=np.float32),
            brain_mask=np.ones((8, 8, 8), dtype=bool),
            affine=np.eye(4),
            voxel_dims_mm=(2.0, 2.0, 2.0),
            voxel_volume_mm3=8.0,
        )
        with pytest.raises(UnprocessableScanError):
            volumetry.run_volumetry(volume)
