"""Unit tests for app/services/volumetry.py: tissue segmentation and
biomarker extraction — including empty-segmentation and malformed-geometry
edge cases the pipeline has to reject rather than silently return a
plausible-looking but meaningless biomarker value for.
"""

import numpy as np
import pytest

from app.core.exceptions import UnprocessableScanError
from app.models.enums import BiomarkerKey
from app.services import preprocessing, volumetry
from app.services.synthetic_mri import VOLUME_SHAPE, VOXEL_SIZE_MM, generate_synthetic_scan

HV = BiomarkerKey.HIPPOCAMPAL_VOLUME
VBR = BiomarkerKey.VENTRICLE_BRAIN_RATIO
CT = BiomarkerKey.CORTICAL_THICKNESS


def _make_volume(data: np.ndarray, mask: np.ndarray) -> preprocessing.PreprocessedVolume:
    return preprocessing.PreprocessedVolume(
        data=data,
        brain_mask=mask,
        affine=np.diag([VOXEL_SIZE_MM] * 3 + [1.0]),
        voxel_dims_mm=(VOXEL_SIZE_MM, VOXEL_SIZE_MM, VOXEL_SIZE_MM),
        voxel_volume_mm3=VOXEL_SIZE_MM**3,
    )


@pytest.fixture
def preprocessed_synthetic_volume():
    intensity, _labels, _truth = generate_synthetic_scan(0.5, seed=21)
    affine = np.diag([VOXEL_SIZE_MM, VOXEL_SIZE_MM, VOXEL_SIZE_MM, 1.0])
    return preprocessing.preprocess(intensity, affine)


class TestSegmentTissueClasses:
    def test_undersized_brain_mask_is_rejected(self):
        data = np.full(VOLUME_SHAPE, 0.5, dtype=np.float32)
        mask = np.zeros(VOLUME_SHAPE, dtype=bool)
        mask[0:5, 0:5, 0:5] = True  # 125 voxels, under the 500-voxel floor
        volume = _make_volume(data, mask)

        with pytest.raises(UnprocessableScanError, match="too small"):
            volumetry.segment_tissue_classes(volume)

    def test_hippocampus_roi_outside_brain_mask_is_rejected(self):
        """A brain mask large enough to pass the size floor, but positioned
        somewhere the fixed hippocampal search ROI never reaches (e.g. a
        registration/geometry bug upstream) — must fail loudly rather than
        silently score a zero hippocampal volume as if it were real.
        """
        data = np.full(VOLUME_SHAPE, 0.5, dtype=np.float32)
        mask = np.zeros(VOLUME_SHAPE, dtype=bool)
        mask[0:12, 0:12, 0:12] = True  # 1728 voxels, but in a far corner
        volume = _make_volume(data, mask)

        with pytest.raises(UnprocessableScanError, match="does not overlap"):
            volumetry.segment_tissue_classes(volume)

    def test_normal_synthetic_volume_produces_all_three_segments(
        self, preprocessed_synthetic_volume
    ):
        seg = volumetry.segment_tissue_classes(preprocessed_synthetic_volume)
        assert seg.ventricle_mask.any()
        assert seg.cortex_mask.any()
        assert seg.hippocampus_weight.sum() > 0


class TestExtractBiomarkers:
    def test_empty_brain_mask_is_rejected(self):
        data = np.zeros(VOLUME_SHAPE, dtype=np.float32)
        mask = np.zeros(VOLUME_SHAPE, dtype=bool)
        volume = _make_volume(data, mask)
        seg = volumetry.SegmentationResult(
            ventricle_mask=mask, hippocampus_weight=data, cortex_mask=mask
        )
        with pytest.raises(UnprocessableScanError, match="Empty brain mask"):
            volumetry.extract_biomarkers(volume, seg)

    def test_empty_cortex_segmentation_is_rejected(self):
        """An abnormal-contrast volume where nothing crosses the cortex
        brightness threshold — e.g. a scan with clipped/saturated dynamic
        range — leaves no ribbon to measure a thickness from.
        """
        mask = np.ones(VOLUME_SHAPE, dtype=bool)
        data = np.full(VOLUME_SHAPE, 0.3, dtype=np.float32)
        volume = _make_volume(data, mask)
        seg = volumetry.SegmentationResult(
            ventricle_mask=np.zeros(VOLUME_SHAPE, dtype=bool),
            hippocampus_weight=np.zeros(VOLUME_SHAPE, dtype=np.float32),
            cortex_mask=np.zeros(VOLUME_SHAPE, dtype=bool),  # nothing crossed the threshold
        )
        with pytest.raises(UnprocessableScanError, match="ribbon segmentation is empty"):
            volumetry.extract_biomarkers(volume, seg)

    def test_biomarker_values_match_hand_computed_expectations(self):
        """Constructs masks with exactly-known voxel counts and verifies
        extract_biomarkers's arithmetic directly, rather than only checking
        plausibility against a synthetic phantom's ground truth.
        """
        mask = np.zeros(VOLUME_SHAPE, dtype=bool)
        mask[:10, :10, :10] = True  # 1000 brain voxels
        data = np.full(VOLUME_SHAPE, 0.5, dtype=np.float32)

        ventricle_mask = np.zeros(VOLUME_SHAPE, dtype=bool)
        ventricle_mask[:10, :10, :5] = True  # 500 of the 1000 brain voxels -> 50% VBR

        hippocampus_weight = np.zeros(VOLUME_SHAPE, dtype=np.float32)
        hippocampus_weight[0, 0, 0] = 1.0
        hippocampus_weight[0, 0, 1] = 0.5  # weighted sum = 1.5 voxels

        # A solid slab (not a thin ribbon) as the "cortex" so the EDT-based
        # thickness estimate is large and clearly non-degenerate.
        cortex_mask = np.zeros(VOLUME_SHAPE, dtype=bool)
        cortex_mask[20:30, 20:30, 20:30] = True

        volume = _make_volume(data, mask)
        seg = volumetry.SegmentationResult(
            ventricle_mask=ventricle_mask,
            hippocampus_weight=hippocampus_weight,
            cortex_mask=cortex_mask,
        )

        result = volumetry.extract_biomarkers(volume, seg)

        voxel_volume_ml = VOXEL_SIZE_MM**3 / 1000.0
        assert result[HV] == pytest.approx(1.5 * voxel_volume_ml, abs=1e-3)
        assert result[VBR] == pytest.approx(50.0, abs=1e-3)
        assert result[CT] > 0


class TestEstimateRibbonThickness:
    def test_empty_ribbon_raises(self):
        empty = np.zeros((20, 20, 20), dtype=bool)
        with pytest.raises(UnprocessableScanError, match="empty"):
            volumetry._estimate_ribbon_thickness(empty, (2.0, 2.0, 2.0))

    def test_thicker_slab_reads_thicker(self):
        thin = np.zeros((40, 40, 40), dtype=bool)
        thin[18:20, :, :] = True  # 2-voxel slab

        thick = np.zeros((40, 40, 40), dtype=bool)
        thick[10:30, :, :] = True  # 20-voxel slab

        thin_estimate = volumetry._estimate_ribbon_thickness(thin, (2.0, 2.0, 2.0))
        thick_estimate = volumetry._estimate_ribbon_thickness(thick, (2.0, 2.0, 2.0))
        assert thick_estimate > thin_estimate

    def test_respects_voxel_spacing(self):
        """The same voxel geometry at 2x the physical voxel size should
        read roughly 2x the physical thickness.
        """
        slab = np.zeros((40, 40, 40), dtype=bool)
        slab[15:25, :, :] = True

        at_1mm = volumetry._estimate_ribbon_thickness(slab, (1.0, 1.0, 1.0))
        at_2mm = volumetry._estimate_ribbon_thickness(slab, (2.0, 2.0, 2.0))
        assert at_2mm == pytest.approx(at_1mm * 2, rel=0.05)


class TestRunVolumetry:
    def test_wrong_grid_shape_is_rejected(self):
        data = np.zeros((8, 8, 8), dtype=np.float32)
        mask = np.ones((8, 8, 8), dtype=bool)
        volume = preprocessing.PreprocessedVolume(
            data=data,
            brain_mask=mask,
            affine=np.eye(4),
            voxel_dims_mm=(2.0, 2.0, 2.0),
            voxel_volume_mm3=8.0,
        )
        with pytest.raises(UnprocessableScanError, match="calibrated for the synthetic phantom"):
            volumetry.run_volumetry(volume)

    def test_end_to_end_on_synthetic_volume_returns_all_three_biomarkers(
        self, preprocessed_synthetic_volume
    ):
        result = volumetry.run_volumetry(preprocessed_synthetic_volume)
        assert set(result.keys()) == {HV, VBR, CT}
        assert all(v >= 0 for v in result.values())
