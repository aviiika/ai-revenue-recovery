"""Razorpay Payment Links adapter (spec section 13, FR-5).

Written against the documented API rather than guessed:

* ``POST /v1/payment_links`` — required ``amount`` (smallest currency unit, so
  paise for INR) and ``currency``; optional ``description``, ``customer``,
  ``notify``, ``reminder_enable``, ``notes``, ``callback_url``, ``expire_by``
  (unix timestamp), ``reference_id``. Response carries ``id``, ``short_url``
  and ``status``.
* ``GET /v1/payment_links/{id}`` — fetch.
* ``POST /v1/payment_links/{id}/cancel`` — cancel.

Authentication is HTTP Basic with key id as username and key secret as
password. We use ``httpx`` directly rather than the SDK: the surface we need is
three endpoints, and a thin explicit client keeps the request shape visible and
mockable without a network.

**Test mode only.** The settings validator refuses to boot unless
``RAZORPAY_MODE=test``, and this adapter refuses to construct without
credentials, so it cannot silently no-op against production.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.config import Settings
from app.db.models import RecoveryCase
from app.domain.enums import InterventionStrategy
from app.domain.interventions.service import ProviderError

logger = logging.getLogger(__name__)

API_BASE = "https://api.razorpay.com/v1"

#: Razorpay requires a 2xx within 5 seconds on *inbound* webhooks; for our
#: outbound calls we set a bounded timeout so a slow provider cannot pin a
#: worker (spec NFR: external API failures use bounded retries).
DEFAULT_TIMEOUT_SECONDS = 10.0

#: How long a recovery link stays payable. Long enough for a customer to act,
#: short enough that a stale link does not collect money against a case the
#: agent has since closed.
LINK_VALIDITY = timedelta(days=7)


class RazorpayError(ProviderError):
    """Raised when the Razorpay API rejects or fails a request.

    Subclasses the domain's :class:`ProviderError` so the intervention executor
    records the failure and leaves the case retryable, instead of the error
    escaping as an unhandled 500 and losing the attempt.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class RazorpayNotConfiguredError(RazorpayError):
    """Raised when credentials are absent. Callers fall back to simulation."""


@dataclass(frozen=True, slots=True)
class PaymentLink:
    """The subset of a Payment Link response we actually use."""

    id: str
    short_url: str
    status: str
    amount_paise: int
    reference_id: str | None

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> PaymentLink:
        return cls(
            id=str(data.get("id", "")),
            short_url=str(data.get("short_url", "")),
            status=str(data.get("status", "")),
            amount_paise=int(data.get("amount", 0)),
            reference_id=data.get("reference_id"),
        )


class RazorpayClient:
    """Thin, explicit client over the three Payment Link endpoints we need."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        if settings.razorpay_mode != "test":
            # Belt and braces: the settings validator already enforces this, but
            # the adapter is the thing that would actually move money.
            raise RazorpayError(
                f"Refusing to build a Razorpay client in mode {settings.razorpay_mode!r}; "
                "this project is test-mode only"
            )
        if not settings.razorpay_key_id or not settings.razorpay_key_secret:
            raise RazorpayNotConfiguredError("RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are not set")

        self._auth = (settings.razorpay_key_id, settings.razorpay_key_secret)
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, f"{API_BASE}{path}", auth=self._auth, **kwargs)
        except httpx.HTTPError as exc:
            # Network failure. Named exception, not bare — and the caller
            # degrades to simulation rather than losing the case.
            raise RazorpayError(f"Razorpay request failed: {exc}") from exc

        if response.status_code >= 400:
            # Never log the response body wholesale: it can echo request fields.
            raise RazorpayError(
                f"Razorpay {method} {path} returned {response.status_code}",
                status_code=response.status_code,
            )
        body = response.json()
        return body if isinstance(body, dict) else {}

    def create_payment_link(
        self,
        *,
        amount_paise: int,
        reference_id: str,
        description: str,
        currency: str = "INR",
        callback_url: str | None = None,
        notes: dict[str, str] | None = None,
        now: datetime | None = None,
    ) -> PaymentLink:
        """Create a payment link for the outstanding amount.

        ``reference_id`` carries our intervention's idempotency key, so the
        ``payment_link.paid`` webhook can be tied back to the exact attempt that
        created it without a lookup table.

        ``notify`` is set to send nothing: this project does not contact real
        customers (spec section 2.3), and Razorpay would otherwise email or SMS
        the address on the link.
        """
        payload: dict[str, Any] = {
            "amount": amount_paise,
            "currency": currency,
            "description": description[:255],
            "reference_id": reference_id,
            "expire_by": int(((now or datetime.now(UTC)) + LINK_VALIDITY).timestamp()),
            # Explicitly suppress provider-side notification.
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "notes": notes or {},
        }
        if callback_url:
            payload["callback_url"] = callback_url

        logger.info(
            "razorpay_create_payment_link",
            extra={"reference_id": reference_id, "amount_paise": amount_paise},
        )
        return PaymentLink.from_response(self._request("POST", "/payment_links", json=payload))

    def fetch_payment_link(self, link_id: str) -> PaymentLink:
        return PaymentLink.from_response(self._request("GET", f"/payment_links/{link_id}"))

    def cancel_payment_link(self, link_id: str) -> PaymentLink:
        return PaymentLink.from_response(self._request("POST", f"/payment_links/{link_id}/cancel"))

    def close(self) -> None:
        self._client.close()


class RazorpayProvider:
    """Execution adapter satisfying the domain's ``PaymentProvider`` Protocol.

    Only ``CREATE_PAYMENT_LINK`` actually reaches Razorpay. The other strategies
    are customer messaging, which this project deliberately simulates rather
    than sending (spec FR-5: "simulation-first unless a compliant messaging
    provider is deliberately integrated").
    """

    def __init__(self, client: RazorpayClient, callback_url: str | None = None) -> None:
        self._client = client
        self._callback_url = callback_url

    @property
    def channel(self) -> str:
        return "RAZORPAY_TEST"

    def execute(
        self,
        *,
        strategy: InterventionStrategy,
        case: RecoveryCase,
        idempotency_key: str,
    ) -> dict[str, object]:
        if strategy is not InterventionStrategy.CREATE_PAYMENT_LINK:
            return {
                "provider": "razorpay",
                "simulated": True,
                "strategy": str(strategy),
                "note": (
                    "Only CREATE_PAYMENT_LINK is executed against Razorpay; "
                    "customer messaging is simulated in this project."
                ),
            }

        link = self._client.create_payment_link(
            amount_paise=case.amount_at_risk_paise,
            reference_id=idempotency_key,
            description=f"Recovery for {case.source_external_id}",
            currency=case.currency,
            callback_url=self._callback_url,
            notes={
                "case_id": str(case.id),
                "source_external_id": case.source_external_id,
                "synthetic": str(case.is_synthetic).lower(),
            },
        )
        return {
            "provider": "razorpay",
            "mode": "test",
            "simulated": False,
            "strategy": str(strategy),
            "payment_link_id": link.id,
            "short_url": link.short_url,
            "status": link.status,
            "reference_id": link.reference_id,
        }


def build_provider(settings: Settings) -> RazorpayProvider | None:
    """Construct the live provider, or ``None`` when unconfigured.

    Returning ``None`` rather than raising is deliberate: a missing key is the
    normal state of a fresh clone, and the orchestrator falls back to the
    simulator so the demo still runs end to end (spec section 19).
    """
    try:
        return RazorpayProvider(RazorpayClient(settings))
    except RazorpayNotConfiguredError:
        logger.info("razorpay_not_configured", extra={"action": "using simulated provider"})
        return None
