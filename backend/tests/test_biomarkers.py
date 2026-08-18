import pytest

from app.models.enums import BiomarkerKey
from app.services.biomarkers import BIOMARKER_DEFS, FuzzyLabel, trapmf


class TestTrapmf:
    def test_zero_outside_support(self):
        assert trapmf(-1, 0, 1, 2, 3) == 0.0
        assert trapmf(4, 0, 1, 2, 3) == 0.0

    def test_full_membership_on_plateau(self):
        assert trapmf(1.5, 0, 1, 2, 3) == 1.0

    def test_rising_and_falling_edges(self):
        assert trapmf(0.5, 0, 1, 2, 3) == pytest.approx(0.5)
        assert trapmf(2.5, 0, 1, 2, 3) == pytest.approx(0.5)

    def test_degenerate_shoulder_is_still_a_step(self):
        # a == b means the membership jumps straight to 1 at a — used for the
        # "normal" set's healthy-side shoulder.
        assert trapmf(0.0, 0.0, 0.0, 6.0, 6.0) == 1.0


class TestHippocampalVolume:
    def test_healthy_value_reads_fully_normal(self):
        d = BIOMARKER_DEFS[BiomarkerKey.HIPPOCAMPAL_VOLUME]
        degrees = d.fuzzify(5.0)
        assert degrees[FuzzyLabel.NORMAL] == 1.0
        assert degrees[FuzzyLabel.LOW] == 0.0
        assert d.abnormality(5.0) == 0.0

    def test_severely_atrophied_value_reads_fully_low(self):
        d = BIOMARKER_DEFS[BiomarkerKey.HIPPOCAMPAL_VOLUME]
        degrees = d.fuzzify(1.0)
        assert degrees[FuzzyLabel.LOW] == 1.0
        assert d.abnormality(1.0) == 1.0

    def test_borderline_value_is_between(self):
        d = BIOMARKER_DEFS[BiomarkerKey.HIPPOCAMPAL_VOLUME]
        abnormality = d.abnormality(2.8)
        assert 0.0 < abnormality < 1.0


class TestVentricleBrainRatio:
    def test_higher_is_worse_direction(self):
        d = BIOMARKER_DEFS[BiomarkerKey.VENTRICLE_BRAIN_RATIO]
        assert d.lower_is_worse is False
        assert d.abnormality(0.05) < d.abnormality(1.5)


class TestAbnormalityMonotonicity:
    @pytest.mark.parametrize("key", list(BiomarkerKey))
    def test_abnormality_is_monotonic_along_the_worse_direction(self, key: BiomarkerKey):
        d = BIOMARKER_DEFS[key]
        lo, hi = d.normal_range
        span = hi - lo if hi > lo else 1.0
        # Sample from deep-normal to deep-abnormal along the "worse" direction.
        if d.lower_is_worse:
            samples = [hi + span, hi, lo, lo - span * 0.5, lo - span * 1.5]
        else:
            samples = [lo - span, lo, hi, hi + span * 0.5, hi + span * 1.5]

        abnormalities = [d.abnormality(v) for v in samples]
        assert abnormalities == sorted(abnormalities)
        assert all(0.0 <= a <= 1.0 for a in abnormalities)
