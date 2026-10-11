"""Phase 2a: the FreeSurfer / FastSurfer stats parser.

The fixture files under tests/fixtures/ are hand-written and every number
in them is invented. No real subject's stats are committed.
"""

from pathlib import Path

import pytest

from app.services.freesurfer_stats import (
    StatsParseError,
    find_stats_files,
    parse_aparc_stats,
    parse_aseg_stats,
    parse_stats_bytes,
    parse_stats_dir,
    read_stats_file,
    vinn_sibling,
)

FIXTURES = Path(__file__).parent / "fixtures"
FREESURFER = FIXTURES / "freesurfer"
LEGACY = FIXTURES / "freesurfer_legacy"
FASTSURFER_STATS = FIXTURES / "fastsurfer" / "stats"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _aseg_with(replacements: dict[str, str]) -> str:
    text = _text(FREESURFER / "aseg.stats")
    for old, new in replacements.items():
        assert old in text, f"fixture no longer contains {old!r}"
        text = text.replace(old, new)
    return text


class TestParseAseg:
    def test_reads_measures_and_structures(self):
        aseg = parse_aseg_stats(_text(FREESURFER / "aseg.stats"))

        assert aseg.icv_mm3 == 1500000.0
        assert aseg.icv_source == "eTIV"
        assert aseg.brain_seg_vol_mm3 == 1100000.0
        assert aseg.left_hippocampus_mm3 == 3900.0
        assert aseg.right_hippocampus_mm3 == 4100.0
        assert aseg.left_lateral_ventricle_mm3 == 12000.0
        assert aseg.right_lateral_ventricle_mm3 == 11000.0
        assert aseg.left_inf_lat_vent_mm3 == 400.0
        assert aseg.right_inf_lat_vent_mm3 == 600.0
        assert aseg.third_ventricle_mm3 == 1500.0
        assert aseg.fourth_ventricle_mm3 == 1800.0

    def test_cortex_volume_comes_from_the_measures_when_not_in_the_table(self):
        aseg = parse_aseg_stats(_text(FREESURFER / "aseg.stats"))
        assert aseg.left_cerebral_cortex_mm3 == 220000.0
        assert aseg.right_cerebral_cortex_mm3 == 225000.0
        assert aseg.cortex_source == "measures"

    def test_a_zero_volume_in_an_unused_structure_is_not_an_error(self):
        """5th-Ventricle is routinely 0 in real files."""
        aseg = parse_aseg_stats(_text(FREESURFER / "aseg.stats"))
        assert aseg.structures["5th-Ventricle"] == 0.0

    def test_measure_description_containing_a_comma_still_parses(self):
        text = _aseg_with(
            {"Brain Segmentation Volume, 1100000.000000": "Brain, Segmentation Volume, 1100000.0"}
        )
        assert parse_aseg_stats(text).brain_seg_vol_mm3 == 1100000.0


class TestLegacyFreeSurfer:
    def test_icv_is_read_from_the_older_measure_name(self):
        aseg = parse_aseg_stats(_text(LEGACY / "aseg.stats"))
        assert aseg.icv_mm3 == 1400000.0
        assert aseg.icv_source == "ICV"

    def test_columns_are_located_by_header_not_position(self):
        """This fixture has StructName before NVoxels and Volume_mm3."""
        aseg = parse_aseg_stats(_text(LEGACY / "aseg.stats"))
        assert aseg.left_hippocampus_mm3 == 3000.0
        assert aseg.right_hippocampus_mm3 == 3300.0
        assert aseg.left_lateral_ventricle_mm3 == 20000.0

    def test_cortex_volume_comes_from_the_structure_table(self):
        aseg = parse_aseg_stats(_text(LEGACY / "aseg.stats"))
        assert aseg.left_cerebral_cortex_mm3 == 210000.0
        assert aseg.right_cerebral_cortex_mm3 == 215000.0
        assert aseg.cortex_source == "structure_table"

    def test_etiv_is_preferred_when_both_measures_exist(self):
        text = _text(LEGACY / "aseg.stats").replace(
            "# NRows 10",
            "# Measure EstimatedTotalIntraCranialVol, eTIV, Estimated, 1450000.0, mm^3\n# NRows 10",
        )
        aseg = parse_aseg_stats(text)
        assert (aseg.icv_mm3, aseg.icv_source) == (1450000.0, "eTIV")


class TestParseAparc:
    def test_reads_thickness_and_area_per_region(self):
        lh = parse_aparc_stats(_text(FREESURFER / "lh.aparc.stats"), "lh")
        assert lh.hemisphere == "lh"
        assert lh.regions["entorhinal"].thick_avg_mm == 3.2
        assert lh.regions["entorhinal"].surf_area_mm2 == 400.0
        assert lh.regions["superiortemporal"].thick_avg_mm == 2.7
        assert set(lh.regions) == {
            "entorhinal",
            "inferiortemporal",
            "middletemporal",
            "parahippocampal",
            "precuneus",
            "superiortemporal",
        }

    def test_right_hemisphere(self):
        rh = parse_aparc_stats(_text(FREESURFER / "rh.aparc.stats"), "rh")
        assert rh.regions["entorhinal"].thick_avg_mm == 3.4
        assert rh.regions["parahippocampal"].thick_avg_mm == 2.6

    def test_swapped_hemisphere_files_are_caught(self):
        with pytest.raises(StatsParseError, match="is a rh file but was supplied as the lh"):
            parse_aparc_stats(_text(FREESURFER / "rh.aparc.stats"), "lh")

    def test_columns_are_located_by_header(self):
        text = "\n".join(
            [
                "# hemi lh",
                "# ColHeaders ThickAvg StructName SurfArea",
                "3.100 entorhinal 410",
            ]
        )
        lh = parse_aparc_stats(text, "lh")
        assert lh.regions["entorhinal"].thick_avg_mm == 3.1
        assert lh.regions["entorhinal"].surf_area_mm2 == 410.0

    def test_missing_thickness_column(self):
        text = "# ColHeaders StructName NumVert SurfArea\nentorhinal 600 400\n"
        with pytest.raises(StatsParseError, match="missing the column.*ThickAvg"):
            parse_aparc_stats(text, "lh")

    def test_no_region_rows(self):
        text = "# hemi lh\n# ColHeaders StructName SurfArea ThickAvg\n"
        with pytest.raises(StatsParseError, match="has no region rows"):
            parse_aparc_stats(text, "lh")


class TestAsegErrors:
    def test_missing_icv(self):
        text = "\n".join(
            line
            for line in _text(FREESURFER / "aseg.stats").splitlines()
            if "EstimatedTotalIntraCranialVol" not in line
        )
        with pytest.raises(StatsParseError, match="no intracranial volume"):
            parse_aseg_stats(text)

    def test_missing_brain_seg_vol(self):
        text = "\n".join(
            line
            for line in _text(FREESURFER / "aseg.stats").splitlines()
            if "Measure BrainSeg, BrainSegVol," not in line
        )
        with pytest.raises(StatsParseError, match="key BrainSegVol"):
            parse_aseg_stats(text)

    def test_missing_structures_are_named(self):
        text = "\n".join(
            line
            for line in _text(FREESURFER / "aseg.stats").splitlines()
            if "Hippocampus" not in line
        )
        with pytest.raises(
            StatsParseError, match="missing the structure.*Left-Hippocampus, Right-Hippocampus"
        ):
            parse_aseg_stats(text)

    def test_zero_volume_in_a_required_structure(self):
        text = _aseg_with(
            {"3900     3900.0  Left-Hippocampus": "   0        0.0  Left-Hippocampus"}
        )
        with pytest.raises(StatsParseError, match="Left-Hippocampus is zero or negative"):
            parse_aseg_stats(text)

    def test_negative_volume_in_a_required_structure(self):
        text = _aseg_with({"12000.0  Left-Lateral-Ventricle": "-12000.0  Left-Lateral-Ventricle"})
        with pytest.raises(StatsParseError, match="Left-Lateral-Ventricle is zero or negative"):
            parse_aseg_stats(text)

    def test_zero_icv(self):
        text = _aseg_with({"Estimated Total Intracranial Volume, 1500000.000000": "Est, 0.0"})
        with pytest.raises(StatsParseError, match="intracranial volume.*zero or negative"):
            parse_aseg_stats(text)

    def test_unparseable_row_reports_the_line_number_only(self):
        text = _aseg_with(
            {"3900     3900.0  Left-Hippocampus": "3900  SECRET-NAME  Left-Hippocampus"}
        )
        with pytest.raises(StatsParseError) as excinfo:
            parse_aseg_stats(text)
        message = str(excinfo.value)
        assert "line 36" in message
        assert "Volume_mm3 is not a number" in message
        assert "SECRET-NAME" not in message

    def test_row_with_the_wrong_number_of_columns(self):
        text = _aseg_with({"  1   4     12000    12000.0  Left-Lateral-Ventricle": "  1   4  Left"})
        with pytest.raises(StatsParseError, match=r"line 31 .*expected 10 columns, found 8"):
            parse_aseg_stats(text)

    def test_data_before_colheaders(self):
        with pytest.raises(StatsParseError, match="before any '# ColHeaders' line"):
            parse_aseg_stats("# Measure BrainSeg, BrainSegVol, x, 1.0, mm^3\n1 4 100 100.0 X\n")

    def test_no_table_at_all(self):
        with pytest.raises(StatsParseError, match="no '# ColHeaders' line"):
            parse_aseg_stats("# Measure BrainSeg, BrainSegVol, x, 1.0, mm^3\n")

    def test_empty_text(self):
        with pytest.raises(StatsParseError, match="is empty"):
            parse_aseg_stats("   \n")

    def test_non_numeric_measure(self):
        text = _aseg_with({"Estimated Total Intracranial Volume, 1500000.000000": "Est, n/a"})
        with pytest.raises(StatsParseError, match="measure eTIV is not a number"):
            parse_aseg_stats(text)

    def test_error_never_echoes_the_header(self):
        """The header names a user, a host and a subject; none may leak."""
        text = _aseg_with({"3900     3900.0  Left-Hippocampus": "3900  oops  Left-Hippocampus"})
        with pytest.raises(StatsParseError) as excinfo:
            parse_aseg_stats(text)
        for private in ("fixture-user", "fixture-host", "FIXTURE_0001"):
            assert private not in str(excinfo.value)

    def test_error_maps_to_http_422(self):
        assert StatsParseError("x").status_code == 422
        assert StatsParseError("x").code == "stats_parse_error"


class TestFastSurferVariants:
    def test_vinn_sibling_name(self):
        assert vinn_sibling(Path("stats/aseg+DKT.stats")).name == "aseg+DKT.VINN.stats"

    def test_empty_symlink_stand_in_falls_back_to_the_vinn_file(self):
        """On Windows FastSurfer's aseg+DKT.stats symlink reads as 0 bytes."""
        empty = FASTSURFER_STATS / "aseg+DKT.stats"
        assert empty.stat().st_size == 0

        text, used = read_stats_file(empty)
        assert used.name == "aseg+DKT.VINN.stats"
        assert parse_aseg_stats(text).left_hippocampus_mm3 == 4000.0

    def test_directory_discovery_accepts_fastsurfer_names(self):
        files = find_stats_files(FASTSURFER_STATS)
        assert files.aseg.name == "aseg+DKT.stats"
        assert files.lh_aparc is not None and files.lh_aparc.name.startswith("lh.aparc.DKTatlas")
        assert files.rh_aparc is not None and files.rh_aparc.name.startswith("rh.aparc.DKTatlas")

    def test_fastsurfer_directory_parses_end_to_end(self):
        parsed = parse_stats_dir(FASTSURFER_STATS)
        assert parsed.aseg.icv_mm3 == 1600000.0
        assert parsed.aseg.brain_seg_vol_mm3 == 1150000.0
        assert parsed.lh_aparc is not None and parsed.rh_aparc is not None
        assert parsed.lh_aparc.regions["entorhinal"].thick_avg_mm == 3.2

    def test_cortex_volume_is_summed_from_dkt_regions(self):
        parsed = parse_stats_dir(FASTSURFER_STATS)
        assert parsed.aseg.cortex_source == "dkt_sum"
        assert parsed.aseg.left_cerebral_cortex_mm3 == 1900.0 + 11000.0 + 10500.0
        assert parsed.aseg.right_cerebral_cortex_mm3 == 1850.0 + 10800.0 + 11500.0

    def test_only_the_vinn_file_present(self, tmp_path):
        (tmp_path / "aseg+DKT.VINN.stats").write_bytes(
            (FASTSURFER_STATS / "aseg+DKT.VINN.stats").read_bytes()
        )
        parsed = parse_stats_dir(tmp_path)
        assert parsed.aseg.left_hippocampus_mm3 == 4000.0
        assert parsed.lh_aparc is None and parsed.rh_aparc is None

    def test_empty_file_with_no_sibling_is_a_clear_error(self, tmp_path):
        (tmp_path / "aseg+DKT.stats").write_bytes(b"")
        with pytest.raises(StatsParseError, match=r"is empty \(0 bytes\).*VINN"):
            read_stats_file(tmp_path / "aseg+DKT.stats")

    def test_empty_file_with_an_empty_sibling_is_still_an_error(self, tmp_path):
        (tmp_path / "aseg+DKT.stats").write_bytes(b"")
        (tmp_path / "aseg+DKT.VINN.stats").write_bytes(b"")
        with pytest.raises(StatsParseError, match="No aseg stats file"):
            find_stats_files(tmp_path)

    def test_freesurfer_directory(self):
        parsed = parse_stats_dir(FREESURFER)
        assert parsed.aseg.icv_source == "eTIV"
        assert parsed.lh_aparc is not None and parsed.rh_aparc is not None

    def test_directory_without_aparc_files(self):
        parsed = parse_stats_dir(LEGACY)
        assert parsed.lh_aparc is None and parsed.rh_aparc is None

    def test_one_hemisphere_on_disk_means_no_thickness(self, tmp_path):
        for name in ("aseg.stats", "lh.aparc.stats"):
            (tmp_path / name).write_bytes((FREESURFER / name).read_bytes())
        parsed = parse_stats_dir(tmp_path)
        assert parsed.lh_aparc is None and parsed.rh_aparc is None

    def test_missing_directory(self, tmp_path):
        with pytest.raises(StatsParseError, match="No stats directory"):
            find_stats_files(tmp_path / "nope")

    def test_directory_with_no_aseg(self, tmp_path):
        with pytest.raises(StatsParseError, match="No aseg stats file"):
            find_stats_files(tmp_path)


class TestParseUploadedBytes:
    def test_all_three_files(self):
        parsed = parse_stats_bytes(
            (FREESURFER / "aseg.stats").read_bytes(),
            (FREESURFER / "lh.aparc.stats").read_bytes(),
            (FREESURFER / "rh.aparc.stats").read_bytes(),
        )
        assert parsed.aseg.icv_mm3 == 1500000.0
        assert parsed.lh_aparc is not None and parsed.rh_aparc is not None

    def test_aseg_only(self):
        parsed = parse_stats_bytes((FREESURFER / "aseg.stats").read_bytes())
        assert parsed.lh_aparc is None and parsed.rh_aparc is None

    def test_one_hemisphere_is_rejected(self):
        with pytest.raises(StatsParseError, match="both lh_aparc and rh_aparc, or neither"):
            parse_stats_bytes(
                (FREESURFER / "aseg.stats").read_bytes(),
                (FREESURFER / "lh.aparc.stats").read_bytes(),
                None,
            )

    def test_empty_aseg_upload_points_at_the_vinn_file(self):
        with pytest.raises(StatsParseError, match="aseg\\+DKT.VINN.stats"):
            parse_stats_bytes(b"")

    def test_bytes_that_are_not_text_do_not_crash(self):
        with pytest.raises(StatsParseError):
            parse_stats_bytes(b"\xff\xfe\x00\x01" * 64)
