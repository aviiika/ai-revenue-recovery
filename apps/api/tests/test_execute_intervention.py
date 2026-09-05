"""Per-case intervention execution from the dashboard.

The execution path itself already existed inside batch orchestration; these
tests cover the endpoint that exposes it, and in particular the invariant the
whole feature turns on:

**Creating a payment link is not a recovery.** Execution moves a case to
OBSERVING. Only a payment event -- a real Razorpay webhook or the simulator --
records money against it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import Settings, get_settings
from app.core.money import Money
from app.db.models import Intervention, Merchant, RecoveryCase
from app.domain.cases import service as cases
from app.domain.cases.service import NewCaseInput
from app.domain.enums import (
    AuditEventType,
    CaseState,
    FailureCategory,
    InterventionStatus,
    SourceType,
)
from app.domain.orchestrator import service as orchestrator
from app.integrations.razorpay import webhooks as razorpay
from app.integrations.razorpay.adapter import RazorpayClient, RazorpayProvider
from app.main import create_app

WEBHOOK_SECRET = "test_webhook_secret"
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)

#: A live Razorpay Payment Link response, in the documented shape.
LIVE_LINK = {
    "id": "plink_TEST123abc",
    "short_url": "https://rzp.io/i/TESTabc123",
    "status": "created",
    "amount": 10476477,
}


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


def make_case(
    session: Session,
    merchant: Merchant,
    *,
    external_id: str = "syn_exec_0001",
    rupees: str = "104764.77",
    category: FailureCategory = FailureCategory.INSUFFICIENT_FUNDS,
    do_not_contact: bool = False,
) -> RecoveryCase:
    """Mirrors the demo case: a large insufficient-funds failure."""
    return cases.ingest(
        session,
        NewCaseInput(
            merchant_id=merchant.id,
            source_type=SourceType.SUBSCRIPTION,
            source_external_id=external_id,
            amount_at_risk=Money.from_rupees(rupees),
            detected_at=NOW,
            failure_category=category,
            do_not_contact=do_not_contact,
            is_synthetic=True,
        ),
    )


def live_razorpay_provider(captured: dict | None = None) -> RazorpayProvider:
    """A provider wired to a mock transport, exercising the real adapter."""

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["body"] = json.loads(request.content)
            captured["url"] = str(request.url)
        return httpx.Response(200, json={**LIVE_LINK, "reference_id": None})

    return RazorpayProvider(
        RazorpayClient(
            Settings(razorpay_mode="test", razorpay_key_id="rzp_test_x", razorpay_key_secret="s"),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    )


# --- Successful creation ----------------------------------------------------


def test_execute_creates_a_payment_link_and_returns_the_real_url(
    client: TestClient, session: Session, merchant: Merchant, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}
    monkeypatch.setattr(orchestrator, "default_provider", lambda: live_razorpay_provider(captured))

    case = make_case(session, merchant)
    response = client.post(f"/api/v1/cases/{case.id}/execute")
    assert response.status_code == 200
    body = response.json()

    assert body["executed"] is True
    assert body["simulated"] is False
    assert body["payment_link_url"] == LIVE_LINK["short_url"]
    assert body["payment_link_id"] == LIVE_LINK["id"]

    # The URL is the provider's, never constructed by us.
    assert captured["url"].endswith("/v1/payment_links")
    assert captured["body"]["amount"] == 10476477


def test_creating_a_link_does_not_recover_the_case(
    client: TestClient, session: Session, merchant: Merchant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single most important assertion in this file."""
    monkeypatch.setattr(orchestrator, "default_provider", live_razorpay_provider)

    case = make_case(session, merchant)
    client.post(f"/api/v1/cases/{case.id}/execute")

    assert case.current_state == CaseState.OBSERVING
    assert case.recovered_amount_paise == 0
    assert case.recovered_at is None


def test_case_detail_exposes_the_link_for_the_dashboard(
    client: TestClient, session: Session, merchant: Merchant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "default_provider", live_razorpay_provider)

    case = make_case(session, merchant)
    client.post(f"/api/v1/cases/{case.id}/execute")

    detail = client.get(f"/api/v1/cases/{case.id}").json()
    assert detail["interventions"]
    result = detail["interventions"][-1]["result"]
    assert result["short_url"] == LIVE_LINK["short_url"]
    assert result["simulated"] is False


def test_execution_is_recorded_in_the_audit_timeline(
    client: TestClient, session: Session, merchant: Merchant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "default_provider", live_razorpay_provider)

    case = make_case(session, merchant)
    client.post(f"/api/v1/cases/{case.id}/execute")

    trail = client.get(f"/api/v1/cases/{case.id}").json()["audit_trail"]
    types = [e["event_type"] for e in trail]
    assert AuditEventType.INTERVENTION_SELECTED in types
    assert AuditEventType.INTERVENTION_EXECUTED in types

    executed = next(e for e in trail if e["event_type"] == "INTERVENTION_EXECUTED")
    assert executed["payload"]["strategy"] == "CREATE_PAYMENT_LINK"


# --- Idempotency ------------------------------------------------------------


def test_double_click_does_not_create_two_links(
    client: TestClient, session: Session, merchant: Merchant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A presenter will click twice. The UNIQUE idempotency key must hold."""
    calls = {"n": 0}

    def counting_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={**LIVE_LINK, "reference_id": None})

    provider = RazorpayProvider(
        RazorpayClient(
            Settings(razorpay_mode="test", razorpay_key_id="k", razorpay_key_secret="s"),
            client=httpx.Client(transport=httpx.MockTransport(counting_handler)),
        )
    )
    monkeypatch.setattr(orchestrator, "default_provider", lambda: provider)

    case = make_case(session, merchant)
    first = client.post(f"/api/v1/cases/{case.id}/execute")
    assert first.status_code == 200
    assert first.json()["executed"] is True

    second = client.post(f"/api/v1/cases/{case.id}/execute")

    # Either the second call is refused by policy (the case has moved on) or it
    # returns without executing -- what must never happen is a second link.
    assert calls["n"] == 1
    assert session.query(Intervention).filter(Intervention.recovery_case_id == case.id).count() == 1
    if second.status_code == 200:
        assert second.json()["executed"] is False


# --- Refusals ---------------------------------------------------------------


def test_already_recovered_case_is_refused(
    client: TestClient, session: Session, merchant: Merchant
) -> None:
    case = make_case(session, merchant)
    cases.record_recovery(session, case, Money.from_rupees("104764.77"), NOW)
    session.flush()

    response = client.post(f"/api/v1/cases/{case.id}/execute")
    assert response.status_code == 409
    assert "already recovered" in response.json()["detail"].lower()


def test_stopped_case_is_refused(client: TestClient, session: Session, merchant: Merchant) -> None:
    case = make_case(session, merchant, do_not_contact=True)
    client.post(f"/api/v1/cases/{case.id}/stop", json={"reason": "operator halted"})

    response = client.post(f"/api/v1/cases/{case.id}/execute")
    assert response.status_code == 409


def test_case_policy_declines_to_act_on_is_refused_with_its_reason(
    client: TestClient, session: Session, merchant: Merchant
) -> None:
    """An opted-out customer must not get a payment link, and the operator is
    told which rule refused it rather than seeing a generic error."""
    case = make_case(session, merchant, do_not_contact=True)

    response = client.post(f"/api/v1/cases/{case.id}/execute")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "Policy did not select an executable action" in detail
    assert "R02_DO_NOT_CONTACT" in detail


def test_unknown_case_returns_404(client: TestClient) -> None:
    response = client.post("/api/v1/cases/00000000-0000-0000-0000-000000000000/execute")
    assert response.status_code == 404


# --- Provider failure -------------------------------------------------------


def test_razorpay_api_failure_is_recorded_and_does_not_lose_the_case(
    client: TestClient, session: Session, merchant: Merchant, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"description": "server error"}})

    provider = RazorpayProvider(
        RazorpayClient(
            Settings(razorpay_mode="test", razorpay_key_id="k", razorpay_key_secret="s"),
            client=httpx.Client(transport=httpx.MockTransport(failing)),
        )
    )
    monkeypatch.setattr(orchestrator, "default_provider", lambda: provider)

    case = make_case(session, merchant)
    response = client.post(f"/api/v1/cases/{case.id}/execute")

    assert response.status_code == 200
    body = response.json()
    assert body["executed"] is False
    assert body["payment_link_url"] is None

    intervention = (
        session.query(Intervention).filter(Intervention.recovery_case_id == case.id).one()
    )
    assert intervention.status == InterventionStatus.FAILED
    # The case did not advance, so it can be retried.
    assert case.current_state == CaseState.ACTION_SELECTED


# --- Simulated path still works --------------------------------------------


def test_without_credentials_execution_falls_back_to_the_simulator(
    client: TestClient, session: Session, merchant: Merchant
) -> None:
    """The synthetic demo must keep working with no Razorpay keys, and must say
    plainly that it is simulated rather than showing a fabricated URL."""
    case = make_case(session, merchant)
    response = client.post(f"/api/v1/cases/{case.id}/execute")

    assert response.status_code == 200
    body = response.json()
    assert body["executed"] is True
    assert body["simulated"] is True
    assert body["payment_link_url"] is None


# --- Payment confirms the recovery, not the link ----------------------------


def sign(body: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_payment_webhook_is_what_records_the_recovery(
    client: TestClient, session: Session, merchant: Merchant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full chain: link created -> not recovered -> payment -> recovered."""
    captured: dict = {}
    monkeypatch.setattr(orchestrator, "default_provider", lambda: live_razorpay_provider(captured))

    case = make_case(session, merchant, external_id="syn_webhook_flow")
    client.post(f"/api/v1/cases/{case.id}/execute")

    assert case.current_state == CaseState.OBSERVING
    assert case.recovered_amount_paise == 0

    # Razorpay echoes back the reference_id we set -- the intervention's
    # idempotency key -- which is how the payment ties to the exact attempt.
    reference_id = captured["body"]["reference_id"]
    body = {
        "entity": "event",
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": LIVE_LINK["id"],
                    "amount": 10476477,
                    "amount_paid": 10476477,
                    "currency": "INR",
                    "status": "paid",
                    "reference_id": reference_id,
                }
            },
            "payment": {"entity": {"id": "pay_settled", "amount": 10476477}},
        },
    }
    raw = json.dumps(body).encode()
    response = client.post(
        "/api/v1/webhooks/razorpay",
        content=raw,
        headers={
            "Content-Type": "application/json",
            razorpay.SIGNATURE_HEADER: sign(raw),
            razorpay.EVENT_ID_HEADER: "evt_flow_001",
        },
    )
    assert response.status_code == 200

    assert case.current_state == CaseState.RECOVERED
    assert case.recovered_amount_paise == 10476477

    trail = client.get(f"/api/v1/cases/{case.id}").json()["audit_trail"]
    types = [e["event_type"] for e in trail]
    assert types.index("INTERVENTION_EXECUTED") < types.index("OUTCOME_OBSERVED")
    assert "RECOVERY_RECORDED" in types


def test_reference_id_carries_the_idempotency_key(
    client: TestClient, session: Session, merchant: Merchant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is the join between a Razorpay payment and our attempt."""
    captured: dict = {}
    monkeypatch.setattr(orchestrator, "default_provider", lambda: live_razorpay_provider(captured))

    case = make_case(session, merchant)
    client.post(f"/api/v1/cases/{case.id}/execute")

    intervention = (
        session.query(Intervention).filter(Intervention.recovery_case_id == case.id).one()
    )
    assert captured["body"]["reference_id"] == intervention.idempotency_key
    assert str(case.id) in intervention.idempotency_key
