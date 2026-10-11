"""Phase 2d: the cohort import.

The fixture cohort (tests/fixtures/cohort/cohort.csv) is hand-written with
invented subjects:

    FAKE_0001_MR1  CDR 0     imported
    FAKE_0002_MR1  CDR 0.5   imported
    FAKE_0003_MR1  CDR blank skipped, no label
    FAKE_0004_MR1  CDR 1     imported
    FAKE_0004_MR2  CDR 1     skipped, reliability rescan
    FAKE_0005_MR1  CDR "NaN" skipped, no label
    FAKE_0006_MR1  CDR 0     imported, QC: ICV out of range
    FAKE_0007_MR1  CDR 0.5   imported, QC: hipp_norm is a fraction, not per-mille
    FAKE_0008_MR1  CDR 2     invalid: sex is "X"
"""

import logging
import math
import shutil
from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.cli import import_cohort as cli
from app.models.enums import DataSource, ExtractionMethod, ScanStatus, Sex
from app.models.patient import Patient
from app.models.scan import Scan
from app.services.cohort_import import (
    CohortImportError,
    ImportOptions,
    import_csv,
    import_stats_folder,
    is_missing,
    is_mr2,
    parse_cdr,
    read_demographics,
    screen_csv,
)

FIXTURES = Path(__file__).parent / "fixtures"
COHORT_CSV = FIXTURES / "cohort" / "cohort.csv"
FREESURFER = FIXTURES / "freesurfer"
LEGACY = FIXTURES / "freesurfer_legacy"


async def _scans_by_subject(db) -> dict[str, Scan]:
    scans = (await db.execute(select(Scan))).scalars().all()
    return {s.external_subject_id: s for s in scans}


def _write_csv(path: Path, *rows: str, header: str | None = None) -> Path:
    header = header or "subject_id,cdr,age,sex,icv_mm3,hipp_mm3,hipp_norm,vbr,cortex_gm_norm"
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


class TestCdrParsing:
    @pytest.mark.parametrize("cell", [None, "", "  ", "NaN", "nan", "N/A", "NA", math.nan, "-"])
    def test_missing_cdr_is_none_never_zero(self, cell):
        """The guard against the old `fillna(0.0)`: an empty CDR cell must
        come back as None, which is not equal to 0.0.
        """
        cdr = parse_cdr(cell)
        assert cdr is None
        assert cdr != 0.0

    @pytest.mark.parametrize(
        ("cell", "expected"),
        [("0", 0.0), (0, 0.0), ("0.0", 0.0), ("0.5", 0.5), (1, 1.0), ("2", 2.0), (3.0, 3.0)],
    )
    def test_real_scores_parse(self, cell, expected):
        assert parse_cdr(cell) == expected

    def test_a_real_zero_is_still_zero(self):
        """Zero is a label when the cell says zero."""
        assert parse_cdr("0") == 0.0
        assert parse_cdr("0") is not None

    @pytest.mark.parametrize("cell", ["0.25", "4", "-1", "mild", "1.5"])
    def test_values_that_are_not_cdr_scores_are_errors(self, cell):
        with pytest.raises(ValueError, match="cdr"):
            parse_cdr(cell)

    def test_is_missing(self):
        assert is_missing(None) and is_missing("") and is_missing(math.nan)
        assert not is_missing(0) and not is_missing("0") and not is_missing(0.0)


class TestMr2:
    @pytest.mark.parametrize("subject", ["OAS1_0001_MR2", "FAKE_0004_MR2", "x_mr2"])
    def test_rescans_are_recognised(self, subject):
        assert is_mr2(subject)

    @pytest.mark.parametrize("subject", ["OAS1_0001_MR1", "OAS1_MR2_0001", "MR2"])
    def test_other_sessions_are_not(self, subject):
        assert not is_mr2(subject)


class TestCsvImport:
    async def test_summary_counts(self, db_session):
        summary = await import_csv(db_session, COHORT_CSV, ImportOptions())

        assert summary.imported == 5
        assert summary.skipped_no_label == 2
        assert summary.skipped_mr2 == 1
        assert summary.skipped_invalid == 1
        assert summary.skipped_existing == 0
        assert summary.qc_flagged == 2
        assert summary.no_label_ids == ["FAKE_0003_MR1", "FAKE_0005_MR1"]
        assert summary.invalid == [("FAKE_0008_MR1", "sex is not M or F")]

    async def test_rows_without_a_cdr_are_skipped_not_labelled_healthy(self, db_session):
        """The regression this import exists to prevent. A previous script
        filled missing CDRs with 0.0 and invented "healthy" labels.
        """
        await import_csv(db_session, COHORT_CSV, ImportOptions())
        scans = await _scans_by_subject(db_session)

        # The unlabelled subjects were not imported at all.
        assert "FAKE_0003_MR1" not in scans
        assert "FAKE_0005_MR1" not in scans

        # Every stored label is exactly the one in the file.
        assert {subject: scan.reference_label for subject, scan in scans.items()} == {
            "FAKE_0001_MR1": 0.0,
            "FAKE_0002_MR1": 0.5,
            "FAKE_0004_MR1": 1.0,
            "FAKE_0006_MR1": 0.0,
            "FAKE_0007_MR1": 0.5,
        }
        # Two rows say CDR 0, so exactly two scans carry 0. Not four.
        assert sum(1 for s in scans.values() if s.reference_label == 0.0) == 2
        assert all(s.reference_label is not None for s in scans.values())

    async def test_an_all_unlabelled_file_imports_nothing(self, db_session, tmp_path):
        path = _write_csv(
            tmp_path / "unlabelled.csv",
            "FAKE_0101_MR1,,25,F,1500000,8000,5.3333,0.02,300",
            "FAKE_0102_MR1,NaN,31,M,1500000,8000,5.3333,0.02,300",
            "FAKE_0103_MR1,N/A,44,F,1500000,8000,5.3333,0.02,300",
        )
        summary = await import_csv(db_session, path, ImportOptions())

        assert summary.imported == 0
        assert summary.skipped_no_label == 3
        assert (await db_session.execute(select(Scan))).scalars().all() == []
        assert (await db_session.execute(select(Patient))).scalars().all() == []

    async def test_patients_and_scans_are_created_as_oasis(self, db_session):
        await import_csv(db_session, COHORT_CSV, ImportOptions())
        patients = (await db_session.execute(select(Patient))).scalars().all()
        scans = await _scans_by_subject(db_session)

        assert len(patients) == 5
        assert {p.source for p in patients} == {DataSource.OASIS}
        first = next(p for p in patients if p.id == scans["FAKE_0001_MR1"].patient_id)
        assert (first.age, first.sex) == (74, Sex.F)
        assert {s.extraction_method for s in scans.values()} == {ExtractionMethod.MANUAL_BIOMARKERS}
        assert {s.status for s in scans.values()} == {ScanStatus.COMPLETED}

    async def test_biomarkers_and_raw_volumes_are_stored(self, db_session):
        await import_csv(db_session, COHORT_CSV, ImportOptions())
        scans = await _scans_by_subject(db_session)
        scan = scans["FAKE_0002_MR1"]
        await db_session.refresh(scan, attribute_names=["biomarkers", "fis_result"])

        readings = {r.key.value: (r.value, r.unit) for r in scan.biomarkers}
        assert readings == {
            "hippocampal_volume": (4.0, "‰ICV"),
            "ventricle_brain_ratio": (0.041, "ratio"),
        }
        assert scan.raw_volumes == {
            "supplied_as": "cohort_csv",
            "icv_mm3": 1600000.0,
            "hippocampus_total_mm3": 6400.0,
            "cortical_gm_volume_norm": 270.1,
        }
        # No thickness column, and only the phantom calibration: not staged.
        assert scan.fis_result.determinate is False
        assert scan.fis_result.stage is None

    async def test_qc_flags_are_recorded_per_scan(self, db_session):
        await import_csv(db_session, COHORT_CSV, ImportOptions())
        scans = await _scans_by_subject(db_session)

        assert [f["code"] for f in scans["FAKE_0006_MR1"].qc_flags] == ["icv_out_of_range"]
        assert [f["code"] for f in scans["FAKE_0007_MR1"].qc_flags] == [
            "hippocampal_norm_inconsistent"
        ]
        assert scans["FAKE_0001_MR1"].qc_flags == []

    async def test_import_is_idempotent(self, db_session):
        first = await import_csv(db_session, COHORT_CSV, ImportOptions())
        second = await import_csv(db_session, COHORT_CSV, ImportOptions())

        assert first.imported == 5
        assert second.imported == 0
        assert second.skipped_existing == 5
        assert len((await db_session.execute(select(Scan))).scalars().all()) == 5
        assert len((await db_session.execute(select(Patient))).scalars().all()) == 5

    async def test_duplicate_rows_in_one_file_import_once(self, db_session, tmp_path):
        row = "FAKE_0201_MR1,0,70,F,1500000,8000,5.3333,0.02,300"
        summary = await import_csv(
            db_session, _write_csv(tmp_path / "dup.csv", row, row), ImportOptions()
        )
        assert (summary.imported, summary.skipped_existing) == (1, 1)

    async def test_dry_run_writes_nothing_and_reports_the_same_counts(self, db_session):
        dry = await import_csv(db_session, COHORT_CSV, ImportOptions(dry_run=True))

        assert dry.dry_run is True
        assert (dry.imported, dry.skipped_no_label, dry.skipped_mr2, dry.qc_flagged) == (5, 2, 1, 2)
        assert (await db_session.execute(select(Scan))).scalars().all() == []
        assert (await db_session.execute(select(Patient))).scalars().all() == []

        real = await import_csv(db_session, COHORT_CSV, ImportOptions())
        assert real.imported == dry.imported

    async def test_require_label_aborts_before_writing(self, db_session):
        with pytest.raises(CohortImportError, match="--require-label: 2 row"):
            await import_csv(db_session, COHORT_CSV, ImportOptions(require_label=True))
        assert (await db_session.execute(select(Scan))).scalars().all() == []

    async def test_require_label_passes_when_every_row_is_labelled(self, db_session, tmp_path):
        path = _write_csv(
            tmp_path / "labelled.csv", "FAKE_0301_MR1,0,70,F,1500000,8000,5.3333,0.02,300"
        )
        summary = await import_csv(db_session, path, ImportOptions(require_label=True))
        assert summary.imported == 1

    async def test_mr2_sessions_can_be_included(self, db_session):
        summary = await import_csv(db_session, COHORT_CSV, ImportOptions(include_mr2=True))
        assert summary.skipped_mr2 == 0
        assert summary.imported == 6
        assert "FAKE_0004_MR2" in await _scans_by_subject(db_session)

    async def test_optional_thickness_column(self, db_session, tmp_path):
        path = _write_csv(
            tmp_path / "thick.csv",
            "FAKE_0401_MR1,0,70,F,1500000,8000,5.3333,0.02,300,2.9",
            header=(
                "subject_id,cdr,age,sex,icv_mm3,hipp_mm3,hipp_norm,vbr,cortex_gm_norm,"
                "cortical_thickness_mm"
            ),
        )
        await import_csv(db_session, path, ImportOptions())
        scan = (await _scans_by_subject(db_session))["FAKE_0401_MR1"]
        await db_session.refresh(scan, attribute_names=["biomarkers"])
        assert {r.key.value: r.value for r in scan.biomarkers}["cortical_thickness"] == 2.9

    async def test_hipp_norm_is_computed_when_only_raw_volumes_are_given(
        self, db_session, tmp_path
    ):
        path = _write_csv(tmp_path / "raw.csv", "FAKE_0501_MR1,0,70,F,1500000,8000,,0.02,300")
        await import_csv(db_session, path, ImportOptions())
        scan = (await _scans_by_subject(db_session))["FAKE_0501_MR1"]
        await db_session.refresh(scan, attribute_names=["biomarkers"])
        values = {r.key.value: r.value for r in scan.biomarkers}
        assert values["hippocampal_volume"] == pytest.approx(8000 / 1_500_000 * 1000)

    @pytest.mark.parametrize(
        ("row", "reason"),
        [
            ("FAKE_0601_MR1,0.25,70,F,1500000,8000,5.3,0.02,300", "cdr is not one of"),
            ("FAKE_0601_MR1,mild,70,F,1500000,8000,5.3,0.02,300", "cdr is not a number"),
            ("FAKE_0601_MR1,0,,F,1500000,8000,5.3,0.02,300", "age is missing"),
            ("FAKE_0601_MR1,0,abc,F,1500000,8000,5.3,0.02,300", "age is not a number"),
            ("FAKE_0601_MR1,0,70,,1500000,8000,5.3,0.02,300", "sex is missing"),
            ("FAKE_0601_MR1,0,70,F,big,8000,5.3,0.02,300", "icv_mm3 is not a number"),
            ("FAKE_0601_MR1,0,70,F,1500000,-8000,5.3,0.02,300", "hipp_mm3 must be greater"),
            ("FAKE_0601_MR1,0,70,F,1500000,8000,5.3,-0.02,300", "vbr must not be negative"),
            ("FAKE_0601_MR1,0,70,F,,,,,", "neither hipp_norm nor vbr"),
            ("John Smith,0,70,F,1500000,8000,5.3,0.02,300", "subject_id has an invalid shape"),
            (",0,70,F,1500000,8000,5.3,0.02,300", "subject_id is missing"),
        ],
    )
    async def test_invalid_rows_are_counted_with_a_reason(self, db_session, tmp_path, row, reason):
        summary = await import_csv(
            db_session, _write_csv(tmp_path / "bad.csv", row), ImportOptions()
        )
        assert summary.imported == 0
        assert summary.skipped_invalid == 1
        assert reason in summary.invalid[0][1]

    async def test_invalid_row_reason_does_not_quote_the_cell(self, db_session, tmp_path):
        path = _write_csv(tmp_path / "bad.csv", "Jane Q Public,0,70,F,1500000,8000,5.3,0.02,300")
        summary = await import_csv(db_session, path, ImportOptions())
        assert "Jane" not in summary.format()

    async def test_missing_required_column_fails_the_whole_import(self, db_session, tmp_path):
        path = _write_csv(tmp_path / "nocdr.csv", "FAKE_1,70,F", header="subject_id,age,sex")
        with pytest.raises(CohortImportError, match="missing the column.*cdr"):
            await import_csv(db_session, path, ImportOptions())

    async def test_phantom_source_is_refused(self, db_session):
        with pytest.raises(CohortImportError, match="PHANTOM"):
            await import_csv(db_session, COHORT_CSV, ImportOptions(source=DataSource.PHANTOM))

    def test_screening_makes_no_database_writes(self):
        screening = screen_csv(COHORT_CSV, ImportOptions())
        assert [c.subject_id for c in screening.candidates] == [
            "FAKE_0001_MR1",
            "FAKE_0002_MR1",
            "FAKE_0004_MR1",
            "FAKE_0006_MR1",
            "FAKE_0007_MR1",
        ]


def _write_demographics(path: Path, rows: list[tuple]) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["ID", "M/F", "Hand", "Age", "Educ", "SES", "MMSE", "CDR", "eTIV", "nWBV"])
    for row in rows:
        sheet.append(list(row))
    workbook.save(path)
    return path


def _make_subject(root: Path, subject_id: str, source: Path, *, nested: bool = True) -> None:
    target = root / subject_id / "stats" if nested else root / subject_id
    target.mkdir(parents=True)
    for file in source.iterdir():
        shutil.copy(file, target / file.name)


@pytest.fixture
def stats_cohort(tmp_path) -> tuple[Path, Path]:
    """A stats folder for five invented subjects plus their demographics sheet."""
    root = tmp_path / "subjects"
    root.mkdir()
    _make_subject(root, "FAKE_1001_MR1", FREESURFER)
    _make_subject(root, "FAKE_1002_MR1", LEGACY, nested=False)
    _make_subject(root, "FAKE_1003_MR1", FREESURFER)  # no CDR in the sheet
    _make_subject(root, "FAKE_1001_MR2", FREESURFER)  # rescan
    _make_subject(root, "FAKE_1004_MR1", FREESURFER)  # not in the sheet
    (root / "FAKE_1005_MR1" / "stats").mkdir(parents=True)  # no stats files
    (root / "notes.txt").write_text("not a subject directory", encoding="utf-8")

    sheet = _write_demographics(
        tmp_path / "oasis_cross-sectional.xlsx",
        [
            ("FAKE_1001_MR1", "F", "R", 74, 2, 3, 29, 0, 1344, 0.743),
            ("FAKE_1002_MR1", "M", "R", 80, 4, 1, 22, 1, 1500, 0.700),
            ("FAKE_1003_MR1", "F", "R", 28, None, None, None, None, 1588, 0.803),
            ("FAKE_1001_MR2", "F", "R", 74, 2, 3, 29, 0, 1344, 0.743),
            ("FAKE_1005_MR1", "M", "R", 71, 3, 2, 27, 0.5, 1400, 0.720),
        ],
    )
    return root, sheet


class TestStatsFolderImport:
    async def test_summary_counts(self, db_session, stats_cohort):
        root, sheet = stats_cohort
        summary = await import_stats_folder(db_session, root, sheet, ImportOptions())

        assert summary.imported == 2
        assert summary.skipped_no_label == 1
        assert summary.skipped_mr2 == 1
        assert summary.skipped_invalid == 2
        assert dict(summary.invalid) == {
            "FAKE_1004_MR1": "no row for this subject in the demographics sheet",
            "FAKE_1005_MR1": (
                "No aseg stats file was found (looked for aseg.stats, aseg+DKT.stats, "
                "aseg+DKT.VINN.stats)."
            ),
        }

    async def test_unlabelled_subject_is_skipped_not_labelled_healthy(
        self, db_session, stats_cohort
    ):
        root, sheet = stats_cohort
        summary = await import_stats_folder(db_session, root, sheet, ImportOptions())
        scans = await _scans_by_subject(db_session)

        assert summary.no_label_ids == ["FAKE_1003_MR1"]
        assert "FAKE_1003_MR1" not in scans
        assert {s: scan.reference_label for s, scan in scans.items()} == {
            "FAKE_1001_MR1": 0.0,
            "FAKE_1002_MR1": 1.0,
        }

    async def test_stats_are_parsed_and_derived(self, db_session, stats_cohort):
        root, sheet = stats_cohort
        await import_stats_folder(db_session, root, sheet, ImportOptions())
        scans = await _scans_by_subject(db_session)

        full = scans["FAKE_1001_MR1"]
        await db_session.refresh(full, attribute_names=["biomarkers"])
        values = {r.key.value: r.value for r in full.biomarkers}
        assert values["hippocampal_volume"] == pytest.approx(5.333333, abs=1e-6)
        assert values["cortical_thickness"] == pytest.approx(2.86)
        assert full.extraction_method == ExtractionMethod.FREESURFER_STATS
        assert full.raw_volumes["icv_mm3"] == 1500000.0

        # Stats placed directly in the subject directory, no stats/ level.
        flat = scans["FAKE_1002_MR1"]
        await db_session.refresh(flat, attribute_names=["biomarkers"])
        assert {r.key.value for r in flat.biomarkers} == {
            "hippocampal_volume",
            "ventricle_brain_ratio",
        }

    async def test_demographics_come_from_the_sheet(self, db_session, stats_cohort):
        root, sheet = stats_cohort
        await import_stats_folder(db_session, root, sheet, ImportOptions())
        scans = await _scans_by_subject(db_session)
        patient = await db_session.get(Patient, scans["FAKE_1002_MR1"].patient_id)
        assert (patient.age, patient.sex, patient.source) == (80, Sex.M, DataSource.OASIS)

    async def test_extraction_method_can_be_fastsurfer(self, db_session, stats_cohort):
        root, sheet = stats_cohort
        options = ImportOptions(stats_extraction_method=ExtractionMethod.FASTSURFER)
        await import_stats_folder(db_session, root, sheet, options)
        scans = await _scans_by_subject(db_session)
        assert {s.extraction_method for s in scans.values()} == {ExtractionMethod.FASTSURFER}

    def test_demographics_can_be_a_csv(self, tmp_path):
        path = tmp_path / "oasis_cross-sectional.csv"
        path.write_text(
            "ID,M/F,Hand,Age,CDR\nFAKE_1_MR1,F,R,74,0\nFAKE_2_MR1,M,R,30,\n", encoding="utf-8"
        )
        people = read_demographics(path)
        assert parse_cdr(people["FAKE_1_MR1"].cdr_cell) == 0.0
        assert parse_cdr(people["FAKE_2_MR1"].cdr_cell) is None

    def test_demographics_missing_a_column(self, tmp_path):
        path = tmp_path / "demo.csv"
        path.write_text("ID,Age\nFAKE_1_MR1,74\n", encoding="utf-8")
        with pytest.raises(CohortImportError, match="missing the column.*M/F, CDR"):
            read_demographics(path)

    async def test_missing_stats_root(self, db_session, tmp_path, stats_cohort):
        _root, sheet = stats_cohort
        with pytest.raises(CohortImportError, match="is not a directory"):
            await import_stats_folder(db_session, tmp_path / "nope", sheet, ImportOptions())


class TestCommandLine:
    """Argument handling only. The import itself is covered above; these
    tests replace it so that no real database is touched.
    """

    @pytest.fixture(autouse=True)
    def _restore_log_levels(self):
        """`main` quietens logging for a terminal; undo that after each test."""
        loggers = [logging.getLogger(), logging.getLogger("sqlalchemy.engine")]
        levels = [logger.level for logger in loggers]
        yield
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)

    @pytest.fixture
    def captured(self, monkeypatch):
        calls = {}

        async def _fake_run(args):
            calls["args"] = args
            from app.services.cohort_import import ImportSummary

            return ImportSummary(dry_run=args.dry_run, imported=3, skipped_no_label=2)

        monkeypatch.setattr(cli, "_run", _fake_run)
        monkeypatch.setattr(cli, "configure_logging", lambda: None)
        return calls

    def test_csv_import_prints_the_summary(self, captured, capsys):
        code = cli.main(["--csv", str(COHORT_CSV), "--dry-run"])
        out = capsys.readouterr().out

        assert code == 0
        assert "DRY RUN: nothing was written" in out
        for label in ("imported:", "skipped-no-label:", "skipped-MR2:", "QC-flagged:"):
            assert label in out
        assert captured["args"].dry_run is True
        assert captured["args"].require_label is False

    def test_flags_are_passed_through(self, captured):
        cli.main(["--csv", "c.csv", "--require-label", "--include-mr2", "--source", "ADNI"])
        args = captured["args"]
        assert (args.require_label, args.include_mr2, args.source) == (True, True, "ADNI")

    @pytest.mark.parametrize(
        "argv",
        [
            [],
            ["--csv", "a.csv", "--stats-dir", "d"],
            ["--stats-dir", "d"],
            ["--csv", "a.csv", "--demographics", "x.xlsx"],
            ["--csv", "a.csv", "--source", "PHANTOM"],
        ],
    )
    def test_bad_argument_combinations_exit_with_usage(self, captured, argv):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(argv)
        assert excinfo.value.code == 2
        assert "args" not in captured

    def test_import_error_gives_a_non_zero_exit(self, monkeypatch, capsys):
        async def _failing_run(args):
            raise CohortImportError("--require-label: 2 row(s) have no CDR")

        monkeypatch.setattr(cli, "_run", _failing_run)
        monkeypatch.setattr(cli, "configure_logging", lambda: None)

        code = cli.main(["--csv", "c.csv", "--require-label"])
        assert code == cli.EXIT_IMPORT_ERROR
        assert "Import failed: --require-label" in capsys.readouterr().err

    def test_summary_output_is_ascii(self):
        from app.services.cohort_import import ImportSummary

        summary = ImportSummary(imported=1, invalid=[("FAKE_1", "sex is not M or F")])
        summary.format().encode("ascii")
