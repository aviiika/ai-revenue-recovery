"""Structured JSON logging with correlation IDs.

Spec (NFR Observability): structured JSON logs, correlation IDs for
cases/webhooks/jobs, and audit logs kept separate from debug logs.
"""

from __future__ import annotations

import contextvars
import logging
import sys
import uuid
from typing import Any

from pythonjsonlogger.json import JsonFormatter

# Set per request/job; every log line emitted downstream carries it.
correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)

# Keys that must never reach a log line, at any nesting level.
_REDACTED_KEYS = frozenset(
    {
        "authorization",
        "x-razorpay-signature",
        "key_secret",
        "razorpay_key_secret",
        "razorpay_webhook_secret",
        "llm_api_key",
        "password",
        "token",
        "email",
        "contact",
        "phone",
    }
)
REDACTED = "[REDACTED]"


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def redact(payload: Any) -> Any:
    """Recursively strip secrets and PII from a structure before logging."""
    if isinstance(payload, dict):
        return {
            key: (REDACTED if str(key).lower() in _REDACTED_KEYS else redact(value))
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact(item) for item in payload]
    return payload


class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id_var.get()
        return True


def configure_logging(level: str = "INFO") -> None:
    """Install a JSON formatter on the root logger. Idempotent."""
    root = logging.getLogger()
    if any(getattr(h, "_revrec_configured", False) for h in root.handlers):
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(correlation_id)s %(message)s",
            rename_fields={"asctime": "ts", "levelname": "level"},
        )
    )
    handler.addFilter(_CorrelationFilter())
    handler._revrec_configured = True  # type: ignore[attr-defined]

    root.handlers = [handler]
    root.setLevel(level.upper())
