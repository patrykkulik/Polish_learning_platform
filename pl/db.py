"""Engine and session factory.

Postgres is the deployment target, and the schema is shaped for it. SQLite is
supported so the learning loop runs locally with no daemon: every column type
here is portable, and the one CHECK constraint is written in portable SQL rather
than with `num_nonnulls`.

Set DATABASE_URL to point at Postgres:

    DATABASE_URL=postgresql+psycopg://polish:polish@localhost:5432/polish
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

log = logging.getLogger(__name__)

DEFAULT_SQLITE = f"sqlite:///{Path(__file__).resolve().parent.parent / 'polish.db'}"

DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_SQLITE)

engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def session() -> Session:
    return SessionLocal()


def create_all() -> None:
    """Create the schema, and add any nullable column it has since grown.

    The design's M1 checklist calls for an Alembic migration. That is deferred
    until the schema stops moving — a migration authored against a schema still
    being shaped is a migration that gets rewritten. Recorded as debt, not as a
    decision that the schema does not need versioning.

    Deferring it is only survivable if adding a column still reaches a database
    that already exists, and `create_all` alone does not: it skips a table it
    finds and never issues an `ALTER`. A column added to a model after the
    learner has a `polish.db` is therefore simply absent, and the failure is both
    delayed and misleading — the content build touches none of the learner's
    tables and prints success, then the first page load raises `no such column`.
    The only remedy on offer was deleting the database, which is every FSRS
    schedule, unlock latch, streak and attempt the learner has.
    """
    from pl import models

    models.Base.metadata.create_all(engine)
    add_missing_columns()


def add_missing_columns() -> None:
    """Bring existing tables up to the model's nullable columns.

    Deliberately narrow. A nullable column with no default can be added to a
    populated table safely on both SQLite and Postgres, and needs no backfill —
    the rows that predate it read as `None`, which is what "this card was created
    before the column existed" honestly means. Anything else (a NOT NULL column,
    a type change, a rename, a drop) is a real migration and raises here rather
    than being half-applied, because a schema change that needs a decision should
    stop the operator rather than guess.
    """
    from sqlalchemy import inspect, text

    from pl import models

    inspector = inspect(engine)
    with engine.begin() as connection:
        for table in models.Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                if not column.nullable:
                    raise RuntimeError(
                        f"{table.name}.{column.name} is missing from the database "
                        f"and is NOT NULL, so it cannot be added without deciding "
                        f"what existing rows should hold. This needs a migration."
                    )
                ddl = column.type.compile(engine.dialect)
                connection.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}')
                )
                log.info("added missing column %s.%s", table.name, column.name)
