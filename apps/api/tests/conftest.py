"""Shared test fixtures.

Tests run against SQLite in-memory by default so the suite is fast and needs no
running container. Set ``TEST_DATABASE_URL`` to a Postgres URL to exercise the
real dialect (CI does this), which is what catches JSONB and constraint
behaviour that SQLite would let through.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

# Settings are read at import time by several modules, so the environment must
# be shaped before the app package is imported.
os.environ.setdefault("APP_ENV", "ci")
os.environ.setdefault("RAZORPAY_MODE", "test")
os.environ.setdefault("DEMO_ENDPOINTS_ENABLED", "true")

from app.db.base import Base
from app.db.models import Merchant


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    url = os.environ.get("TEST_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    eng = create_engine(url, connect_args=connect_args, future=True)

    if url.startswith("sqlite"):
        # SQLite ignores foreign keys unless asked; without this the cascade and
        # FK behaviour under test would not actually be exercised.
        @event.listens_for(eng, "connect")
        def _enable_fk(dbapi_connection: object, _record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session wrapped in a transaction that is rolled back after each test.

    Guarantees test isolation without recreating the schema per test.
    """
    connection = engine.connect()
    transaction = connection.begin()
    # join_transaction_mode="create_savepoint" makes session.commit() release a
    # SAVEPOINT instead of committing the outer transaction. Without it, the API
    # dependency's commit would end the test transaction and the rollback below
    # would have nothing left to undo.
    factory = sessionmaker(
        bind=connection,
        expire_on_commit=False,
        future=True,
        join_transaction_mode="create_savepoint",
    )
    db = factory()
    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def merchant(session: Session) -> Merchant:
    record = Merchant(
        id=uuid.uuid4(),
        name="Test Merchant",
        currency="INR",
        timezone="Asia/Kolkata",
        high_value_threshold_paise=2_500_000,
        policy_config={"max_automated_attempts": 3},
    )
    session.add(record)
    session.flush()
    return record


@pytest.fixture
def now() -> datetime:
    """A fixed, timezone-aware instant. Tests must not depend on wall-clock."""
    return datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
