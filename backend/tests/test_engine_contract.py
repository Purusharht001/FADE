"""Phase 4: the engine contract for real data. The engine must decline to
stage a case it has no basis for (indeterminate), report a centroid
severity when it does stage one, and leave every previously computed
number exactly as it was.
"""

import datetime
import math

import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.models.enums import BiomarkerKey, DataSource, ScanStatus, Sex, Stage
from app.repositories import patient_repo
from app.services import scan_service
from app.services.calibration import CALIBRATION_DIR, DataDomain, load_calibration
from app.services.fis_engine import (
    ENGINE_VERSION,
    REASON_CALIBRATION_DOMAIN_MISMATCH,
    REASON_CALIBRATION_INCOMPLETE,
    REASON_CALIBRATION_NOT_REAL,
    REASON_MISSING_BIOMARKER,
    REASON_NO_RULE_FIRED,
    REASON_QC_FLAG,
    REASON_UNIT_MISMATCH,
    defuzzify_centroid,
    run_fis,
)
from app.services.pipeline import evaluate_biomarkers, run_pipeline_synthetic
from app.services.synthetic_mri import generate_synthetic_scan, save_nifti
from app.services.volumetry import INHOUSE_UNITS
from tests.calibration_utils import (
    REAL_UNITS,
    TEST_REAL_VERSION,
    load_test_real_calibration,
    write_test_real_calibration,
)

HV = BiomarkerKey.HIPPOCAMPAL_VOLUME
VBR = BiomarkerKey.VENTRICLE_BRAIN_RATIO
CT = BiomarkerKey.CORTICAL_THICKNESS

HEALTHY = {HV: 5.0, VBR: 0.05, CT: 6.0}
# Outside every fuzzy set of the phantom calibration: no rule can fire.
NO_RULE = {HV: 7.0, VBR: 0.1, CT: 6.0}
REAL_UNITS_BY_KEY = {BiomarkerKey(k): v for k, v in REAL_UNITS.items()}

PHANTOM_OUTPUT_SETS = {
    Stage.CN: (0.0, 0.0, 0.0, 0.5),
    Stage.MCI: (0.0, 0.5, 0.5, 1.0),
    Stage.AD: (0.5, 1.0, 1.0, 1.0),
}

# (inputs, stage, confidence, uncertainty, needs_review, membership, fired rules)
# produced by the engine as it stood at commit 6619ffe, before this work.
# fmt: off
GOLDEN = [
    ((5.0, 0.05, 6.0), "CN", 100.0, 0.0, False, {"CN": 1.0, "MCI": 0.0, "AD": 0.0},
     [("R1", 1.0), ("R2", 1.0)]),
    ((1.0, 2.0, 4.5), "AD", 100.0, 0.0, False, {"CN": 0.0, "MCI": 0.0, "AD": 1.0},
     [("R3", 1.0), ("R4", 1.0), ("R5", 1.0), ("R6", 0.75)]),
    ((3.0, 0.5, 5.5), "MCI", 100.0, 0.0, False, {"CN": 0.0, "MCI": 1.0, "AD": 0.0},
     [("R7", 1.0), ("R8", 1.0), ("R10", 1.0)]),
    ((0.1, 0.01, 0.1), "AD", 100.0, 0.0, False, {"CN": 0.0, "MCI": 0.0, "AD": 1.0},
     [("R4", 1.0), ("R6", 0.75)]),
    ((3.3, 0.42, 5.65), "MCI", 71.4, 57.1, True, {"CN": 0.2857, "MCI": 0.7143, "AD": 0.0},
     [("R8", 0.5), ("R9", 0.4667), ("R10", 0.4667), ("R7", 0.25), ("R1", 0.2), ("R2", 0.2)]),
    ((1.2, 1.5, 4.8), "AD", 100.0, 0.0, False, {"CN": 0.0, "MCI": 0.0, "AD": 1.0},
     [("R3", 1.0), ("R4", 1.0), ("R5", 1.0), ("R6", 0.75)]),
    ((2.5, 0.6, 5.3), "MCI", 88.9, 22.2, False, {"CN": 0.0, "MCI": 0.8889, "AD": 0.1111},
     [("R10", 1.0), ("R7", 0.75), ("R8", 0.75), ("R6", 0.125)]),
    ((2.8, 0.3, 5.9), "MCI", 100.0, 0.0, False, {"CN": 0.0, "MCI": 1.0, "AD": 0.0},
     [("R7", 1.0)]),
    ((3.5, 0.8, 5.7), "MCI", 56.1, 73.8, True, {"CN": 0.2991, "MCI": 0.5607, "AD": 0.1402},
     [("R9", 0.75), ("R8", 0.6), ("R10", 0.6), ("R2", 0.4), ("R6", 0.1875)]),
    ((2.4, 0.9, 5.1), "AD", 52.9, 94.1, True, {"CN": 0.0, "MCI": 0.4706, "AD": 0.5294},
     [("R6", 0.5625), ("R8", 0.5), ("R10", 0.5), ("R3", 0.3333), ("R4", 0.3333),
      ("R5", 0.3333), ("R7", 0.25)]),
    ((4.2, 0.45, 5.75), "MCI", 52.6, 94.7, True, {"CN": 0.4737, "MCI": 0.5263, "AD": 0.0},
     [("R9", 0.6667), ("R2", 0.6), ("R8", 0.4), ("R10", 0.4), ("R1", 0.25)]),
    ((2.1, 0.2, 6.2), "AD", 100.0, 0.0, False, {"CN": 0.0, "MCI": 0.0, "AD": 1.0},
     [("R6", 0.625)]),
    ((3.1, 1.2, 5.0), "AD", 69.2, 61.5, True, {"CN": 0.0, "MCI": 0.3077, "AD": 0.6923},
     [("R6", 0.75), ("R5", 0.6667), ("R8", 0.3333), ("R10", 0.3333)]),
]
# fmt: on


def _values(hv, vbr, ct):
    return {HV: hv, VBR: vbr, CT: ct}


class TestExistingOutputsAreUnchanged:
    @pytest.mark.parametrize("case", GOLDEN, ids=[str(c[0]) for c in GOLDEN])
    def test_stage_confidence_uncertainty_and_membership_match_the_old_engine(self, case):
        inputs, stage, confidence, uncertainty, needs_review, membership, fired = case
        result = run_fis(_values(*inputs))

        assert result.determinate is True
        assert result.indeterminate_reason is None
        assert result.stage == Stage(stage)
        assert result.confidence == confidence
        assert result.uncertainty == uncertainty
        assert result.needs_review is needs_review
        assert {s.value: v for s, v in result.membership.items()} == membership
        assert [(r.id, r.firing_strength) for r in result.fired_rules] == fired
        assert result.calibration_version == "phantom.v0"


class TestIndeterminateNoRuleFired:
    def test_no_rule_fired_is_indeterminate_not_cn(self):
        """C4: this input used to come back as CN."""
        result = run_fis(NO_RULE)
        assert result.stage is None
        assert result.determinate is False
        assert result.indeterminate_reason == REASON_NO_RULE_FIRED
        assert result.needs_review is True

    def test_an_indeterminate_result_carries_no_engine_numbers(self):
        result = run_fis(NO_RULE)
        assert result.confidence == 0.0
        assert result.uncertainty == 100.0
        assert result.severity is None
        assert result.fired_rules == []
        assert set(result.membership.values()) == {0.0}
        assert result.calibration_version == "phantom.v0"

    def test_needs_review_is_true_even_with_a_lenient_threshold(self):
        assert run_fis(NO_RULE, review_threshold=200.0).needs_review is True


class TestIndeterminateMissingBiomarker:
    @pytest.mark.parametrize("bad", [None, math.nan, math.inf])
    def test_unusable_value_is_a_missing_biomarker(self, bad):
        result = run_fis({HV: 5.0, VBR: 0.05, CT: bad})
        assert result.stage is None
        assert result.determinate is False
        assert result.indeterminate_reason == f"{REASON_MISSING_BIOMARKER}:cortical_thickness"

    def test_absent_key_is_a_missing_biomarker(self):
        result = run_fis({HV: 5.0})
        assert result.determinate is False
        assert result.indeterminate_reason == (
            f"{REASON_MISSING_BIOMARKER}:ventricle_brain_ratio,cortical_thickness"
        )

    def test_missing_biomarker_never_falls_through_to_a_stage(self):
        """The other two values alone would fire AD rules."""
        result = run_fis({HV: 1.0, VBR: 2.0, CT: None})
        assert result.stage is None
        assert result.fired_rules == []


class TestIndeterminateQcFlag:
    def test_plausibility_flag_makes_the_result_indeterminate(self):
        flags = [{"code": "icv_out_of_range", "kind": "plausibility"}]
        result = run_fis(HEALTHY, qc_flags=flags)
        assert result.stage is None
        assert result.determinate is False
        assert result.indeterminate_reason == f"{REASON_QC_FLAG}:icv_out_of_range"
        assert result.needs_review is True

    def test_info_flag_does_not(self):
        flags = [{"code": "icv_from_legacy_key", "kind": "info"}]
        result = run_fis(HEALTHY, qc_flags=flags)
        assert result.determinate is True
        assert result.stage == Stage.CN

    def test_each_flag_code_is_listed_once(self):
        flags = [
            {"code": "thickness_out_of_range", "kind": "plausibility"},
            {"code": "icv_out_of_range", "kind": "plausibility"},
            {"code": "icv_out_of_range", "kind": "plausibility"},
        ]
        result = run_fis(HEALTHY, qc_flags=flags)
        assert result.indeterminate_reason == (
            f"{REASON_QC_FLAG}:icv_out_of_range,thickness_out_of_range"
        )


class TestCalibrationGating:
    def test_real_data_is_refused_under_the_phantom_calibration(self):
        result = run_fis(HEALTHY, data_domain=DataDomain.REAL)
        assert result.stage is None
        assert result.determinate is False
        assert result.indeterminate_reason == REASON_CALIBRATION_NOT_REAL
        assert result.needs_review is True

    def test_phantom_data_is_staged_under_the_phantom_calibration(self):
        result = run_fis(HEALTHY, data_domain=DataDomain.PHANTOM, units=INHOUSE_UNITS)
        assert result.determinate is True
        assert result.stage == Stage.CN

    def test_null_calibration_refuses_real_data(self):
        template = load_calibration(CALIBRATION_DIR / "real.template.json")
        result = run_fis(
            HEALTHY, calibration=template, data_domain=DataDomain.REAL, units=REAL_UNITS_BY_KEY
        )
        assert result.stage is None
        assert result.determinate is False
        assert result.indeterminate_reason == REASON_CALIBRATION_INCOMPLETE
        assert result.calibration_version == "real.template"

    def test_null_calibration_refuses_everything_else_too(self):
        template = load_calibration(CALIBRATION_DIR / "real.template.json")
        for domain in (DataDomain.PHANTOM, None):
            result = run_fis(HEALTHY, calibration=template, data_domain=domain)
            assert result.determinate is False
            assert result.indeterminate_reason == REASON_CALIBRATION_INCOMPLETE

    def test_real_data_is_staged_under_a_real_calibration(self, tmp_path):
        real = load_test_real_calibration(tmp_path)
        result = run_fis(
            HEALTHY, calibration=real, data_domain=DataDomain.REAL, units=REAL_UNITS_BY_KEY
        )
        assert result.determinate is True
        assert result.stage == Stage.CN
        assert result.calibration_version == TEST_REAL_VERSION

    def test_phantom_data_is_refused_under_a_real_calibration(self, tmp_path):
        real = load_test_real_calibration(tmp_path)
        result = run_fis(HEALTHY, calibration=real, data_domain=DataDomain.PHANTOM)
        assert result.determinate is False
        assert result.indeterminate_reason == REASON_CALIBRATION_DOMAIN_MISMATCH

    def test_unit_mismatch_is_refused_even_when_the_domain_matches(self, tmp_path):
        """A real calibration written in percent must not score a ratio."""
        real = load_test_real_calibration(tmp_path)
        units = dict(REAL_UNITS_BY_KEY)
        units[VBR] = "%"
        result = run_fis(HEALTHY, calibration=real, data_domain=DataDomain.REAL, units=units)
        assert result.determinate is False
        assert result.indeterminate_reason == f"{REASON_UNIT_MISMATCH}:ventricle_brain_ratio"

    def test_every_applicable_reason_is_reported(self):
        result = run_fis(
            {HV: 5.0, VBR: 0.05, CT: None},
            data_domain=DataDomain.REAL,
            units={HV: "‰ICV", VBR: "ratio"},
            qc_flags=[{"code": "icv_out_of_range", "kind": "plausibility"}],
        )
        assert result.indeterminate_reason == "; ".join(
            [
                REASON_CALIBRATION_NOT_REAL,
                f"{REASON_MISSING_BIOMARKER}:cortical_thickness",
                f"{REASON_UNIT_MISMATCH}:hippocampal_volume,ventricle_brain_ratio",
                f"{REASON_QC_FLAG}:icv_out_of_range",
            ]
        )


class TestDefuzzifyCentroid:
    def test_pure_stage_activations(self):
        cn = defuzzify_centroid({Stage.CN: 1.0}, PHANTOM_OUTPUT_SETS)
        mci = defuzzify_centroid({Stage.MCI: 1.0}, PHANTOM_OUTPUT_SETS)
        ad = defuzzify_centroid({Stage.AD: 1.0}, PHANTOM_OUTPUT_SETS)
        # Centroid of a right triangle of base 0.5 sits a third along it.
        assert cn == pytest.approx(1 / 6, abs=1e-3)
        assert mci == pytest.approx(0.5, abs=1e-3)
        assert ad == pytest.approx(5 / 6, abs=1e-3)

    def test_no_activation_has_no_centroid(self):
        assert defuzzify_centroid(dict.fromkeys(Stage, 0.0), PHANTOM_OUTPUT_SETS) is None
        assert defuzzify_centroid({}, PHANTOM_OUTPUT_SETS) is None

    def test_clipping_height_changes_the_shape_not_just_the_scale(self):
        """Scaling a single set leaves its centroid alone; clipping moves it."""
        full = defuzzify_centroid({Stage.CN: 1.0}, PHANTOM_OUTPUT_SETS)
        clipped = defuzzify_centroid({Stage.CN: 0.2}, PHANTOM_OUTPUT_SETS)
        assert clipped is not None and full is not None
        assert clipped > full

    def test_aggregation_is_max_not_sum(self):
        """Two stages at the same level: the overlap is counted once."""
        both = defuzzify_centroid({Stage.CN: 0.5, Stage.AD: 0.5}, PHANTOM_OUTPUT_SETS)
        assert both == pytest.approx(0.5, abs=1e-3)

    def test_severity_rises_as_activation_shifts_toward_ad(self):
        severities = [
            defuzzify_centroid(
                {Stage.CN: 1.0 - t, Stage.MCI: 0.3, Stage.AD: t}, PHANTOM_OUTPUT_SETS
            )
            for t in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
        ]
        assert severities == sorted(severities)
        assert severities[0] < severities[-1]

    def test_result_stays_on_the_severity_axis(self):
        for activation in ({Stage.CN: 1.0}, {Stage.AD: 1.0}, {Stage.CN: 0.01, Stage.AD: 0.99}):
            severity = defuzzify_centroid(activation, PHANTOM_OUTPUT_SETS)
            assert severity is not None
            assert 0.0 <= severity <= 1.0

    def test_activation_above_one_is_clamped(self):
        assert defuzzify_centroid({Stage.MCI: 5.0}, PHANTOM_OUTPUT_SETS) == defuzzify_centroid(
            {Stage.MCI: 1.0}, PHANTOM_OUTPUT_SETS
        )

    def test_output_sets_come_from_the_calibration(self, tmp_path):
        """Different output sets, same activations, different severity."""
        narrow = {
            Stage.CN: (0.0, 0.0, 0.0, 0.2),
            Stage.MCI: (0.4, 0.5, 0.5, 0.6),
            Stage.AD: (0.8, 1.0, 1.0, 1.0),
        }
        activation = {Stage.AD: 1.0}
        assert defuzzify_centroid(activation, narrow) != defuzzify_centroid(
            activation, PHANTOM_OUTPUT_SETS
        )

    def test_engine_reports_no_severity_without_output_sets(self, tmp_path):
        from tests.calibration_utils import phantom_calibration_dict, write_calibration

        raw = phantom_calibration_dict()
        del raw["output_sets"]
        calibration = load_calibration(write_calibration(tmp_path, raw))
        result = run_fis(HEALTHY, calibration=calibration)
        assert result.stage == Stage.CN
        assert result.severity is None


class TestSeverityOnPhantomCases:
    @pytest.mark.parametrize("seed", [1, 2, 3, 42])
    def test_severity_rises_monotonically_with_phantom_severity(self, seed: int):
        dial = [0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0]
        severities = [run_pipeline_synthetic(s, seed=seed).fis_output.severity for s in dial]

        assert all(s is not None for s in severities)
        assert severities == sorted(severities)
        assert severities[0] < 0.25
        assert severities[-1] > 0.75

    def test_severity_agrees_with_the_stage_at_the_extremes(self):
        healthy = run_pipeline_synthetic(0.0, seed=42).fis_output
        severe = run_pipeline_synthetic(1.0, seed=42).fis_output
        assert (healthy.stage, severe.stage) == (Stage.CN, Stage.AD)
        assert healthy.severity < 0.5 < severe.severity


class TestEvaluateBiomarkers:
    def test_abnormality_is_withheld_when_the_calibration_does_not_apply(self):
        result = evaluate_biomarkers(
            {HV: 3.9, VBR: 0.04, CT: 2.8}, REAL_UNITS_BY_KEY, data_domain=DataDomain.REAL
        )
        assert result.fis_output.determinate is False
        assert result.abnormality == {HV: None, VBR: None, CT: None}
        assert result.units == REAL_UNITS_BY_KEY

    def test_missing_value_is_left_out_of_the_readings(self):
        result = evaluate_biomarkers(
            {HV: 5.0, VBR: 0.05, CT: None}, INHOUSE_UNITS, data_domain=DataDomain.PHANTOM
        )
        assert set(result.biomarkers) == {HV, VBR}
        assert result.fis_output.determinate is False
        # The calibration does apply to the two values that are present.
        assert result.abnormality == {HV: 0.0, VBR: 0.0}


class TestPersistence:
    async def test_inhouse_result_for_a_non_phantom_patient_is_indeterminate(
        self, db_session, tmp_path
    ):
        """The in-house volumetry is phantom-tuned: run on a patient that is
        not labelled PHANTOM, it must not produce a stage.
        """
        intensity, _labels, _truth = generate_synthetic_scan(0.6, seed=3)
        path = tmp_path / "scan.nii.gz"
        save_nifti(intensity, path)
        patient = await patient_repo.create(db_session, age=70, sex=Sex.F, source=DataSource.CLINIC)
        scan = await patient_repo.create_scan(
            db_session, patient_id=patient.id, scan_date=datetime.date.today(), file_path=str(path)
        )

        result = await scan_service.process_uploaded_scan(db_session, scan, str(path))

        assert result.status == ScanStatus.COMPLETED
        assert result.fis_result.stage is None
        assert result.fis_result.determinate is False
        assert result.fis_result.indeterminate_reason == REASON_CALIBRATION_NOT_REAL
        assert result.fis_result.needs_review is True
        assert result.fis_result.severity is None
        assert result.fis_result.calibration_version == "phantom.v0"
        assert [r.abnormality for r in result.biomarkers] == [None, None, None]
        assert result.engine_version == ENGINE_VERSION

    async def test_phantom_patient_upload_is_staged(self, db_session, tmp_path):
        intensity, _labels, _truth = generate_synthetic_scan(0.6, seed=3)
        path = tmp_path / "scan.nii.gz"
        save_nifti(intensity, path)
        patient = await patient_repo.create(
            db_session, age=70, sex=Sex.F, source=DataSource.PHANTOM
        )
        scan = await patient_repo.create_scan(
            db_session, patient_id=patient.id, scan_date=datetime.date.today(), file_path=str(path)
        )

        result = await scan_service.process_uploaded_scan(db_session, scan, str(path))

        assert result.fis_result.determinate is True
        assert result.fis_result.stage is not None
        assert result.fis_result.severity is not None
        assert all(r.abnormality is not None for r in result.biomarkers)

    async def test_synthetic_result_records_severity_and_calibration(self, db_session):
        patient = await patient_repo.create(db_session, age=70, sex=Sex.F, source=DataSource.CLINIC)
        scan = await patient_repo.create_scan(
            db_session, patient_id=patient.id, scan_date=datetime.date.today(), file_path=None
        )
        result = await scan_service.process_synthetic_scan(db_session, scan, 0.9, seed=42)

        assert result.fis_result.determinate is True
        assert result.fis_result.indeterminate_reason is None
        assert 0.0 <= result.fis_result.severity <= 1.0
        assert result.fis_result.calibration_version == "phantom.v0"
        assert result.calibration_version == "phantom.v0"


class TestApiContract:
    async def test_simulate_returns_the_additive_fields(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/fis/simulate",
            json={"hippocampalVolume": 4.5, "ventricleBrainRatio": 0.1, "corticalThickness": 6.0},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["stage"] == "CN"
        assert body["determinate"] is True
        assert body["indeterminateReason"] is None
        assert body["severity"] == pytest.approx(1 / 6, abs=1e-3)
        assert body["calibrationVersion"] == "phantom.v0"

    async def test_simulate_returns_a_null_stage_when_no_rule_fires(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/fis/simulate",
            json={"hippocampalVolume": 7.0, "ventricleBrainRatio": 0.1, "corticalThickness": 6.0},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["stage"] is None
        assert body["determinate"] is False
        assert body["indeterminateReason"] == REASON_NO_RULE_FIRED
        assert body["needsReview"] is True
        assert body["severity"] is None

    async def test_scan_and_list_responses_carry_the_additive_fields(self, client: AsyncClient):
        patient = (await client.post("/api/v1/patients", json={"age": 70, "sex": "F"})).json()
        scan = (
            await client.post(
                f"/api/v1/patients/{patient['id']}/scans/synthetic",
                json={"severity": 0.9, "seed": 5},
            )
        ).json()
        fis = scan["fisResult"]
        assert fis["determinate"] is True
        assert fis["indeterminateReason"] is None
        assert 0.0 <= fis["severity"] <= 1.0
        assert fis["calibrationVersion"] == "phantom.v0"

        latest = (await client.get("/api/v1/patients")).json()[0]["latestScan"]
        assert latest["determinate"] is True
        assert latest["indeterminateReason"] is None
        assert latest["severity"] == fis["severity"]

    async def test_cohort_stats_count_indeterminate_results_separately(
        self, client: AsyncClient, tmp_path, monkeypatch
    ):
        patient = (await client.post("/api/v1/patients", json={"age": 70, "sex": "F"})).json()
        await client.post(
            f"/api/v1/patients/{patient['id']}/scans/synthetic", json={"severity": 0.05, "seed": 3}
        )
        # A real calibration makes the next phantom scan indeterminate.
        monkeypatch.setattr(
            get_settings(), "calibration_file", str(write_test_real_calibration(tmp_path))
        )
        other = (await client.post("/api/v1/patients", json={"age": 70, "sex": "F"})).json()
        second = await client.post(
            f"/api/v1/patients/{other['id']}/scans/synthetic", json={"severity": 0.05, "seed": 3}
        )
        assert second.status_code == 201
        assert second.json()["fisResult"]["stage"] is None
        # Wrong domain and, because the units differ too, wrong units.
        assert second.json()["fisResult"]["indeterminateReason"] == (
            f"{REASON_CALIBRATION_DOMAIN_MISMATCH}; "
            f"{REASON_UNIT_MISMATCH}:hippocampal_volume,ventricle_brain_ratio"
        )
        assert [b["abnormality"] for b in second.json()["biomarkers"]] == [None, None, None]

        stats = (await client.get("/api/v1/cohort/stats")).json()
        assert stats["totalScans"] == 2
        assert stats["indeterminate"] == 1
        assert sum(stats["byStage"].values()) == 1
        assert stats["needsReview"] == 1
        # Averaged over the one staged result, not dragged down by the 0.
        assert stats["avgConfidence"] > 50

        listed = (await client.get("/api/v1/patients")).json()
        # Indeterminate results have full uncertainty, so they sort first.
        assert listed[0]["latestScan"]["stage"] is None
        assert listed[0]["latestScan"]["determinate"] is False
        staged_only = (await client.get("/api/v1/patients", params={"stage": "CN"})).json()
        assert len(staged_only) == 1

    async def test_biomarker_definitions_are_served_from_the_calibration(self, client: AsyncClient):
        resp = await client.get("/api/v1/fis/biomarkers")
        assert resp.status_code == 200
        by_key = {d["key"]: d for d in resp.json()}
        assert by_key["hippocampal_volume"]["unit"] == "mL"
        assert by_key["hippocampal_volume"]["normalRange"] == [3.0, 4.5]
        assert by_key["ventricle_brain_ratio"]["lowerIsWorse"] is False
        assert set(by_key["cortical_thickness"]["curve"]) == {"low", "borderline", "normal"}

    async def test_incomplete_calibration_is_reported_not_served(
        self, client: AsyncClient, monkeypatch
    ):
        monkeypatch.setattr(get_settings(), "calibration_file", "real.template.json")

        defs = await client.get("/api/v1/fis/biomarkers")
        assert defs.status_code == 409
        assert defs.json()["error"]["code"] == "calibration_incomplete"

        simulated = await client.post(
            "/api/v1/fis/simulate",
            json={"hippocampalVolume": 4.5, "ventricleBrainRatio": 0.1, "corticalThickness": 6.0},
        )
        assert simulated.status_code == 200
        assert simulated.json()["stage"] is None
        assert simulated.json()["indeterminateReason"] == REASON_CALIBRATION_INCOMPLETE
