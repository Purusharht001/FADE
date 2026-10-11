"""Runs the real Alembic migrations against a throwaway SQLite file.

The rest of the suite builds its schema with `Base.metadata.create_all`,
which would hide a migration that disagrees with the models or that
mishandles rows which already exist. These tests are synchronous on
purpose: Alembic's env.py drives its own event loop with `asyncio.run`.
"""

import datetime
import sqlite3
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect

import app.models  # noqa: F401 - registers every mapped class on Base.metadata
from alembic import command
from app.core.config import get_settings
from app.db.base import Base

BACKEND_DIR = Path(__file__).resolve().parents[1]
INITIAL_REVISION = "d65864356d55"
PROVENANCE_REVISION = "002753d4af4c"
ENGINE_CONTRACT_REVISION = "87e388644744"


@pytest.fixture
def alembic_db(tmp_path, monkeypatch):
    """(alembic Config, path to an empty SQLite file the migrations target)."""
    db_path = tmp_path / "migrate.db"
    monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    # Built without alembic.ini so env.py skips logging.config.fileConfig,
    # which would otherwise disable every logger for the rest of the session.
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg, db_path


def insert_legacy_cohort(db_path: Path) -> dict[str, str]:
    """Rows shaped the way the pre-provenance seed wrote them: phantom
    scans on patients stamped with real-cohort labels. Returns patient ids
    by role; `scan:<role>` keys hold the matching scan ids.
    """
    now = datetime.datetime.now(datetime.UTC).isoformat()
    ids = {name: uuid.uuid4().hex for name in ("seeded", "failed_upload", "no_scans")}
    con = sqlite3.connect(db_path)
    with con:
        for name, source in (
            ("seeded", "OASIS"),
            ("failed_upload", "CLINIC"),
            ("no_scans", "ADNI"),
        ):
            con.execute(
                "INSERT INTO patients (id, display_id, age, sex, source, created_at, updated_at) "
                "VALUES (?, ?, 70, 'F', ?, ?, ?)",
                (ids[name], f"PT-{name}", source, now, now),
            )
        for name, status in (("seeded", "COMPLETED"), ("failed_upload", "FAILED")):
            scan_id = uuid.uuid4().hex
            ids[f"scan:{name}"] = scan_id
            con.execute(
                "INSERT INTO scans (id, patient_id, scan_date, modality, status, created_at, "
                "updated_at) VALUES (?, ?, '2026-01-06', 'T1w', ?, ?, ?)",
                (scan_id, ids[name], status, now, now),
            )
    con.close()
    return ids


class TestProvenanceMigration:
    def test_existing_rows_are_relabelled(self, alembic_db):
        cfg, db_path = alembic_db
        command.upgrade(cfg, INITIAL_REVISION)
        ids = insert_legacy_cohort(db_path)

        command.upgrade(cfg, PROVENANCE_REVISION)

        con = sqlite3.connect(db_path)
        sources = dict(con.execute("SELECT id, source FROM patients").fetchall())
        scans = con.execute(
            "SELECT status, extraction_method, calibration_version, engine_version, "
            "reference_label FROM scans ORDER BY status"
        ).fetchall()
        con.close()

        # A completed scan could only have come from the phantom.
        assert sources[ids["seeded"]] == "PHANTOM"
        # No completed scan, so nothing proves these are phantom patients.
        assert sources[ids["failed_upload"]] == "CLINIC"
        assert sources[ids["no_scans"]] == "ADNI"

        assert scans == [
            ("COMPLETED", "INHOUSE_PHANTOM", "phantom.v0", "1.0.0", None),
            ("FAILED", "INHOUSE_PHANTOM", None, None, None),
        ]

    def test_downgrade_removes_the_provenance_columns(self, alembic_db):
        cfg, db_path = alembic_db
        command.upgrade(cfg, PROVENANCE_REVISION)
        command.downgrade(cfg, INITIAL_REVISION)

        con = sqlite3.connect(db_path)
        columns = {row[1] for row in con.execute("PRAGMA table_info(scans)")}
        con.close()
        assert "extraction_method" not in columns
        assert "reference_label" not in columns
        assert "failure_reason" in columns


def _insert_fis_result(db_path: Path, scan_id: str, *, stage: str, fired_rules: str) -> str:
    now = datetime.datetime.now(datetime.UTC).isoformat()
    result_id = uuid.uuid4().hex
    con = sqlite3.connect(db_path)
    with con:
        con.execute(
            "INSERT INTO fis_results (id, scan_id, stage, confidence, uncertainty, needs_review, "
            "membership, fired_rules, created_at, updated_at) "
            "VALUES (?, ?, ?, 33.3, 100.0, 1, ?, ?, ?, ?)",
            (result_id, scan_id, stage, '{"CN": 0.3333}', fired_rules, now, now),
        )
        con.execute(
            "INSERT INTO biomarker_readings (id, scan_id, key, value, unit, abnormality, "
            "created_at, updated_at) VALUES (?, ?, 'HIPPOCAMPAL_VOLUME', 4.0, 'mL', 0.0, ?, ?)",
            (uuid.uuid4().hex, scan_id, now, now),
        )
    con.close()
    return result_id


class TestEngineContractMigration:
    def test_existing_results_are_stamped_and_no_rule_results_corrected(self, alembic_db):
        cfg, db_path = alembic_db
        command.upgrade(cfg, PROVENANCE_REVISION)
        ids = insert_legacy_cohort(db_path)
        staged = _insert_fis_result(
            db_path, ids["scan:seeded"], stage="AD", fired_rules='[{"id": "R3"}]'
        )
        # C4: no rule fired, yet the old tie-break stored CN.
        fallback = _insert_fis_result(
            db_path, ids["scan:failed_upload"], stage="CN", fired_rules="[]"
        )

        command.upgrade(cfg, ENGINE_CONTRACT_REVISION)

        con = sqlite3.connect(db_path)
        rows = {
            row[0]: row[1:]
            for row in con.execute(
                "SELECT id, stage, determinate, indeterminate_reason, needs_review, severity, "
                "calibration_version, confidence, uncertainty FROM fis_results"
            )
        }
        abnormalities = [r[0] for r in con.execute("SELECT abnormality FROM biomarker_readings")]
        con.close()

        assert rows[staged] == ("AD", 1, None, 1, None, "phantom.v0", 33.3, 100.0)
        assert rows[fallback] == (None, 0, "no_rule_fired", 1, None, "phantom.v0", 0.0, 100.0)
        assert abnormalities == [0.0, 0.0]

    def test_downgrade_drops_what_the_old_schema_cannot_hold(self, alembic_db):
        cfg, db_path = alembic_db
        command.upgrade(cfg, PROVENANCE_REVISION)
        ids = insert_legacy_cohort(db_path)
        _insert_fis_result(db_path, ids["scan:seeded"], stage="AD", fired_rules='[{"id": "R3"}]')
        _insert_fis_result(db_path, ids["scan:failed_upload"], stage="CN", fired_rules="[]")
        command.upgrade(cfg, ENGINE_CONTRACT_REVISION)

        command.downgrade(cfg, PROVENANCE_REVISION)

        con = sqlite3.connect(db_path)
        stages = [r[0] for r in con.execute("SELECT stage FROM fis_results")]
        columns = {row[1]: row for row in con.execute("PRAGMA table_info(fis_results)")}
        scans = con.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
        con.close()
        # The indeterminate row is removed rather than given an invented stage.
        assert stages == ["AD"]
        assert "determinate" not in columns
        assert columns["stage"][3] == 1  # NOT NULL again
        assert scans == 2


class TestMigrationsMatchModels:
    def test_head_schema_has_every_model_column(self, alembic_db):
        """`alembic upgrade head` on an empty database must produce every
        table and column the ORM models declare; otherwise a Postgres
        deployment (which never calls create_all) is missing schema.
        """
        cfg, db_path = alembic_db
        command.upgrade(cfg, "head")

        engine = create_engine(f"sqlite:///{db_path.as_posix()}")
        inspector = inspect(engine)
        try:
            for table in Base.metadata.sorted_tables:
                assert inspector.has_table(table.name), f"missing table {table.name}"
                migrated = {c["name"]: c for c in inspector.get_columns(table.name)}
                for column in table.columns:
                    assert column.name in migrated, f"missing {table.name}.{column.name}"
                    assert bool(migrated[column.name]["nullable"]) == bool(column.nullable), (
                        f"nullability differs for {table.name}.{column.name}"
                    )
        finally:
            engine.dispose()
