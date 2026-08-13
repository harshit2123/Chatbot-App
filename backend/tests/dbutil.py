"""Shared test-database helper.

Each suite gets its own database so counts stay deterministic when suites run
together. The database is created on demand: a suite that skips because its
database happens not to exist is a suite that has silently stopped protecting
anything, which is worse than a hard failure.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text

ADMIN_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/postgres"
BASE_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432"


def url_for(database: str) -> str:
    return f"{BASE_URL}/{database}"


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
