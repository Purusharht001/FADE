"""CLI entrypoint: `uv run python scripts/create_user.py --help`

Creates a user account (clinician, researcher or admin), or changes an
existing account's role. The logic lives in app/cli/create_user.py so it
is type-checked, tested and available inside the Docker image as
`python -m app.cli.create_user`.
"""

from app.cli.create_user import main

if __name__ == "__main__":
    raise SystemExit(main())
