"""CLI entrypoint: `uv run python scripts/import_cohort.py --help`

Imports a research cohort (OASIS-1) from a biomarker CSV, or from a folder
of FreeSurfer stats directories plus the OASIS demographics spreadsheet.
The logic lives in app/cli/import_cohort.py and app/services/cohort_import.py
so that it is type-checked, tested and available inside the Docker image.
"""

from app.cli.import_cohort import main

if __name__ == "__main__":
    raise SystemExit(main())
