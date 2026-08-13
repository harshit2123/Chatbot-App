"""Shared test-database helper.

Each suite gets its own database so counts stay deterministic when suites run
together. The database is created on demand: a suite that skips because its
database happens not to exist is a suite that has silently stopped protecting
anything, which is worse than a hard failure.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text

ADMIN_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/postgres"
BASE_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432"


def url_for(database: str) -> str:
    return f"{BASE_URL}/{database}"


def reset_schema(database_url: str) -> None:
    """Drop and recreate the schema via Alembic.

    Migrations rather than `create_all()`, so the tests exercise the same DDL
    path production uses — a migration that fails to apply should fail the test
    suite, not just the deploy.
    """
    from alembic import command
    from alembic.config import Config

    backend_dir = Path(__file__).resolve().parent.parent

    engine = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))

    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def ensure_database(database: str) -> bool:
    """Create `database` if absent. Returns False only if Postgres is unreachable."""
    try:
        admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            exists = conn.execute(
                text("select 1 from pg_database where datname = :name"), {"name": database}
            ).scalar()
            if not exists:
                # Identifier cannot be parameterized; the name is a test-local
                # constant, never user input.
                conn.execute(text(f'create database "{database}"'))
        return True
    except Exception:
        return False
