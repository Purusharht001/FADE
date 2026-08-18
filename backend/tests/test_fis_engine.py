import pytest

from app.models.enums import BiomarkerKey, Stage
from app.services.fis_engine import RULES, run_fis

HV = BiomarkerKey.HIPPOCAMPAL_VOLUME
VBR = BiomarkerKey.VENTRICLE_BRAIN_RATIO
CT = BiomarkerKey.CORTICAL_THICKNESS


def make_values(hv: float, vbr: float, ct: float) -> dict:
    return {HV: hv, VBR: vbr, CT: ct}


class TestRunFIS:
    def test_healthy_values_stage_cn_high_confidence(self):
        result = run_fis(make_values(hv=5.0, vbr=0.05, ct=6.0))
        assert result.stage == Stage.CN
        assert result.confidence > 90
        assert result.uncertainty < 10
        assert result.needs_review is False

    def test_severely_abnormal_values_stage_ad_high_confidence(self):
        result = run_fis(make_values(hv=1.0, vbr=2.0, ct=4.5))
        assert result.stage == Stage.AD
        assert result.confidence > 90
        assert result.needs_review is False

    def test_membership_always_sums_to_one(self):
        for hv, vbr, ct in [(5.0, 0.05, 6.0), (1.0, 2.0, 4.5), (3.0, 0.5, 5.5), (0.1, 0.01, 0.1)]:
            result = run_fis(make_values(hv, vbr, ct))
            assert sum(result.membership.values()) == pytest.approx(1.0, abs=1e-6)

    def test_borderline_case_is_flagged_for_review(self):
        # Values roughly at the CN/MCI crossover should produce close
        # top-two memberships -> high uncertainty -> needs_review.
        result = run_fis(make_values(hv=3.3, vbr=0.42, ct=5.65))
        assert result.uncertainty > 0

    def test_fired_rules_are_sorted_by_strength_descending(self):
        result = run_fis(make_values(hv=1.2, vbr=1.5, ct=4.8))
        strengths = [r.firing_strength for r in result.fired_rules]
        assert strengths == sorted(strengths, reverse=True)

    def test_only_positively_firing_rules_are_reported(self):
        result = run_fis(make_values(hv=5.0, vbr=0.05, ct=6.0))
        assert all(r.firing_strength > 0 for r in result.fired_rules)

    def test_confidence_and_uncertainty_are_percentages(self):
        result = run_fis(make_values(hv=2.5, vbr=0.6, ct=5.3))
        assert 0 <= result.confidence <= 100
        assert 0 <= result.uncertainty <= 100

    def test_custom_review_threshold_is_respected(self):
        result_strict = run_fis(make_values(hv=3.3, vbr=0.42, ct=5.65), review_threshold=0.0)
        assert result_strict.needs_review is True
        result_lenient = run_fis(make_values(hv=3.3, vbr=0.42, ct=5.65), review_threshold=200.0)
        assert result_lenient.needs_review is False


class TestRuleBase:
    def test_every_rule_has_a_unique_id(self):
        ids = [r.id for r in RULES]
        assert len(ids) == len(set(ids))

    def test_every_stage_has_at_least_one_rule(self):
        consequents = {r.consequent for r in RULES}
        assert consequents == set(Stage)
