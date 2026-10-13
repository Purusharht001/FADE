"""Phase 2b: derivations from parsed volumes to the engine's three inputs.

Fixture numbers are invented, chosen so each expected value can be checked
by hand in the comment beside it.
"""

import os
from pathlib import Path

import pytest

from app.core.config import Settings, get_settings
from app.models.enums import BiomarkerKey
from app.services.biomarker_derivation import (
    DERIVED_UNITS,
    UnitError,
    canonical_unit,
    check_plausibility,
    compute_cortical_gm_volume_norm,
    compute_cortical_thickness,
    compute_hippocampal_volume_norm,
    compute_vbr,
    derive_from_stats,
    derive_from_values,
)
from app.services.freesurfer_stats import (
    StatsParseError,
    parse_aseg_stats,
    parse_stats_bytes,
    parse_stats_dir,
)

FIXTURES = Path(__file__).parent / "fixtures"
FREESURFER = FIXTURES / "freesurfer"
LEGACY = FIXTURES / "freesurfer_legacy"

HV = BiomarkerKey.HIPPOCAMPAL_VOLUME
VBR = BiomarkerKey.VENTRICLE_BRAIN_RATIO
CT = BiomarkerKey.CORTICAL_THICKNESS

# Where the real OAS1_0001_MR1 aseg.stats goes once the ML teammate supplies
# it. The directory is gitignored: real subject data is never committed.
OAS1_0001_ASEG = Path(
    os.environ.get(
        "FADE_OAS1_0001_ASEG", str(FIXTURES / "private" / "OAS1_0001_MR1" / "aseg.stats")
    )
)


def _flag_codes(flags):
    return [f["code"] for f in flags]


class TestFormulas:
    def test_hippocampal_volume_is_per_mille_of_icv(self):
        # (3900 + 4100) / 1 500 000 x 1000 = 5.3333...
        assert compute_hippocampal_volume_norm(3900.0, 4100.0, 1_500_000.0) == pytest.approx(
            5.333333, abs=1e-6
        )

    def test_vbr_is_lateral_plus_inferior_lateral_over_brain_seg_vol(self):
        # (12000 + 11000 + 400 + 600) / 1 100 000 = 0.0218181...
        assert compute_vbr(12000.0, 11000.0, 400.0, 600.0, 1_100_000.0) == pytest.approx(
            0.0218182, abs=1e-7
        )

    def test_vbr_is_a_ratio_not_a_percentage(self):
        assert compute_vbr(20000.0, 18000.0, 1000.0, 1000.0, 1_000_000.0) == pytest.approx(0.04)

    def test_cortical_gm_volume_norm(self):
        # (220000 + 225000) / 1 500 000 x 1000 = 296.666...
        assert compute_cortical_gm_volume_norm(220000.0, 225000.0, 1_500_000.0) == pytest.approx(
            296.666667, abs=1e-6
        )

    @pytest.mark.skipif(
        not OAS1_0001_ASEG.is_file(),
        reason=(
            "Needs the real OAS1_0001_MR1 aseg.stats, which is not committed. Place it at "
            "tests/fixtures/private/OAS1_0001_MR1/aseg.stats or set FADE_OAS1_0001_ASEG."
        ),
    )
    def test_vbr_for_oas1_0001_mr1(self):
        """(27330 + 22792 + 1487 + 1419) / 921495 = 0.057546 for this
        subject. The earlier parser's 0.04143 used a different numerator
        and ICV as denominator; see compute_vbr.
        """
        aseg = parse_aseg_stats(OAS1_0001_ASEG.read_text(encoding="utf-8", errors="replace"))
        vbr = compute_vbr(
            aseg.left_lateral_ventricle_mm3,
            aseg.right_lateral_ventricle_mm3,
            aseg.left_inf_lat_vent_mm3,
            aseg.right_inf_lat_vent_mm3,
            aseg.brain_seg_vol_mm3,
        )
        assert vbr == pytest.approx(0.057546, abs=5e-6)


class TestCorticalThickness:
    REGIONS = ["entorhinal", "parahippocampal", "superiortemporal", "middletemporal"]

    def _aparc(self):
        parsed = parse_stats_dir(FREESURFER)
        assert parsed.lh_aparc is not None and parsed.rh_aparc is not None
        return parsed.lh_aparc, parsed.rh_aparc

    def test_default_regions_plain_mean_over_both_hemispheres(self):
        lh, rh = self._aparc()
        regions = get_settings().cortical_thickness_regions
        # lh 3.2 + 2.8 + 2.7 + 2.8 + 2.7 = 14.2 ; rh 3.4 + 2.6 + 2.7 + 2.9 + 2.8 = 14.4
        assert compute_cortical_thickness(lh, rh, regions) == pytest.approx(28.6 / 10)

    def test_default_region_list(self):
        assert get_settings().cortical_thickness_regions == [
            "entorhinal",
            "parahippocampal",
            "superiortemporal",
            "middletemporal",
            "inferiortemporal",
        ]

    def test_region_list_is_configurable(self):
        lh, rh = self._aparc()
        assert compute_cortical_thickness(lh, rh, ["entorhinal"]) == pytest.approx((3.2 + 3.4) / 2)
        assert compute_cortical_thickness(lh, rh, ["precuneus"]) == pytest.approx(2.3)

    def test_surface_area_weighting(self):
        lh, rh = self._aparc()
        # entorhinal: lh 3.2 over 400 mm2, rh 3.4 over 380 mm2
        expected = (3.2 * 400 + 3.4 * 380) / (400 + 380)
        weighted = compute_cortical_thickness(lh, rh, ["entorhinal"], weighting="surface_area")
        assert weighted == pytest.approx(expected)
        assert weighted != pytest.approx((3.2 + 3.4) / 2)

    def test_missing_region_is_named_with_its_hemisphere(self):
        lh, rh = self._aparc()
        with pytest.raises(StatsParseError, match="lh.fusiform, rh.fusiform"):
            compute_cortical_thickness(lh, rh, ["entorhinal", "fusiform"])

    def test_empty_region_list_is_a_configuration_error(self):
        lh, rh = self._aparc()
        with pytest.raises(ValueError, match="No cortical thickness regions"):
            compute_cortical_thickness(lh, rh, [])


class TestDeriveFromStats:
    def test_three_inputs_and_units_from_a_full_stats_set(self):
        derived = derive_from_stats(parse_stats_dir(FREESURFER))

        assert derived.values[HV] == pytest.approx(5.333333, abs=1e-6)
        assert derived.values[VBR] == pytest.approx(0.0218182, abs=1e-7)
        assert derived.values[CT] == pytest.approx(2.86)
        assert derived.units == {HV: "‰ICV", VBR: "ratio", CT: "mm"}
        assert derived.units == DERIVED_UNITS
        assert derived.qc_flags == []

    def test_raw_volumes_hold_the_parsed_numbers_untouched(self):
        raw = derive_from_stats(parse_stats_dir(FREESURFER)).raw_volumes

        assert raw["icv_mm3"] == 1500000.0
        assert raw["icv_source"] == "eTIV"
        assert raw["brain_seg_vol_mm3"] == 1100000.0
        assert raw["left_hippocampus_mm3"] == 3900.0
        assert raw["right_hippocampus_mm3"] == 4100.0
        assert raw["left_lateral_ventricle_mm3"] == 12000.0
        assert raw["right_inf_lat_vent_mm3"] == 600.0
        assert raw["third_ventricle_mm3"] == 1500.0
        assert raw["fourth_ventricle_mm3"] == 1800.0
        assert raw["left_cerebral_cortex_mm3"] == 220000.0
        assert raw["thickness_mm"]["lh"]["entorhinal"] == 3.2
        assert raw["thickness_mm"]["rh"]["precuneus"] == 2.3
        assert raw["surface_area_mm2"]["rh"]["entorhinal"] == 380.0
        assert raw["thickness_regions"] == get_settings().cortical_thickness_regions
        assert raw["thickness_weighting"] == "unweighted"

    def test_cortical_gm_volume_norm_is_stored_for_the_ml_team(self):
        raw = derive_from_stats(parse_stats_dir(FREESURFER)).raw_volumes
        assert raw["cortical_gm_volume_norm"] == pytest.approx(296.666667, abs=1e-6)
        assert raw["hippocampus_total_mm3"] == 8000.0

    def test_without_aparc_files_thickness_is_missing_not_invented(self):
        derived = derive_from_stats(parse_stats_bytes((FREESURFER / "aseg.stats").read_bytes()))

        assert derived.values[CT] is None
        assert derived.values[HV] is not None and derived.values[VBR] is not None
        assert "thickness_mm" not in derived.raw_volumes
        assert _flag_codes(derived.qc_flags) == ["cortical_thickness_unavailable"]
        # Informational: the missing biomarker, not this flag, is what makes
        # the engine's result indeterminate.
        assert derived.qc_flags[0]["kind"] == "info"

    def test_legacy_file_derives_and_notes_the_icv_measure(self):
        derived = derive_from_stats(parse_stats_dir(LEGACY))
        # (3000 + 3300) / 1 400 000 x 1000 = 4.5 ; 40000 / 1 000 000 = 0.04
        assert derived.values[HV] == pytest.approx(4.5)
        assert derived.values[VBR] == pytest.approx(0.04)
        assert "icv_from_legacy_measure" in _flag_codes(derived.qc_flags)
        assert derived.raw_volumes["cortical_gm_volume_norm"] == pytest.approx(425000 / 1400)

    def test_region_list_setting_is_honoured(self):
        settings = Settings(_env_file=None, cortical_thickness_regions=["precuneus"])
        derived = derive_from_stats(parse_stats_dir(FREESURFER), settings=settings)
        assert derived.values[CT] == pytest.approx(2.3)
        assert derived.raw_volumes["thickness_regions"] == ["precuneus"]

    def test_weighting_setting_is_honoured(self):
        settings = Settings(_env_file=None, cortical_thickness_weighting="surface_area")
        derived = derive_from_stats(parse_stats_dir(FREESURFER), settings=settings)
        assert derived.raw_volumes["thickness_weighting"] == "surface_area"
        assert derived.values[CT] != pytest.approx(2.86)


class TestPlausibility:
    def test_bounds_come_from_settings(self):
        settings = get_settings()
        assert settings.plausibility_icv_mm3 == (1.0e6, 2.2e6)
        assert settings.plausibility_hippocampus_total_mm3 == (2000.0, 12000.0)
        assert settings.plausibility_thickness_mm == (1.0, 5.0)

    def test_values_inside_the_bounds_raise_no_flag(self):
        assert (
            check_plausibility(
                icv_mm3=1.5e6, hippocampus_total_mm3=8000.0, cortical_thickness_mm=2.8
            )
            == []
        )

    def test_values_on_the_bounds_are_plausible(self):
        assert (
            check_plausibility(
                icv_mm3=1.0e6, hippocampus_total_mm3=12000.0, cortical_thickness_mm=5.0
            )
            == []
        )

    @pytest.mark.parametrize(
        ("kwargs", "code"),
        [
            ({"icv_mm3": 0.9e6}, "icv_out_of_range"),
            ({"icv_mm3": 2.5e6}, "icv_out_of_range"),
            ({"hippocampus_total_mm3": 1500.0}, "hippocampus_out_of_range"),
            ({"hippocampus_total_mm3": 13000.0}, "hippocampus_out_of_range"),
            ({"cortical_thickness_mm": 0.6}, "thickness_out_of_range"),
            ({"cortical_thickness_mm": 5.4}, "thickness_out_of_range"),
        ],
    )
    def test_out_of_range_values_are_flagged(self, kwargs, code):
        flags = check_plausibility(**kwargs)
        assert _flag_codes(flags) == [code]
        flag = flags[0]
        assert flag["kind"] == "plausibility"
        assert flag["value"] == next(iter(kwargs.values()))
        assert flag["min"] < flag["max"]
        assert "outside the plausible range" in flag["message"]

    def test_changing_a_bound_in_settings_changes_the_check(self):
        strict = Settings(_env_file=None, plausibility_thickness_mm=(2.9, 5.0))
        assert check_plausibility(cortical_thickness_mm=2.8) == []
        assert _flag_codes(check_plausibility(cortical_thickness_mm=2.8, settings=strict)) == [
            "thickness_out_of_range"
        ]

    def test_bounds_can_be_set_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("PLAUSIBILITY_ICV_MM3", "[900000, 2000000]")
        assert Settings(_env_file=None).plausibility_icv_mm3 == (900000.0, 2000000.0)

    def test_implausible_stats_are_flagged_not_rejected(self):
        """A segmentation failure must not crash ingestion."""
        text = (FREESURFER / "aseg.stats").read_text(encoding="utf-8")
        text = text.replace(
            "Estimated Total Intracranial Volume, 1500000.000000",
            "Estimated Total Intracranial Volume, 2600000.000000",
        ).replace("3900     3900.0  Left-Hippocampus", " 300      300.0  Left-Hippocampus")
        derived = derive_from_stats(parse_stats_bytes(text.encode()))

        assert set(_flag_codes(derived.qc_flags)) >= {"icv_out_of_range"}
        assert derived.values[HV] is not None
        assert all(
            f["kind"] == "plausibility" for f in derived.qc_flags if f["code"].endswith("_range")
        )


class TestDeriveFromValues:
    def test_values_pass_through_with_canonical_units(self):
        derived = derive_from_values(
            hippocampal_volume_norm=5.2, ventricle_brain_ratio=0.03, cortical_thickness_mm=2.9
        )
        assert derived.values == {HV: 5.2, VBR: 0.03, CT: 2.9}
        assert derived.units == DERIVED_UNITS
        assert derived.qc_flags == []

    def test_missing_thickness_stays_missing(self):
        derived = derive_from_values(
            hippocampal_volume_norm=5.2, ventricle_brain_ratio=0.03, cortical_thickness_mm=None
        )
        assert derived.values[CT] is None

    def test_consistent_raw_volumes_raise_no_flag(self):
        derived = derive_from_values(
            hippocampal_volume_norm=5.3333,
            ventricle_brain_ratio=0.03,
            cortical_thickness_mm=None,
            icv_mm3=1_500_000.0,
            hippocampus_total_mm3=8000.0,
        )
        assert derived.qc_flags == []

    @pytest.mark.parametrize("wrong_scale", [0.0053333, 0.53333, 8000.0])
    def test_normalised_value_in_the_wrong_scale_is_flagged(self, wrong_scale):
        """A fraction, a percentage or a raw mm3 volume in a per-mille column."""
        derived = derive_from_values(
            hippocampal_volume_norm=wrong_scale,
            ventricle_brain_ratio=0.03,
            cortical_thickness_mm=None,
            icv_mm3=1_500_000.0,
            hippocampus_total_mm3=8000.0,
        )
        assert _flag_codes(derived.qc_flags) == ["hippocampal_norm_inconsistent"]
        assert derived.qc_flags[0]["kind"] == "plausibility"

    def test_raw_volume_bounds_apply(self):
        derived = derive_from_values(
            hippocampal_volume_norm=0.5,
            ventricle_brain_ratio=0.03,
            cortical_thickness_mm=6.0,
            icv_mm3=3_000_000.0,
            hippocampus_total_mm3=1500.0,
        )
        assert _flag_codes(derived.qc_flags) == [
            "icv_out_of_range",
            "hippocampus_out_of_range",
            "thickness_out_of_range",
        ]


class TestUnits:
    def test_canonical_units_are_accepted(self):
        assert canonical_unit(HV, "‰ICV") == "‰ICV"
        assert canonical_unit(VBR, "ratio") == "ratio"
        assert canonical_unit(CT, "mm") == "mm"

    def test_ascii_alias_for_per_mille(self):
        assert canonical_unit(HV, "permille_ICV") == "‰ICV"

    @pytest.mark.parametrize(
        ("key", "unit"),
        [
            (HV, "mL"),
            (HV, "mm3"),
            (HV, "%"),
            (HV, "permille_icv"),
            (VBR, "%"),
            (VBR, "percent"),
            (VBR, ""),
            (CT, "cm"),
            (CT, "MM"),
        ],
    )
    def test_any_other_unit_is_rejected_not_converted(self, key, unit):
        with pytest.raises(UnitError, match="must be given in"):
            canonical_unit(key, unit)

    def test_unit_error_maps_to_http_422(self):
        with pytest.raises(UnitError) as excinfo:
            canonical_unit(VBR, "%")
        assert excinfo.value.status_code == 422
        assert excinfo.value.code == "invalid_unit"
