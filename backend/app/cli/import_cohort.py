"""Command line entry point for the cohort import.

    uv run python scripts/import_cohort.py --csv cohort.csv --dry-run
    uv run python scripts/import_cohort.py --stats-dir D:/oasis/subjects \\
        --demographics D:/oasis/oasis_cross-sectional.xlsx

Inside the Docker image, which does not ship `scripts/`:

    python -m app.cli.import_cohort --csv /data/cohort.csv

The database is whatever DATABASE_URL points at; run `alembic upgrade head`
first. See docs/REAL_DATA_RUNBOOK.md.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from app.core.logging import configure_logging
from app.db.session import dispose_engine, get_session_factory
from app.models.enums import DataSource, ExtractionMethod
from app.services.cohort_import import (
    CohortImportError,
    ImportOptions,
    ImportSummary,
    import_csv,
    import_stats_folder,
)

EXIT_OK = 0
EXIT_IMPORT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="import_cohort",
        description=(
            "Import a research cohort as patients and scans. Rows with no CDR are skipped "
            "and counted, never given a default label."
        ),
    )
    source = parser.add_argument_group("input (choose one)")
    source.add_argument(
        "--csv",
        type=Path,
        help=(
            "CSV with columns subject_id,cdr,age,sex,icv_mm3,hipp_mm3,hipp_norm,vbr,"
            "cortex_gm_norm and an optional cortical_thickness_mm."
        ),
    )
    source.add_argument(
        "--stats-dir",
        type=Path,
        help="Folder holding one directory per subject, each with a FreeSurfer stats/ folder.",
    )
    parser.add_argument(
        "--demographics",
        type=Path,
        help="OASIS demographics sheet (.xlsx or .csv) with ID, M/F, Age, CDR. For --stats-dir.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do everything, print the summary, then roll back. Nothing is written.",
    )
    parser.add_argument(
        "--require-label",
        action="store_true",
        help="Abort before writing anything if any row has no CDR, instead of skipping it.",
    )
    parser.add_argument(
        "--include-mr2",
        action="store_true",
        help="Also import _MR2 reliability rescans (skipped by default).",
    )
    parser.add_argument(
        "--source",
        choices=[s.value for s in DataSource if s != DataSource.PHANTOM],
        default=DataSource.OASIS.value,
        help="Data source recorded on the created patients. Default: OASIS.",
    )
    parser.add_argument(
        "--extraction-method",
        choices=[ExtractionMethod.FREESURFER_STATS.value, ExtractionMethod.FASTSURFER.value],
        default=ExtractionMethod.FREESURFER_STATS.value,
        help="How the stats under --stats-dir were produced. Default: freesurfer_stats.",
    )
    parser.add_argument("--verbose", action="store_true", help="Show application logs.")
    return parser


async def _run(args: argparse.Namespace) -> ImportSummary:
    options = ImportOptions(
        dry_run=args.dry_run,
        require_label=args.require_label,
        include_mr2=args.include_mr2,
        source=DataSource(args.source),
        stats_extraction_method=ExtractionMethod(args.extraction_method),
    )
    try:
        async with get_session_factory()() as session:
            if args.csv is not None:
                return await import_csv(session, args.csv, options)
            return await import_stats_folder(session, args.stats_dir, args.demographics, options)
    finally:
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if (args.csv is None) == (args.stats_dir is None):
        parser.error("give exactly one of --csv or --stats-dir")
    if args.stats_dir is not None and args.demographics is None:
        parser.error("--stats-dir needs --demographics")
    if args.csv is not None and args.demographics is not None:
        parser.error("--demographics is only used with --stats-dir")

    configure_logging()
    if not args.verbose:
        logging.getLogger().setLevel(logging.WARNING)
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    try:
        summary = asyncio.run(_run(args))
    except CohortImportError as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return EXIT_IMPORT_ERROR

    print(summary.format())
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
