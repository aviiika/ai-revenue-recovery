"""Health and readiness endpoints (spec NFR Observability)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.schemas import HealthResponse
from app.core.config import Settings, get_settings

router = APIRouter(tags=["system"])


@router.get("/api/v1/integrations/health")
def integration_health(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    """Integration status panel (spec Milestone 5).

    Reports configuration, never credentials. ``configured: false`` is a normal
    state, not an error -- the system falls back to the simulated provider.
    """
    return {
        "razorpay": {
            "mode": settings.razorpay_mode,
            "api_configured": bool(settings.razorpay_key_id and settings.razorpay_key_secret),
            "webhook_secret_configured": bool(settings.razorpay_webhook_secret),
            "webhook_path": "/api/v1/webhooks/razorpay",
            "live_mode_blocked": True,
            "note": (
                "Test mode only. Without credentials the agent executes through the "
                "simulated provider and the demo still runs end to end."
            ),
        },
        "llm": {"configured": settings.llm_enabled, "provider": settings.llm_provider},
        "demo_endpoints_enabled": settings.demo_endpoints_enabled,
    }


@router.get("/health", response_model=HealthResponse)
def health(
    response: Response,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HealthResponse:
    """Liveness plus dependency status.

    Reports degraded rather than failing outright when the database is
    unreachable: the spec requires the system to degrade visibly instead of
    disappearing.
    """
    database_status = "up"
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        database_status = "down"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="ok" if database_status == "up" else "degraded",
        app_env=settings.app_env,
        database=database_status,
        razorpay_mode=settings.razorpay_mode,
        llm_enabled=settings.llm_enabled,
        demo_endpoints_enabled=settings.demo_endpoints_enabled,
    )
