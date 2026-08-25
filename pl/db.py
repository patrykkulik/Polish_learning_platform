"""Engine and session factory.

Postgres is the deployment target, and the schema is shaped for it. SQLite is
supported so the learning loop runs locally with no daemon: every column type
here is portable, and the one CHECK constraint is written in portable SQL rather
than with `num_nonnulls`.

Set DATABASE_URL to point at Postgres:

    DATABASE_URL=postgresql+psycopg://polish:polish@localhost:5432/polish
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_SQLITE = f"sqlite:///{Path(__file__).resolve().parent.parent / 'polish.db'}"

DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_SQLITE)

engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def session() -> Session:
    return SessionLocal()


def create_all() -> None:
    """Create the schema.

    The design's M1 checklist calls for an Alembic migration. That is deferred
    until the schema stops moving — a migration authored against a schema still
    being shaped is a migration that gets rewritten. Recorded as debt, not as a
    decision that the schema does not need versioning.
    """
    from pl import models

    models.Base.metadata.create_all(engine)
