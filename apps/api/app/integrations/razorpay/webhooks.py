"""Razorpay webhook verification and event mapping.

Everything here is written against the current Razorpay documentation rather
than guessed, per spec section 13. The facts this module depends on:

* Signature arrives in the ``X-Razorpay-Signature`` header, computed as
  **HMAC-SHA256 over the raw request body** keyed by the webhook secret. The
  docs are explicit: *"Do not parse or cast the webhook request body"* before
  verifying — re-serialising JSON changes the bytes and the signature fails.
* Delivery is **at-least-once**, so the same event can arrive repeatedly. The
  ``x-razorpay-event-id`` header is unique per event and is the documented
  dedup key.
* The endpoint must return **2xx within 5 seconds** or the delivery counts as
  failed; Razorpay then retries with exponential backoff for 24 hours and
  disables the webhook if it keeps failing. So the handler stores and returns —
  it does not do the work inline.
* Payload shape is ``{entity, account_id, event, contains, payload, created_at}``
  with entities nested at ``payload.<entity>.entity``.
* All amounts are in the smallest currency unit (paise for INR).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from app.domain.enums import FailureCategory, SourceType

__all__ = [
    "FailureCategory",
    "ParsedEvent",
    "SignatureError",
    "parse_event",
    "redact_payload",
    "verify_signature",
]

SIGNATURE_HEADER = "X-Razorpay-Signature"
EVENT_ID_HEADER = "x-razorpay-event-id"

# --- Documented event names -------------------------------------------------
# Payments
EVENT_PAYMENT_FAILED = "payment.failed"
EVENT_PAYMENT_CAPTURED = "payment.captured"
EVENT_PAYMENT_AUTHORIZED = "payment.authorized"
# Payment Links
EVENT_LINK_PAID = "payment_link.paid"
EVENT_LINK_PARTIALLY_PAID = "payment_link.partially_paid"
EVENT_LINK_CANCELLED = "payment_link.cancelled"
EVENT_LINK_EXPIRED = "payment_link.expired"
# Subscriptions
EVENT_SUBSCRIPTION_CHARGED = "subscription.charged"
EVENT_SUBSCRIPTION_PENDING = "subscription.pending"
EVENT_SUBSCRIPTION_HALTED = "subscription.halted"

#: Events that mean money arrived.
RECOVERY_EVENTS: frozenset[str] = frozenset(
    {EVENT_LINK_PAID, EVENT_PAYMENT_CAPTURED, EVENT_SUBSCRIPTION_CHARGED}
)

#: Events that mean revenue is newly at risk.
FAILURE_EVENTS: frozenset[str] = frozenset(
    {EVENT_PAYMENT_FAILED, EVENT_SUBSCRIPTION_PENDING, EVENT_SUBSCRIPTION_HALTED}
)

#: Every event we act on. Anything else is stored and ignored, not rejected --
#: an unrecognised event is not an error, and refusing it would make Razorpay
#: retry it for 24 hours.
HANDLED_EVENTS: frozenset[str] = RECOVERY_EVENTS | FAILURE_EVENTS


class SignatureError(Exception):
    """Raised when a webhook signature cannot be verified."""


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Verify ``X-Razorpay-Signature`` against the raw body.

    Implemented directly rather than through the SDK helper so the security
    property is visible in our own code and testable without the SDK: HMAC-SHA256
    over the exact bytes received, compared in constant time.

    ``hmac.compare_digest`` matters — a plain ``==`` on a hex digest leaks
    timing information that can be used to forge a signature byte by byte.
    """
    if not secret:
        raise SignatureError("No webhook secret configured; refusing to accept unverified events")
    if not signature:
        return False

    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@dataclass(frozen=True, slots=True)
class ParsedEvent:
    """A webhook reduced to the facts the domain cares about."""

    event_type: str
    #: Provider id of the entity the event concerns, used to find our case.
    external_id: str | None
    #: Reference we set when creating the link, echoed back by Razorpay.
    reference_id: str | None
    amount_paise: int | None
    source_type: SourceType | None
    failure_category: FailureCategory | None
    failure_reason_code: str | None
    is_recovery: bool
    is_failure: bool

    @property
    def is_handled(self) -> bool:
        return self.event_type in HANDLED_EVENTS


def _entity(payload: dict[str, Any], name: str) -> dict[str, Any]:
    """Read ``payload.<name>.entity``, the documented nesting."""
    container = payload.get("payload") or {}
    wrapper = container.get(name) or {}
    entity = wrapper.get("entity") or {}
    return entity if isinstance(entity, dict) else {}


#: Razorpay error codes and descriptions vary; the reason string is the most
#: informative field. Mapped to our own categories so the policy engine keys off
#: a stable vocabulary rather than provider text (spec FR-2).
_REASON_TO_CATEGORY: dict[str, FailureCategory] = {
    "insufficient_funds": FailureCategory.INSUFFICIENT_FUNDS,
    "payment_failed": FailureCategory.ISSUER_DECLINED,
    "card_expired": FailureCategory.CARD_EXPIRED,
    "incorrect_otp": FailureCategory.AUTHENTICATION_FAILED,
    "auth_failure": FailureCategory.AUTHENTICATION_FAILED,
    "gateway_error": FailureCategory.TECHNICAL_ERROR,
    "server_error": FailureCategory.TECHNICAL_ERROR,
    "network_error": FailureCategory.NETWORK_TIMEOUT,
    "payment_limit_exceeded": FailureCategory.LIMIT_EXCEEDED,
    "invalid_mandate": FailureCategory.MANDATE_REVOKED,
}


def classify_failure(entity: dict[str, Any]) -> tuple[FailureCategory, str | None]:
    """Map a failed payment entity to our failure vocabulary.

    Uses ``error_reason`` first (most specific), then ``error_code``, then the
    description. Falls back to UNKNOWN rather than guessing — an unrecognised
    reason routes to a human, which is the safe default.
    """
    reason = str(entity.get("error_reason") or "").lower()
    code = str(entity.get("error_code") or "").lower()
    description = str(entity.get("error_description") or "").lower()

    for key, category in _REASON_TO_CATEGORY.items():
        if key in reason or key in code or key in description:
            return category, str(entity.get("error_reason") or entity.get("error_code") or "")

    return FailureCategory.UNKNOWN, str(
        entity.get("error_reason") or entity.get("error_code") or ""
    ) or None


def parse_event(body: dict[str, Any]) -> ParsedEvent:
    """Reduce a verified webhook body to a :class:`ParsedEvent`.

    Tolerant by design: a missing field yields ``None`` rather than raising, so
    a payload shape we did not anticipate is stored and ignored instead of
    causing a non-2xx that Razorpay would retry for 24 hours.
    """
    event_type = str(body.get("event") or "")

    payment = _entity(body, "payment")
    link = _entity(body, "payment_link")
    subscription = _entity(body, "subscription")

    external_id: str | None = None
    reference_id: str | None = None
    amount: int | None = None
    source: SourceType | None = None
    category: FailureCategory | None = None
    reason_code: str | None = None

    if event_type.startswith("payment_link."):
        external_id = link.get("id")
        reference_id = link.get("reference_id")
        # amount_paid is the documented field for what actually arrived.
        amount = link.get("amount_paid") or link.get("amount")
        source = SourceType.PAYMENT
    elif event_type.startswith("subscription."):
        external_id = subscription.get("id")
        amount = payment.get("amount") or subscription.get("quantity")
        source = SourceType.SUBSCRIPTION
        if event_type in {EVENT_SUBSCRIPTION_PENDING, EVENT_SUBSCRIPTION_HALTED}:
            category, reason_code = (
                (FailureCategory.MANDATE_REVOKED, "subscription_halted")
                if event_type == EVENT_SUBSCRIPTION_HALTED
                else classify_failure(payment)
            )
    elif event_type.startswith("payment."):
        external_id = payment.get("id")
        amount = payment.get("amount")
        source = SourceType.PAYMENT
        if event_type == EVENT_PAYMENT_FAILED:
            category, reason_code = classify_failure(payment)

    return ParsedEvent(
        event_type=event_type,
        external_id=str(external_id) if external_id else None,
        reference_id=str(reference_id) if reference_id else None,
        amount_paise=int(amount) if isinstance(amount, int | float) else None,
        source_type=source,
        failure_category=category,
        failure_reason_code=reason_code or None,
        is_recovery=event_type in RECOVERY_EVENTS,
        is_failure=event_type in FAILURE_EVENTS,
    )


#: Header names and any field whose value must never reach storage or logs.
_SENSITIVE_KEYS = frozenset(
    {
        "x-razorpay-signature",
        "authorization",
        "card",
        "token",
        "vpa",
        "email",
        "contact",
        "customer_email",
        "customer_contact",
    }
)


def redact_payload(value: Any) -> Any:
    """Strip PII and credentials from a webhook body before it is stored.

    Razorpay payment entities carry ``email``, ``contact`` and card details.
    None of that is needed to recover a payment, and the spec forbids storing
    real card data or letting PII reach logs.
    """
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if str(key).lower() in _SENSITIVE_KEYS else redact_payload(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    return value
