"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import health
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging, correlation_id_var, new_correlation_id
from app.core.money import MoneyError
from app.domain.cases.state_machine import IllegalTransitionError

logger = logging.getLogger(__name__)

CORRELATION_HEADER = "X-Correlation-ID"


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="AI Revenue Recovery Agent",
        version="0.1.0",
        description=(
            "Detects revenue at risk, diagnoses it, selects a bounded recovery "
            "intervention, measures money recovered, and keeps a full audit trail. "
            "Demo data is synthetic and labelled as such."
        ),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def correlation_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Attach a correlation ID to every request and every log line it emits.

        Honours an inbound header so a webhook delivery can be traced from the
        provider's id all the way to the audit trail.
        """
        correlation_id = request.headers.get(CORRELATION_HEADER) or new_correlation_id()
        token = correlation_id_var.set(correlation_id)
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        response.headers[CORRELATION_HEADER] = correlation_id
        return response

    @app.exception_handler(IllegalTransitionError)
    async def illegal_transition_handler(
        _request: Request, exc: IllegalTransitionError
    ) -> JSONResponse:
        """A rejected state change is a client error, not a server fault."""
        return JSONResponse(
            status_code=409,
            content={
                "error": "illegal_state_transition",
                "detail": str(exc),
                "source_state": str(exc.source),
                "attempted_target": str(exc.target),
            },
        )

    @app.exception_handler(MoneyError)
    async def money_error_handler(_request: Request, exc: MoneyError) -> JSONResponse:
        return JSONResponse(
            status_code=422, content={"error": "invalid_money_value", "detail": str(exc)}
        )

    app.include_router(health.router)
    app.include_router(api_router)

    logger.info(
        "application_started",
        extra={
            "app_env": settings.app_env,
            "razorpay_mode": settings.razorpay_mode,
            "llm_enabled": settings.llm_enabled,
            "demo_endpoints_enabled": settings.demo_endpoints_enabled,
        },
    )
    return app


app = create_app()
