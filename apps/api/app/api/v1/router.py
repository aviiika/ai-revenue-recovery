"""v1 API router assembly."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import cases, demo, metrics

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(cases.router)
api_router.include_router(demo.router)
api_router.include_router(metrics.router)
