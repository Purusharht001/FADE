"""Phase 4: calibration files. Every number the engine uses is loaded from
a versioned JSON file; these tests pin the shipped files and the loader's
handling of incomplete and malformed ones.
"""

import copy
import json

import pytest

from app.core.config import get_settings
from app.models.enums import BiomarkerKey, Stage
from app.services import biomarkers
from app.services.biomarkers import BIOMARKER_DEFS, FuzzyLabel
from app.services.calibration import (
    CALIBRATION_DIR,
    CalibrationError,
    DataDomain,
    get_calibration,
    load_calibration,
    resolve_calibration_path,
)
from tests.calibration_utils import (
    TEST_REAL_VERSION,
    phantom_calibration_dict,
    write_calibration,
    write_test_real_calibration,
)

HV = BiomarkerKey.HIPPOCAMPAL_VOLUME
VBR = BiomarkerKey.VENTRICLE_BRAIN_RATIO
CT = BiomarkerKey.CORTICAL_THICKNESS

# The literals that lived in app/services/biomarkers.py before they moved to
# phantom.v0.json, captured from that code at commit 6619ffe. The move must
# not change a single number.
LEGACY_DEFS = {
    HV: {
        "unit": "mL",
        "normal_range": (3.0, 4.5),
        "display_range": (0.0, 6.5),
        "lower_is_worse": True,
        "label": "Hippocampal Volume",
        "short_label": "Hippocampus",
        "sets": {
            "low": (0.0, 0.0, 2.0, 2.6),
            "borderline": (2.2, 2.6, 3.0, 3.4),
            "normal": (3.0, 3.6, 6.0, 6.0),
        },
    },
    VBR: {
        "unit": "%",
        "normal_range": (0.0, 0.5),
        "display_range": (0.0, 1.6),
        "lower_is_worse": False,
        "label": "Ventricle-to-Brain Ratio",
        "short_label": "VBR",
        "sets": {
            "normal": (0.0, 0.0, 0.3, 0.5),
            "borderline": (0.35, 0.5, 0.75, 0.95),
            "high": (0.75, 0.95, 3.0, 3.0),
        },
    },
    CT: {
        "unit": "mm",
        "normal_range": (5.6, 6.0),
        "display_range": (4.5, 7.0),
        "lower_is_worse": True,
        "label": "Mean Cortical Thickness",
        "short_label": "Cortex",
        "sets": {
            "low": (0.0, 0.0, 4.9, 5.2),
            "borderline": (4.9, 5.2, 5.6, 5.85),
            "normal": (5.6, 5.85, 10.0, 10.0),
        },
    },
}

LEGACY_ABNORMALITY = [
    (HV, 5.0, 0.0),
    (HV, 3.2, 0.25),
    (HV, 2.8, 0.5),
    (HV, 2.4, 0.5833),
    (HV, 1.0, 1.0),
    (VBR, 0.05, 0.0),
    (VBR, 0.4, 0.1666),
    (VBR, 0.6, 0.5),
    (VBR, 0.85, 0.75),
    (VBR, 1.5, 1.0),
    (CT, 6.0, 0.0),
    (CT, 5.7, 0.3),
    (CT, 5.4, 0.5),
    (CT, 5.05, 0.75),
    (CT, 4.5, 1.0),
]


def _numbers_in(node, path=""):
    """Every (path, number) in a JSON tree. Booleans are not numbers here."""
    if isinstance(node, bool):
        return
    if isinstance(node, int | float):
        yield path, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _numbers_in(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _numbers_in(value, f"{path}[{i}]")


class TestPhantomCalibration:
    def test_default_calibration_is_the_phantom_file(self):
        calibration = get_calibration()
        assert calibration.version == "phantom.v0"
        assert calibration.domain == DataDomain.PHANTOM
        assert calibration.complete is True
        assert calibration.missing == ()
        assert calibration.provisional is True

    def test_values_are_unchanged_from_the_old_literals(self):
        calibration = load_calibration(CALIBRATION_DIR / "phantom.v0.json")
        assert set(calibration.biomarkers) == set(LEGACY_DEFS)
        for key, legacy in LEGACY_DEFS.items():
            d = calibration.biomarkers[key]
            assert d.unit == legacy["unit"]
            assert d.normal_range == legacy["normal_range"]
            assert d.display_range == legacy["display_range"]
            assert d.lower_is_worse == legacy["lower_is_worse"]
            assert d.label == legacy["label"]
            assert d.short_label == legacy["short_label"]
            assert {label.value: params for label, params in d.sets.items()} == legacy["sets"]

    @pytest.mark.parametrize(("key", "value", "expected"), LEGACY_ABNORMALITY)
    def test_abnormality_is_unchanged(self, key, value, expected):
        assert get_calibration().biomarkers[key].abnormality(value) == expected

    def test_output_sets_are_centred_at_0_half_and_1_and_provisional(self):
        calibration = load_calibration(CALIBRATION_DIR / "phantom.v0.json")
        assert calibration.output_sets_provisional is True
        assert calibration.output_sets is not None
        peaks = {stage: params[1:3] for stage, params in calibration.output_sets.items()}
        assert peaks == {Stage.CN: (0.0, 0.0), Stage.MCI: (0.5, 0.5), Stage.AD: (1.0, 1.0)}

    def test_output_sets_carry_a_todo_for_the_ml_team(self):
        raw = phantom_calibration_dict()
        assert raw["output_sets"]["provisional"] is True
        assert "TODO(ml-team)" in raw["output_sets"]["todo"]

    def test_hippocampal_volume_is_not_described_as_icv_normalised(self):
        """C2: the phantom measures a raw mL volume and must say so."""
        d = get_calibration().biomarkers[HV]
        assert d.unit == "mL"
        assert "Not normalised for intracranial volume" in d.description

    def test_biomarker_defs_alias_resolves_to_the_active_calibration(self):
        assert BIOMARKER_DEFS is get_calibration().biomarkers
        assert biomarkers.BIOMARKER_DEFS[HV].fuzzify(5.0)[FuzzyLabel.NORMAL] == 1.0

    def test_unknown_module_attribute_still_raises(self):
        with pytest.raises(AttributeError):
            _ = biomarkers.NOT_A_REAL_NAME


class TestRealTemplate:
    def test_template_has_the_same_structure_as_the_phantom_file(self):
        template = json.loads((CALIBRATION_DIR / "real.template.json").read_text("utf-8"))
        phantom = phantom_calibration_dict()
        assert set(template["biomarkers"]) == set(phantom["biomarkers"])
        for key, entry in phantom["biomarkers"].items():
            assert set(template["biomarkers"][key]["sets"]) == set(entry["sets"])
        assert set(template["output_sets"]["sets"]) == set(phantom["output_sets"]["sets"])

    def test_every_number_in_the_template_is_null(self):
        template = json.loads((CALIBRATION_DIR / "real.template.json").read_text("utf-8"))
        numbers = [path for path, _ in _numbers_in(template) if path != "schema_version"]
        assert numbers == []

    def test_template_is_marked_for_the_ml_team(self):
        text = (CALIBRATION_DIR / "real.template.json").read_text("utf-8")
        assert text.count("TODO(ml-team)") >= 5

    def test_template_loads_as_incomplete_rather_than_failing(self):
        calibration = load_calibration(CALIBRATION_DIR / "real.template.json")
        assert calibration.domain == DataDomain.REAL
        assert calibration.complete is False
        assert calibration.biomarkers == {}
        assert calibration.output_sets is None
        # 3 biomarkers x (3 sets x 4 + normal_range 2 + display_range 2) = 48
        assert len(calibration.missing) == 48
        assert "biomarkers.hippocampal_volume.sets.low[0]" in calibration.missing

    def test_template_declares_real_units(self):
        calibration = load_calibration(CALIBRATION_DIR / "real.template.json")
        assert calibration.units == {HV: "‰ICV", VBR: "ratio", CT: "mm"}


class TestRealV1:
    """real.v1.json: fitted to the OASIS-1 FreeSurfer cohort by
    fit_real_calibration.py (outside this repo). These pin that it loads,
    is complete, and keeps the units the derivation code produces.
    """

    def test_loads_complete_and_real(self):
        calibration = load_calibration(CALIBRATION_DIR / "real.v1.json")
        assert calibration.version == "real.v1"
        assert calibration.domain == DataDomain.REAL
        assert calibration.complete is True
        assert calibration.missing == ()
        assert calibration.output_sets is not None

    def test_declares_the_same_units_as_the_template(self):
        real = load_calibration(CALIBRATION_DIR / "real.v1.json")
        template = load_calibration(CALIBRATION_DIR / "real.template.json")
        assert real.units == template.units

    def test_output_sets_are_still_the_provisional_placeholders(self):
        # TODO(clinical: C10): replace once the neurologist agrees the sets.
        calibration = load_calibration(CALIBRATION_DIR / "real.v1.json")
        assert calibration.output_sets_provisional is True


class TestLoaderValidation:
    def test_missing_file(self, tmp_path):
        with pytest.raises(CalibrationError, match="not found"):
            load_calibration(tmp_path / "nope.json")

    def test_invalid_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(CalibrationError, match="not valid JSON"):
            load_calibration(path)

    def test_unordered_breakpoints_are_rejected(self, tmp_path):
        raw = phantom_calibration_dict()
        raw["biomarkers"]["hippocampal_volume"]["sets"]["low"] = [0.0, 3.0, 2.0, 2.6]
        with pytest.raises(CalibrationError, match="a <= b <= c <= d"):
            load_calibration(write_calibration(tmp_path, raw))

    def test_wrong_set_names_are_rejected(self, tmp_path):
        raw = phantom_calibration_dict()
        sets = raw["biomarkers"]["ventricle_brain_ratio"]["sets"]
        sets["low"] = sets.pop("high")
        with pytest.raises(CalibrationError, match="ventricle_brain_ratio.sets"):
            load_calibration(write_calibration(tmp_path, raw))

    def test_missing_biomarker_is_rejected(self, tmp_path):
        raw = phantom_calibration_dict()
        del raw["biomarkers"]["cortical_thickness"]
        with pytest.raises(CalibrationError, match="cortical_thickness is missing"):
            load_calibration(write_calibration(tmp_path, raw))

    def test_unknown_domain_is_rejected(self, tmp_path):
        raw = phantom_calibration_dict()
        raw["domain"] = "clinic"
        with pytest.raises(CalibrationError, match="domain"):
            load_calibration(write_calibration(tmp_path, raw))

    def test_non_numeric_breakpoint_is_rejected(self, tmp_path):
        raw = phantom_calibration_dict()
        raw["biomarkers"]["hippocampal_volume"]["sets"]["low"][0] = "0.0"
        with pytest.raises(CalibrationError, match="must be a number or null"):
            load_calibration(write_calibration(tmp_path, raw))

    def test_boolean_is_not_accepted_as_a_number(self, tmp_path):
        raw = phantom_calibration_dict()
        raw["biomarkers"]["hippocampal_volume"]["normal_range"] = [True, 4.5]
        with pytest.raises(CalibrationError, match="must be a number or null"):
            load_calibration(write_calibration(tmp_path, raw))

    def test_inverted_range_is_rejected(self, tmp_path):
        raw = phantom_calibration_dict()
        raw["biomarkers"]["hippocampal_volume"]["normal_range"] = [4.5, 3.0]
        with pytest.raises(CalibrationError, match="low <= high"):
            load_calibration(write_calibration(tmp_path, raw))

    def test_output_set_outside_the_severity_axis_is_rejected(self, tmp_path):
        raw = phantom_calibration_dict()
        raw["output_sets"]["sets"]["AD"] = [0.5, 1.0, 1.0, 1.5]
        with pytest.raises(CalibrationError, match="0-1 severity axis"):
            load_calibration(write_calibration(tmp_path, raw))

    def test_unsupported_schema_version_is_rejected(self, tmp_path):
        raw = phantom_calibration_dict()
        raw["schema_version"] = 2
        with pytest.raises(CalibrationError, match="schema_version"):
            load_calibration(write_calibration(tmp_path, raw))

    def test_one_null_makes_the_whole_calibration_incomplete(self, tmp_path):
        raw = phantom_calibration_dict()
        raw["biomarkers"]["cortical_thickness"]["sets"]["normal"][3] = None
        calibration = load_calibration(write_calibration(tmp_path, raw))
        assert calibration.complete is False
        assert calibration.missing == ("biomarkers.cortical_thickness.sets.normal[3]",)
        assert calibration.biomarkers == {}

    def test_null_output_sets_leave_the_biomarkers_usable(self, tmp_path):
        raw = copy.deepcopy(phantom_calibration_dict())
        raw["output_sets"]["sets"]["MCI"] = [None, None, None, None]
        calibration = load_calibration(write_calibration(tmp_path, raw))
        assert calibration.complete is True
        assert calibration.output_sets is None

    def test_absent_output_sets_section_is_allowed(self, tmp_path):
        raw = phantom_calibration_dict()
        del raw["output_sets"]
        calibration = load_calibration(write_calibration(tmp_path, raw))
        assert calibration.complete is True
        assert calibration.output_sets is None


class TestCalibrationSelection:
    def test_bare_name_resolves_into_the_package_directory(self):
        assert resolve_calibration_path("phantom.v0.json") == CALIBRATION_DIR / "phantom.v0.json"

    def test_absolute_path_is_used_as_given(self, tmp_path):
        path = tmp_path / "x.json"
        assert resolve_calibration_path(str(path)) == path

    def test_setting_selects_the_file(self, tmp_path, monkeypatch):
        path = write_test_real_calibration(tmp_path)
        monkeypatch.setattr(get_settings(), "calibration_file", str(path))
        calibration = get_calibration()
        assert calibration.version == TEST_REAL_VERSION
        assert calibration.domain == DataDomain.REAL
