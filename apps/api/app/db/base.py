"""SQLAlchemy declarative base and shared column types."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, mapped_column
from sqlalchemy.types import JSON

# Explicit naming convention so Alembic autogenerate produces stable,
# reviewable migration names instead of database-assigned ones.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: JSONB on Postgres (indexable, the spec's choice); plain JSON elsewhere so the
#: models remain importable under SQLite for fast unit tests.
JSONBType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def uuid_pk() -> uuid.UUID:
    return uuid.uuid4()


def created_at_column() -> object:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def utcnow() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)
