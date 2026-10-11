"""Re-score stored scans under the active calibration.

    uv run python scripts/rescore.py --dry-run
    uv run python scripts/rescore.py --source OASIS

Run this after CALIBRATION_FILE changes. Stored results keep the
calibration they were computed with until they are re-scored, and the
cohort import skips subjects it has already imported, so this is how a new
calibration reaches data that is already in the database.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from app.core.logging import configure_logging
from app.db.session import dispose_engine, get_session_factory
from app.models.enums import DataSource
from app.services.calibration import CalibrationError
from app.services.rescore import RescoreSummary, rescore_all

EXIT_OK = 0
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rescore",
        description="Re-run the fuzzy engine on stored scans with the active calibration file.",
    )
    parser.add_argument(
        "--source",
        choices=[s.value for s in DataSource],
        help="Only re-score scans of patients with this data source.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print the summary, then roll back. Nothing is written.",
    )
    parser.add_argument("--verbose", action="store_true", help="Show application logs.")
    return parser


async def _run(args: argparse.Namespace) -> RescoreSummary:
    try:
        async with get_session_factory()() as session:
            return await rescore_all(
                session,
                source=DataSource(args.source) if args.source else None,
                dry_run=args.dry_run,
            )
    finally:
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()
    if not args.verbose:
        logging.getLogger().setLevel(logging.WARNING)
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    try:
        summary = asyncio.run(_run(args))
    except CalibrationError as exc:
        print(f"Re-score failed: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(summary.format())
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
