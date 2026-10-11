"""CLI entrypoint: `uv run python scripts/rescore.py --help`

Re-runs the fuzzy engine on scans already in the database, using the
active calibration file. Run it after CALIBRATION_FILE changes. The logic
lives in app/cli/rescore.py and app/services/rescore.py; inside the Docker
image use `python -m app.cli.rescore`.
"""

from app.cli.rescore import main

if __name__ == "__main__":
    raise SystemExit(main())
