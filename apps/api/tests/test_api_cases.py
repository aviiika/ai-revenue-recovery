"""API integration tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.db.models import Merchant
from app.main import create_app


@pytest.fixture
def client(session: Session, merchant: Merchant) -> Iterator[TestClient]:
    """App wired to the test session, so requests share the test transaction."""
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def batch_payload(**overrides: object) -> dict[str, object]:
    case: dict[str, object] = {
        "source_type": "PAYMENT",
        "source_external_id": "pay_api_0001",
        "amount_at_risk_paise": 250000,
        "detected_at": "2026-09-01T12:00:00+05:30",
        "failure_category": "NETWORK_TIMEOUT",
        "failure_reason_code": "GATEWAY_TIMEOUT",
        "is_synthetic": True,
    }
    case.update(overrides)
    return {"cases": [case]}


def test_health_reports_dependency_status(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "up"
    # The safety gate must be visible in the health payload.
    assert body["razorpay_mode"] == "test"


def test_batch_ingest_then_list(client: TestClient) -> None:
    response = client.post("/api/v1/cases/batch", json=batch_payload())
    assert response.status_code == 201
    assert response.json()["ingested"] == 1

    listing = client.get("/api/v1/cases")
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    assert body["contains_synthetic"] is True

    row = body["items"][0]
    assert row["amount_at_risk"]["paise"] == 250000
    assert row["amount_at_risk"]["formatted"] == "INR 2,500.00"
    assert row["current_state"] == "NEW"


def test_batch_ingest_is_idempotent(client: TestClient) -> None:
    """Re-posting the same batch must not double the revenue at risk."""
    client.post("/api/v1/cases/batch", json=batch_payload())
    second = client.post("/api/v1/cases/batch", json=batch_payload())

    assert second.status_code == 201
    assert second.json()["ingested"] == 0
    assert second.json()["duplicates"] == ["pay_api_0001"]
    assert client.get("/api/v1/cases").json()["total"] == 1


def test_naive_timestamp_is_rejected(client: TestClient) -> None:
    """A naive timestamp becomes a wrong timestamp across a timezone boundary,
    and cooldown windows depend on it."""
    response = client.post(
        "/api/v1/cases/batch", json=batch_payload(detected_at="2026-09-01T12:00:00")
    )
    assert response.status_code == 422
    assert "timezone-aware" in response.text


def test_non_positive_amount_is_rejected(client: TestClient) -> None:
    response = client.post("/api/v1/cases/batch", json=batch_payload(amount_at_risk_paise=0))
    assert response.status_code == 422


def test_case_detail_includes_audit_trail_and_legal_transitions(client: TestClient) -> None:
    client.post("/api/v1/cases/batch", json=batch_payload())
    case_id = client.get("/api/v1/cases").json()["items"][0]["id"]

    response = client.get(f"/api/v1/cases/{case_id}")
    assert response.status_code == 200
    body = response.json()

    assert len(body["audit_trail"]) == 1
    assert body["audit_trail"][0]["event_type"] == "CASE_INGESTED"
    assert body["audit_trail"][0]["sequence"] == 1

    # A NEW case may progress to DIAGNOSED, or be stopped/escalated/recovered
    # out of band -- but it may not skip ahead to execution.
    assert set(body["allowed_transitions"]) == {"DIAGNOSED", "ESCALATED", "RECOVERED", "STOPPED"}


def test_filters_narrow_the_result_set(client: TestClient) -> None:
    client.post("/api/v1/cases/batch", json=batch_payload())
    client.post(
        "/api/v1/cases/batch",
        json=batch_payload(
            source_external_id="sub_api_0002",
            source_type="SUBSCRIPTION",
            failure_category="MANDATE_REVOKED",
            amount_at_risk_paise=9_000_000,
        ),
    )

    assert client.get("/api/v1/cases", params={"state": "NEW"}).json()["total"] == 2
    assert client.get("/api/v1/cases", params={"source_type": "SUBSCRIPTION"}).json()["total"] == 1
    assert (
        client.get("/api/v1/cases", params={"failure_category": "MANDATE_REVOKED"}).json()["total"]
        == 1
    )
    assert client.get("/api/v1/cases", params={"min_amount_paise": 1_000_000}).json()["total"] == 1


def test_inverted_amount_filter_is_rejected(client: TestClient) -> None:
    response = client.get(
        "/api/v1/cases", params={"min_amount_paise": 900, "max_amount_paise": 100}
    )
    assert response.status_code == 422


def test_unknown_case_returns_404(client: TestClient) -> None:
    response = client.get("/api/v1/cases/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_correlation_id_is_echoed(client: TestClient) -> None:
    """Spec NFR Observability: correlation IDs must be traceable end to end."""
    response = client.get("/health", headers={"X-Correlation-ID": "trace-abc-123"})
    assert response.headers["X-Correlation-ID"] == "trace-abc-123"


def test_correlation_id_is_generated_when_absent(client: TestClient) -> None:
    response = client.get("/health")
    assert len(response.headers["X-Correlation-ID"]) == 32
