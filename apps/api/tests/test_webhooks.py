"""Razorpay webhook tests (spec section 19).

The spec names four webhook failure modes that must be demonstrated: duplicate
delivery, invalid signature, provider API failure, and a stale queued action.
The first two live here; the others are covered in test_orchestrator.

Payload shapes below follow the documented structure —
``{entity, account_id, event, contains, payload, created_at}`` with entities at
``payload.<name>.entity``, and amounts in paise.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import Settings, get_settings
from app.core.money import Money
from app.db.models import Merchant, RecoveryCase, WebhookEvent
from app.domain.cases import service as cases
from app.domain.cases.service import NewCaseInput
from app.domain.enums import CaseState, FailureCategory, SourceType, WebhookStatus
from app.integrations.razorpay import webhooks as razorpay
from app.main import create_app

WEBHOOK_SECRET = "test_webhook_secret_do_not_use"
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client(session: Session, merchant: Merchant) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_settings] = lambda: Settings(
        razorpay_mode="test",
        razorpay_webhook_secret=WEBHOOK_SECRET,
        demo_endpoints_enabled=True,
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def post_webhook(
    client: TestClient,
    body: dict,
    *,
    event_id: str = "evt_test_0001",
    signature: str | None = None,
) -> object:
    raw = json.dumps(body).encode()
    return client.post(
        "/api/v1/webhooks/razorpay",
        content=raw,
        headers={
            "Content-Type": "application/json",
            razorpay.SIGNATURE_HEADER: signature if signature is not None else sign(raw),
            razorpay.EVENT_ID_HEADER: event_id,
        },
    )


def payment_failed_body(payment_id: str = "pay_test_failed_001", amount: int = 250000) -> dict:
    return {
        "entity": "event",
        "account_id": "acc_test",
        "event": "payment.failed",
        "contains": ["payment"],
        "created_at": 1788000000,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": amount,
                    "currency": "INR",
                    "status": "failed",
                    "method": "card",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "Payment failed due to insufficient funds",
                    "error_source": "bank",
                    "error_step": "payment_authorization",
                    "error_reason": "insufficient_funds",
                    "email": "someone@example.com",
                    "contact": "+919999999999",
                }
            }
        },
    }


def link_paid_body(reference_id: str, amount: int = 250000) -> dict:
    return {
        "entity": "event",
        "account_id": "acc_test",
        "event": "payment_link.paid",
        "contains": ["payment_link", "payment", "order"],
        "created_at": 1788000100,
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_test_001",
                    "entity": "payment_link",
                    "amount": amount,
                    "amount_paid": amount,
                    "currency": "INR",
                    "status": "paid",
                    "reference_id": reference_id,
                }
            },
            "payment": {"entity": {"id": "pay_link_001", "amount": amount}},
        },
    }


# --- Signature verification -------------------------------------------------


def test_valid_signature_is_accepted(client: TestClient) -> None:
    response = post_webhook(client, payment_failed_body())
    assert response.status_code == 200
    assert response.json()["accepted"] is True


def test_invalid_signature_is_rejected(client: TestClient) -> None:
    """Spec section 19: an invalid webhook must be refused."""
    response = post_webhook(client, payment_failed_body(), signature="deadbeef")
    assert response.status_code == 400
    assert response.json()["accepted"] is False


def test_missing_signature_is_rejected(client: TestClient) -> None:
    response = post_webhook(client, payment_failed_body(), signature="")
    assert response.status_code == 400


def test_rejected_event_is_still_recorded(client: TestClient, session: Session) -> None:
    """A forged delivery is evidence. It must be stored, not silently dropped."""
    post_webhook(client, payment_failed_body(), signature="deadbeef")

    record = session.query(WebhookEvent).one()
    assert record.signature_valid is False
    assert record.processing_status == WebhookStatus.INVALID


def test_signature_is_computed_over_the_raw_body() -> None:
    """The docs are explicit that the body must not be parsed before signing.

    Re-serialising changes whitespace and key order, so a signature computed
    over a round-tripped body will not match the one Razorpay sent.
    """
    original = b'{"event":"payment.failed","payload":{}}'
    signature = sign(original)
    assert razorpay.verify_signature(original, signature, WEBHOOK_SECRET) is True

    round_tripped = json.dumps(json.loads(original)).encode()
    assert round_tripped != original
    assert razorpay.verify_signature(round_tripped, signature, WEBHOOK_SECRET) is False


def test_verification_refuses_when_no_secret_is_configured() -> None:
    with pytest.raises(razorpay.SignatureError):
        razorpay.verify_signature(b"{}", "abc", "")


def test_endpoint_refuses_events_when_secret_is_unset(session: Session, merchant: Merchant) -> None:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_settings] = lambda: Settings(
        razorpay_mode="test", razorpay_webhook_secret=""
    )
    with TestClient(app) as unconfigured:
        response = post_webhook(unconfigured, payment_failed_body())
    assert response.status_code == 503
    app.dependency_overrides.clear()


# --- Duplicate delivery (at-least-once semantics) ---------------------------


def test_duplicate_event_id_is_acknowledged_but_not_reprocessed(
    client: TestClient, session: Session
) -> None:
    """Razorpay delivers at-least-once, so this is expected traffic."""
    body = payment_failed_body()
    first = post_webhook(client, body, event_id="evt_dup_001")
    second = post_webhook(client, body, event_id="evt_dup_001")

    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    # Both must be 2xx, or Razorpay retries for 24 hours.
    assert second.status_code == 200
    assert session.query(WebhookEvent).count() == 1


def test_duplicate_recovery_event_does_not_double_count_revenue(
    client: TestClient, session: Session, merchant: Merchant
) -> None:
    """The money-critical case: redelivery must not inflate recovered revenue."""
    case = cases.ingest(
        session,
        NewCaseInput(
            merchant_id=merchant.id,
            source_type=SourceType.PAYMENT,
            source_external_id="pay_wh_recover_001",
            amount_at_risk=Money.from_rupees("2500.00"),
            detected_at=NOW,
            failure_category=FailureCategory.NETWORK_TIMEOUT,
        ),
    )
    session.flush()

    body = link_paid_body(reference_id="pay_wh_recover_001")
    # reference_id has no matching intervention, so it falls back to
    # source_external_id -- exercised deliberately.
    body["payload"]["payment_link"]["entity"]["reference_id"] = None
    body["payload"]["payment_link"]["entity"]["id"] = "pay_wh_recover_001"

    post_webhook(client, body, event_id="evt_recover_001")
    first_total = case.recovered_amount_paise

    post_webhook(client, body, event_id="evt_recover_002")  # different id, same money
    assert case.recovered_amount_paise == first_total
    assert case.current_state == CaseState.RECOVERED


# --- Event handling ---------------------------------------------------------


def test_payment_failed_ingests_a_new_case(
    client: TestClient, session: Session, merchant: Merchant
) -> None:
    response = post_webhook(client, payment_failed_body(amount=500000))
    assert response.status_code == 200

    case = (
        session.query(RecoveryCase)
        .filter(RecoveryCase.source_external_id == "pay_test_failed_001")
        .one()
    )
    assert case.amount_at_risk_paise == 500000
    assert case.failure_category == FailureCategory.INSUFFICIENT_FUNDS
    # Real provider events are not synthetic and must survive a demo reset.
    assert case.is_synthetic is False


def test_unhandled_event_is_ignored_with_a_2xx(client: TestClient) -> None:
    """Refusing an unknown event would make Razorpay retry it for 24 hours."""
    body = {"entity": "event", "event": "payment.downtime.started", "payload": {}}
    response = post_webhook(client, body, event_id="evt_unknown_001")

    assert response.status_code == 200
    assert "ignored" in response.json()["action"]


def test_malformed_but_signed_body_is_rejected_not_crashed(
    client: TestClient,
) -> None:
    raw = b"this is not json"
    response = client.post(
        "/api/v1/webhooks/razorpay",
        content=raw,
        headers={
            razorpay.SIGNATURE_HEADER: sign(raw),
            razorpay.EVENT_ID_HEADER: "evt_malformed_001",
        },
    )
    assert response.status_code == 400


def test_missing_event_id_header_still_deduplicates(client: TestClient, session: Session) -> None:
    """Without the header we hash the body, so replays are still caught."""
    raw = json.dumps(payment_failed_body()).encode()
    headers = {
        "Content-Type": "application/json",
        razorpay.SIGNATURE_HEADER: sign(raw),
    }
    client.post("/api/v1/webhooks/razorpay", content=raw, headers=headers)
    second = client.post("/api/v1/webhooks/razorpay", content=raw, headers=headers)

    assert second.json()["duplicate"] is True
    assert session.query(WebhookEvent).count() == 1


# --- Redaction --------------------------------------------------------------


def test_pii_is_redacted_before_storage(client: TestClient, session: Session) -> None:
    """Razorpay payment entities carry email and contact. Neither is needed to
    recover a payment, and the spec forbids letting PII reach storage or logs."""
    post_webhook(client, payment_failed_body(), event_id="evt_pii_001")

    record = session.query(WebhookEvent).one()
    entity = record.payload["payload"]["payment"]["entity"]
    assert entity["email"] == "[REDACTED]"
    assert entity["contact"] == "[REDACTED]"
    # Non-sensitive fields survive, or the record would be useless.
    assert entity["error_reason"] == "insufficient_funds"


# --- Parsing ----------------------------------------------------------------


def test_parse_reads_documented_nested_paths() -> None:
    parsed = razorpay.parse_event(link_paid_body(reference_id="case:abc:attempt:1"))
    assert parsed.event_type == "payment_link.paid"
    assert parsed.reference_id == "case:abc:attempt:1"
    assert parsed.amount_paise == 250000
    assert parsed.is_recovery is True


def test_parse_maps_subscription_charged_as_recovery() -> None:
    body = {
        "entity": "event",
        "event": "subscription.charged",
        "payload": {
            "subscription": {"entity": {"id": "sub_001"}},
            "payment": {"entity": {"id": "pay_001", "amount": 99900}},
        },
    }
    parsed = razorpay.parse_event(body)
    assert parsed.is_recovery is True
    assert parsed.source_type == SourceType.SUBSCRIPTION
    assert parsed.amount_paise == 99900


def test_parse_maps_subscription_halted_to_mandate_revoked() -> None:
    body = {
        "entity": "event",
        "event": "subscription.halted",
        "payload": {"subscription": {"entity": {"id": "sub_002"}}, "payment": {"entity": {}}},
    }
    parsed = razorpay.parse_event(body)
    assert parsed.is_failure is True
    assert parsed.failure_category == FailureCategory.MANDATE_REVOKED


def test_unrecognised_failure_reason_falls_back_to_unknown() -> None:
    """An unmapped reason must route to a human, not be guessed at."""
    category, _ = razorpay.classify_failure({"error_reason": "some_new_reason_2027"})
    assert category == FailureCategory.UNKNOWN


def test_parse_tolerates_a_payload_shape_we_did_not_anticipate() -> None:
    parsed = razorpay.parse_event({"event": "payment.failed"})
    assert parsed.external_id is None
    assert parsed.amount_paise is None


# --- Payment Links adapter --------------------------------------------------


def test_adapter_refuses_to_build_without_credentials() -> None:
    """A missing key is a normal state, surfaced as a typed error the caller
    turns into a simulated fallback -- not a crash."""
    from app.integrations.razorpay.adapter import RazorpayClient, RazorpayNotConfiguredError

    with pytest.raises(RazorpayNotConfiguredError):
        RazorpayClient(Settings(razorpay_mode="test", razorpay_key_id="", razorpay_key_secret=""))


def test_build_provider_returns_none_when_unconfigured() -> None:
    from app.integrations.razorpay.adapter import build_provider

    assert build_provider(Settings(razorpay_mode="test")) is None


def test_payment_link_request_matches_the_documented_shape(
    session: Session, merchant: Merchant
) -> None:
    """Amount in paise, reference_id carrying our idempotency key, and provider
    notifications explicitly suppressed."""
    import httpx

    from app.domain.enums import InterventionStrategy
    from app.integrations.razorpay.adapter import RazorpayClient, RazorpayProvider

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "id": "plink_abc",
                "short_url": "https://rzp.io/i/abc",
                "status": "created",
                "amount": 250000,
                "reference_id": captured["body"]["reference_id"],
            },
        )

    case = cases.ingest(
        session,
        NewCaseInput(
            merchant_id=merchant.id,
            source_type=SourceType.PAYMENT,
            source_external_id="pay_link_shape_001",
            amount_at_risk=Money.from_rupees("2500.00"),
            detected_at=NOW,
            failure_category=FailureCategory.INSUFFICIENT_FUNDS,
        ),
    )

    client = RazorpayClient(
        Settings(razorpay_mode="test", razorpay_key_id="rzp_test_x", razorpay_key_secret="s"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = RazorpayProvider(client).execute(
        strategy=InterventionStrategy.CREATE_PAYMENT_LINK,
        case=case,
        idempotency_key="case:abc:attempt:1",
    )

    assert captured["url"].endswith("/v1/payment_links")
    assert captured["body"]["amount"] == 250000  # paise, not rupees
    assert captured["body"]["currency"] == "INR"
    assert captured["body"]["reference_id"] == "case:abc:attempt:1"
    # We must never make Razorpay contact a customer on our behalf.
    assert captured["body"]["notify"] == {"sms": False, "email": False}
    assert captured["body"]["reminder_enable"] is False
    assert captured["auth"], "requests must be authenticated"

    assert result["payment_link_id"] == "plink_abc"
    assert result["simulated"] is False


def test_adapter_only_calls_razorpay_for_payment_links(
    session: Session, merchant: Merchant
) -> None:
    """Messaging strategies are simulated; only links touch the provider."""
    import httpx

    from app.domain.enums import InterventionStrategy
    from app.integrations.razorpay.adapter import RazorpayClient, RazorpayProvider

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not call Razorpay for a messaging strategy")

    case = cases.ingest(
        session,
        NewCaseInput(
            merchant_id=merchant.id,
            source_type=SourceType.PAYMENT,
            source_external_id="pay_no_call_001",
            amount_at_risk=Money.from_rupees("100.00"),
            detected_at=NOW,
            failure_category=FailureCategory.NETWORK_TIMEOUT,
        ),
    )
    client = RazorpayClient(
        Settings(razorpay_mode="test", razorpay_key_id="k", razorpay_key_secret="s"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = RazorpayProvider(client).execute(
        strategy=InterventionStrategy.SEND_REMINDER_SIMULATED,
        case=case,
        idempotency_key="case:x:attempt:1",
    )
    assert result["simulated"] is True


def test_provider_api_failure_surfaces_as_a_typed_error(
    session: Session, merchant: Merchant
) -> None:
    """Spec section 19: a Razorpay API failure must degrade, not crash."""
    import httpx

    from app.integrations.razorpay.adapter import RazorpayClient, RazorpayError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"description": "server error"}})

    client = RazorpayClient(
        Settings(razorpay_mode="test", razorpay_key_id="k", razorpay_key_secret="s"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RazorpayError) as exc:
        client.create_payment_link(amount_paise=1000, reference_id="r", description="d")
    assert exc.value.status_code == 500


def test_integration_health_reports_configuration_not_credentials(
    client: TestClient,
) -> None:
    body = client.get("/api/v1/integrations/health").json()
    assert body["razorpay"]["mode"] == "test"
    assert body["razorpay"]["live_mode_blocked"] is True
    assert body["razorpay"]["webhook_secret_configured"] is True
    # No secret value may appear anywhere in the response.
    assert WEBHOOK_SECRET not in json.dumps(body)
