"""Razorpay webhook endpoint (spec section 13, FR-1).

Three documented constraints shape this handler:

1. **Signature is over the raw body.** The request bytes are read before any
   JSON parsing, and verification uses those exact bytes. Parsing and
   re-serialising would change them and every signature would fail.
2. **Delivery is at-least-once.** ``x-razorpay-event-id`` is unique per event
   and is stored with a UNIQUE constraint, so a redelivery is recorded as a
   duplicate and does nothing.
3. **A 2xx is required within 5 seconds**, or Razorpay retries for 24 hours and
   eventually disables the webhook. So this endpoint stores the event and
   returns; interpretation happens in a separate, bounded step.

An invalid signature returns 400 deliberately — that is not a delivery failure
to be retried, it is a rejected request. An *unrecognised but validly signed*
event returns 200: refusing it would make Razorpay retry something we will
never handle.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import Settings, get_settings
from app.core.logging import correlation_id_var
from app.db.models import WebhookEvent
from app.domain.enums import WebhookStatus
from app.domain.webhooks import service as webhook_service
from app.integrations.razorpay import webhooks as razorpay

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/razorpay", status_code=status.HTTP_200_OK)
async def razorpay_webhook(
    request: Request,
    response: Response,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Receive, verify, deduplicate and store one Razorpay event."""
    # Raw bytes first. Nothing may parse the body before the signature is
    # checked against these exact bytes.
    raw_body = await request.body()
    signature = request.headers.get(razorpay.SIGNATURE_HEADER, "")

    # Razorpay's own unique-per-event id is the documented dedup key. When it is
    # absent (a replayed or hand-crafted delivery) a content hash stands in, so
    # dedup still holds.
    event_id = request.headers.get(razorpay.EVENT_ID_HEADER) or (
        f"sha256:{hashlib.sha256(raw_body).hexdigest()}"
    )

    correlation_id = correlation_id_var.get()

    if not settings.razorpay_webhook_secret:
        # Refusing unverified events is the safe posture: without a secret we
        # cannot tell a real event from anyone's POST.
        logger.warning("webhook_secret_missing", extra={"event_id": event_id})
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "accepted": False,
            "reason": "RAZORPAY_WEBHOOK_SECRET is not configured; events are not accepted",
        }

    signature_valid = razorpay.verify_signature(
        raw_body, signature, settings.razorpay_webhook_secret
    )

    existing = session.execute(
        select(WebhookEvent).where(
            WebhookEvent.provider == "razorpay", WebhookEvent.event_id == event_id
        )
    ).scalar_one_or_none()
    if existing is not None:
        # At-least-once delivery in action. Acknowledge so Razorpay stops
        # retrying, and do nothing else.
        logger.info("webhook_duplicate", extra={"event_id": event_id})
        return {
            "accepted": True,
            "duplicate": True,
            "event_id": event_id,
            "status": str(existing.processing_status),
        }

    body: dict[str, Any] = {}
    event_type = "unknown"
    if signature_valid:
        try:
            body = await request.json()
            event_type = str(body.get("event") or "unknown")
        except ValueError:
            signature_valid = False  # a signed body that is not JSON is not usable

    record = WebhookEvent(
        id=uuid.uuid4(),
        provider="razorpay",
        event_id=event_id,
        event_type=event_type,
        signature_valid=signature_valid,
        processing_status=(WebhookStatus.RECEIVED if signature_valid else WebhookStatus.INVALID),
        payload=razorpay.redact_payload(body),
        correlation_id=correlation_id,
        received_at=datetime.now(UTC),
    )
    session.add(record)
    session.flush()

    if not signature_valid:
        logger.warning(
            "webhook_signature_invalid",
            extra={"event_id": event_id, "event_type": event_type},
        )
        # A bad signature is a rejected request, not a delivery failure.
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"accepted": False, "reason": "signature verification failed"}

    # Processing is fast and bounded, so it runs inline and still clears the
    # 5-second budget comfortably. If it ever grows, this is the seam where it
    # moves to the Celery worker -- the record is already durable.
    result = webhook_service.process(session, record, body, now=datetime.now(UTC))

    return {
        "accepted": True,
        "duplicate": False,
        "event_id": event_id,
        "event_type": event_type,
        "status": str(record.processing_status),
        "action": result,
    }
