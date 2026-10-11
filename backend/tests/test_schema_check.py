"""The startup schema check: an empty SQLite database is created and
stamped, and a database that is behind the code is refused with the
command that fixes it.
"""

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

import app.models  # noqa: F401 - registers every mapped class on Base.metadata
from alembic import command
from app.core.config import get_settings
from app.db.schema import SCHEMA_HEAD, SchemaOutOfDateError, ensure_schema, missing_from_database

BACKEND_DIR = Path(__file__).resolve().parents[1]
INITIAL_REVISION = "d65864356d55"


def _alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


@pytest.fixture
def db_path(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "app.db"
    monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{path.as_posix()}")
    return path


def _ensure(path: Path, *, create_if_empty: bool = True) -> str:
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.begin() as connection:
            return ensure_schema(connection, create_if_empty=create_if_empty)
    finally:
        engine.dispose()


class TestSchemaHead:
    def test_constant_matches_the_latest_migration(self):
        """Adding a migration without updating SCHEMA_HEAD fails here."""
        assert ScriptDirectory.from_config(_alembic_config()).get_current_head() == SCHEMA_HEAD


class TestEmptyDatabase:
    def test_sqlite_file_is_created_and_stamped(self, db_path):
        assert _ensure(db_path) == "created"

        con = sqlite3.connect(db_path)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = con.execute("SELECT version_num FROM alembic_version").fetchall()
        con.close()
        assert {"patients", "scans", "fis_results", "clinician_ratings", "audit_log"} <= tables
        assert version == [(SCHEMA_HEAD,)]

    def test_a_created_database_is_then_current(self, db_path):
        _ensure(db_path)
        assert _ensure(db_path) == "current"

    def test_alembic_accepts_a_created_database_as_up_to_date(self, db_path):
        """The point of stamping: later migrations can run against it."""
        _ensure(db_path)
        command.upgrade(_alembic_config(), "head")  # must be a no-op, not a failure
        assert _ensure(db_path) == "current"

    def test_empty_database_is_left_alone_when_creation_is_not_allowed(self, db_path):
        assert _ensure(db_path, create_if_empty=False) == "empty"
        con = sqlite3.connect(db_path)
        assert con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
        con.close()


class TestStaleDatabase:
    def test_migrated_database_is_current(self, db_path):
        command.upgrade(_alembic_config(), "head")
        assert _ensure(db_path) == "current"

    def test_database_one_migration_behind_is_refused(self, db_path):
        command.upgrade(_alembic_config(), INITIAL_REVISION)

        with pytest.raises(SchemaOutOfDateError) as excinfo:
            _ensure(db_path)
        message = str(excinfo.value)
        assert "older than this code" in message
        assert "scans.extraction_method" in message
        assert "uv run alembic upgrade head" in message

    def test_a_refused_database_is_not_half_upgraded(self, db_path):
        """The old behaviour created the new tables but not the new columns,
        after which the migrations could no longer run.
        """
        command.upgrade(_alembic_config(), INITIAL_REVISION)
        with pytest.raises(SchemaOutOfDateError):
            _ensure(db_path)

        con = sqlite3.connect(db_path)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
        assert "clinician_ratings" not in tables
        assert "audit_log" not in tables

        command.upgrade(_alembic_config(), "head")  # still possible
        assert _ensure(db_path) == "current"

    def test_unmanaged_stale_database_gets_different_advice(self, db_path):
        command.upgrade(_alembic_config(), INITIAL_REVISION)
        con = sqlite3.connect(db_path)
        with con:
            con.execute("DROP TABLE alembic_version")
        con.close()

        with pytest.raises(SchemaOutOfDateError, match="created without migrations"):
            _ensure(db_path)

    def test_missing_items_are_listed(self, db_path):
        command.upgrade(_alembic_config(), INITIAL_REVISION)
        engine = create_engine(f"sqlite:///{db_path.as_posix()}")
        try:
            with engine.connect() as connection:
                missing = missing_from_database(connection)
        finally:
            engine.dispose()
        assert "clinician_ratings" in missing
        assert "audit_log" in missing
        assert "scans.reference_label" in missing
        assert "fis_results.determinate" in missing
        assert "patients.display_id" not in missing
