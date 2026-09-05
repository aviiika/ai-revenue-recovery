"""End-to-end demo test (spec section 17, section 23).

The spec asks for "at least one demo test proving recovered amount across a
batch". This is that test, and it deliberately mirrors the demo scenario in
section 17 step for step — so the script a presenter reads and the assertion a
CI run makes are the same claim.

Everything goes through the HTTP API rather than the services directly. A demo
runs over HTTP, so the test should too; a serialisation bug that only shows at
the boundary would otherwise pass every other test in the suite and break the
demo.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import Settings, get_settings
from app.main import create_app

#: Spec section 17 asks for 120 cases; section 23 requires at least 50 in one
#: batch. 120 keeps the test honest against the number the demo actually shows.
DEMO_CASE_COUNT = 120


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_settings] = lambda: Settings(
        razorpay_mode="test",
        demo_endpoints_enabled=True,
        synthetic_seed=20260902,
        llm_provider="none",
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_full_demo_recovers_money_across_a_batch(client: TestClient, session: Session) -> None:
    """The whole loop, in the order the demo presents it.

    Asserted as one test rather than several because the claim being made is
    itself end to end: *a batch of failures goes in and measured recovered money
    comes out*. Splitting it would let the pieces pass while the chain is broken.
    """

    # --- 1. Seed a batch of revenue at risk ------------------------------
    seeded = client.post("/api/v1/demo/seed", json={"count": DEMO_CASE_COUNT, "reset": True})
    assert seeded.status_code == 201
    seed_body = seeded.json()
    assert seed_body["cases_created"] == DEMO_CASE_COUNT
    assert seed_body["synthetic"] is True

    at_risk_paise = seed_body["total_at_risk"]["paise"]
    # Spec section 17: a demo batch worth roughly INR 8-12 lakh.
    assert 800_000_00 <= at_risk_paise <= 1_200_000_00, (
        f"demo batch is {at_risk_paise / 100:,.0f} INR, outside the spec's 8-12 lakh band"
    )

    # --- 2. Run the agent to convergence ---------------------------------
    cycle = client.post("/api/v1/demo/run-full-cycle", json={"limit": 500, "rounds": 4})
    assert cycle.status_code == 200
    rounds = cycle.json()["rounds"]
    assert rounds, "the agent must actually do something"

    # --- 3. Money was recovered, and it is computed not asserted ---------
    overview = client.get("/api/v1/metrics/overview").json()
    kpis = overview["kpis"]

    recovered_paise = kpis["revenue_recovered"]["paise"]
    assert recovered_paise > 0, "the headline claim of the demo is recovered money"
    assert kpis["revenue_at_risk"]["paise"] == at_risk_paise
    assert 0 < kpis["recovery_rate_by_value"] < 1
    # Recovered can never exceed what was at risk -- a money bug would show here
    # before it showed on a slide.
    assert recovered_paise <= at_risk_paise
    assert overview["synthetic"] is True, "synthetic data must be labelled"

    # --- 4. The guardrails visibly fired ---------------------------------
    # Spec section 17 asks the demo to show a stopped case and a human
    # escalation, not merely a happy path.
    assert kpis["cases_stopped"] > 0, "no case was stopped; guardrails invisible"
    assert kpis["cases_escalated"] > 0, "no case escalated; human control invisible"

    cases = client.get("/api/v1/cases", params={"limit": 500}).json()
    stopped = [c for c in cases["items"] if c["current_state"] == "STOPPED"]
    opted_out = [c for c in stopped if c["do_not_contact"]]
    assert opted_out, "the do-not-contact population must be visibly stopped"

    # --- 5. A human review queue exists and is workable -------------------
    reviews = client.get("/api/v1/reviews").json()
    assert reviews["pending"] > 0
    assert reviews["value_awaiting_review"]["paise"] > 0

    top = reviews["items"][0]
    assert top["explanation"], "a reviewer must see prose, not a rule id"
    assert top["applied_rules"], "and the rules that produced the escalation"

    # A human decision lands and is attributed.
    decided = client.post(
        f"/api/v1/reviews/{top['id']}/reject",
        json={"reviewer": "demo@ops", "notes": "not worth pursuing"},
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "REJECTED"

    # --- 6. Every case has a reconstructable audit trail -------------------
    sample = cases["items"][0]
    detail = client.get(f"/api/v1/cases/{sample['id']}").json()
    trail = detail["audit_trail"]

    assert trail, "every case must have an audit trail"
    # Strictly monotonic sequence: history replays in exact order.
    assert [e["sequence"] for e in trail] == list(range(1, len(trail) + 1))
    event_types = {e["event_type"] for e in trail}
    assert "CASE_INGESTED" in event_types
    assert "POLICY_EVALUATED" in event_types
    # Who decided is answerable for every event.
    assert all(e["actor_type"] for e in trail)

    # --- 7. Incremental recovery, measured against a holdout --------------
    experiments = client.get("/api/v1/metrics/experiments").json()
    assert experiments["available"] is True

    incremental = experiments["incremental"]
    assert incremental["holdout"]["cases"] > 0, "no holdout; no causal claim possible"
    assert incremental["holdout"]["executed_actions"] == 0, (
        "a contacted holdout invalidates the entire measurement"
    )
    # Treated probability is >= spontaneous for every case by construction, so a
    # negative lift means a measurement bug rather than a weak agent.
    assert incremental["incremental_rate_points"] > 0
    assert "not evidence of real-world" in incremental["caveat"], (
        "the causal caveat must ship with the number"
    )

    # --- 8. The agent is compared against a baseline ----------------------
    policies = experiments["policies"]
    assert policies["agent"]["actions"] > 0
    assert policies["baseline"]["actions"] > 0
    # The agent's restraint is the point: it acts less than contact-everyone.
    assert policies["agent"]["actions"] < policies["baseline"]["actions"], (
        "the agent should act more selectively than the naive baseline"
    )


def test_rerunning_the_demo_does_not_double_count_revenue(
    client: TestClient,
) -> None:
    """A presenter will press the button twice. The number must not move.

    This is the demo-facing face of the idempotency guarantees: seeding,
    orchestration and outcome observation are each replay-safe.
    """
    client.post("/api/v1/demo/seed", json={"count": 60, "reset": True})
    client.post("/api/v1/demo/run-full-cycle", json={"limit": 200, "rounds": 3})
    first = client.get("/api/v1/metrics/overview").json()["kpis"]

    client.post("/api/v1/demo/run-full-cycle", json={"limit": 200, "rounds": 3})
    second = client.get("/api/v1/metrics/overview").json()["kpis"]

    assert second["revenue_recovered"]["paise"] == first["revenue_recovered"]["paise"]
    assert second["revenue_at_risk"]["paise"] == first["revenue_at_risk"]["paise"]
    assert second["total_cases"] == first["total_cases"]


def test_reseeding_with_the_same_seed_is_reproducible(client: TestClient) -> None:
    """A judge who re-runs the demo must see identical figures."""
    first = client.post("/api/v1/demo/seed", json={"count": 80, "reset": True}).json()
    second = client.post("/api/v1/demo/seed", json={"count": 80, "reset": True}).json()

    assert first["total_at_risk"]["paise"] == second["total_at_risk"]["paise"]
    assert first["seed_used"] == second["seed_used"]


def test_demo_survives_with_no_model_and_no_llm(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec section 19: the demo must survive dependency degradation.

    With no trained model and no LLM configured, the loop still runs on the
    deterministic scorer and template explainer, and still recovers money.
    """
    from pathlib import Path

    import ml.src.inference as inference

    monkeypatch.setattr(inference, "MODEL_PATH", Path("does-not-exist.joblib"))
    inference.load_model(force=True)

    client.post("/api/v1/demo/seed", json={"count": 60, "reset": True})
    client.post("/api/v1/demo/run-full-cycle", json={"limit": 200, "rounds": 3})

    kpis = client.get("/api/v1/metrics/overview").json()["kpis"]
    assert kpis["revenue_recovered"]["paise"] > 0

    cases = client.get("/api/v1/cases", params={"limit": 5}).json()
    assert cases["items"][0]["model_version"] == "deterministic-baseline-v1"

    reviews = client.get("/api/v1/reviews").json()
    if reviews["items"]:
        assert reviews["items"][0]["explanation"], "template explanations still written"

    # Restore for any later test in the session.
    inference.load_model(force=True)


def test_health_and_integration_status_are_demo_ready(client: TestClient) -> None:
    """The two endpoints a presenter is most likely to be asked to show."""
    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["razorpay_mode"] == "test", "live mode must never be possible"

    integrations = client.get("/api/v1/integrations/health").json()
    assert integrations["razorpay"]["live_mode_blocked"] is True
    assert integrations["razorpay"]["mode"] == "test"


def test_definition_of_done_surface_is_reachable(client: TestClient) -> None:
    """Every screen the spec's definition of done names has data behind it."""
    client.post("/api/v1/demo/seed", json={"count": 60, "reset": True})
    client.post("/api/v1/demo/run-batch", json={"limit": 200})

    for path in (
        "/api/v1/cases",
        "/api/v1/metrics/overview",
        "/api/v1/metrics/interventions",
        "/api/v1/metrics/models",
        "/api/v1/metrics/experiments",
        "/api/v1/reviews",
        "/api/v1/policies",
        "/api/v1/integrations/health",
    ):
        response = client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"
