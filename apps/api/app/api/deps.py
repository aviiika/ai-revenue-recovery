"""FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_session_factory


def get_db() -> Iterator[Session]:
    """Request-scoped session; commits on success, rolls back on any error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def require_demo_enabled(settings: Settings = Depends(get_settings)) -> Settings:
    """Gate for ``/api/v1/demo/*``.

    Spec section 14: "Demo endpoints must be disabled or protected outside demo
    environment." These endpoints wipe and reseed data, so the gate is a hard
    404 rather than a warning.
    """
    if not settings.demo_endpoints_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Demo endpoints are disabled. Set DEMO_ENDPOINTS_ENABLED=true to enable them.",
        )
    return settings
