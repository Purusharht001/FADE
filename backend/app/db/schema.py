"""Startup check that the database schema matches the code.

The schema now changes between releases (provenance columns, the engine
contract, the study tables). Starting the API against a database that is
one migration behind used to fail on the first query with a raw SQL
error, and on SQLite `create_all` made it worse by adding the new tables
but not the new columns, after which `alembic upgrade head` could no
longer run. So the application now looks before it starts:

* An empty SQLite database is created from the models and stamped with
  the current Alembic revision, so later migrations apply to it normally.
  This keeps the zero-configuration local start.
* A database that is missing tables or columns the models declare is
  refused, with the command that fixes it.
"""

from sqlalchemy import Connection, inspect, text

from app.db.base import Base

# The Alembic revision this code's models correspond to. A test checks it
# against the migration scripts, so adding a migration without updating
# this fails the suite.
SCHEMA_HEAD = "ef8721d5b1d9"

_ALEMBIC_TABLE = "alembic_version"


class SchemaOutOfDateError(RuntimeError):
    """The database is behind the code. Not recoverable at runtime."""


def stamp_head(connection: Connection) -> None:
    """Records SCHEMA_HEAD the way `alembic stamp head` would."""
    connection.execute(
        text(f"CREATE TABLE IF NOT EXISTS {_ALEMBIC_TABLE} (version_num VARCHAR(32) NOT NULL)")
    )
    connection.execute(text(f"DELETE FROM {_ALEMBIC_TABLE}"))  # noqa: S608 - constant table name
    connection.execute(
        text(f"INSERT INTO {_ALEMBIC_TABLE} (version_num) VALUES (:rev)"),  # noqa: S608
        {"rev": SCHEMA_HEAD},
    )


def missing_from_database(connection: Connection) -> list[str]:
    """Tables (`name`) and columns (`table.column`) the models declare that
    the database does not have.
    """
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    missing: list[str] = []
    for name, table in Base.metadata.tables.items():
        if name not in existing_tables:
            missing.append(name)
            continue
        existing_columns = {column["name"] for column in inspector.get_columns(name)}
        missing += [f"{name}.{c.name}" for c in table.columns if c.name not in existing_columns]
    return missing


def ensure_schema(connection: Connection, *, create_if_empty: bool) -> str:
    """Brings an empty database up (when allowed) and refuses a stale one.
    Returns "created", "current" or "empty".
    """
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    has_app_tables = bool(existing_tables & set(Base.metadata.tables))

    if not has_app_tables:
        if not create_if_empty:
            return "empty"
        Base.metadata.create_all(connection)
        stamp_head(connection)
        return "created"

    missing = missing_from_database(connection)
    if not missing:
        return "current"

    shown = ", ".join(missing[:8]) + (", ..." if len(missing) > 8 else "")
    if _ALEMBIC_TABLE in existing_tables:
        fix = "Run `uv run alembic upgrade head` in backend/, then start the server again."
    else:
        fix = (
            "This database was created without migrations and cannot be upgraded in place. "
            "If it holds only demo data, delete the file and re-seed; otherwise run "
            "`uv run alembic stamp <its revision>` followed by `uv run alembic upgrade head`."
        )
    raise SchemaOutOfDateError(
        f"The database schema is older than this code (missing: {shown}). {fix}"
    )
