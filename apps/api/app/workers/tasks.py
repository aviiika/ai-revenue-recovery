"""Celery worker tasks (spec section 7, Milestone 4).

**These are thin wrappers.** All orchestration logic lives in
``app.domain.orchestrator``, which is synchronous and infrastructure-free. The
worker exists to move long batches off the request thread, not to hold business
rules.

That separation is deliberate. The spec asks for background workers *and* for a
demo that does not depend on external timing; keeping the core callable inline
means the demo runs with or without a worker, and the worker runs exactly the
same code that the tests cover.

Run one with:
    celery -A app.workers.tasks worker --loglevel=info
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from celery import Celery

from app.core.config import get_settings
from app.core.logging import configure_logging, correlation_id_var, new_correlation_id
from app.db.models import Merchant
from app.db.session import session_scope
from app.domain.orchestrator import service as orchestrator
from app.domain.simulator import service as simulator

logger = logging.getLogger(__name__)

settings = get_settings()
configure_logging(settings.log_level)

celery_app = Celery(
    "revrec",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Acknowledge only after the task body finishes, so a worker crash
    # redelivers rather than silently dropping a batch. Safe because every
    # action underneath is idempotent.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)


@celery_app.task(name="revrec.run_batch", bind=True, max_retries=3)
def run_batch_task(self: Any, merchant_id: str, limit: int = 500) -> dict[str, Any]:
    """Evaluate and action pending cases for one merchant."""
    correlation_id_var.set(new_correlation_id())
    with session_scope() as session:
        merchant = session.get(Merchant, uuid.UUID(merchant_id))
        if merchant is None:
            raise ValueError(f"Merchant {merchant_id} not found")

        result = orchestrator.run_batch(session, merchant, now=datetime.now(UTC), limit=limit)
        logger.info(
            "batch_completed",
            extra={"evaluated": result.evaluated, "executed": result.executed},
        )
        return {
            "evaluated": result.evaluated,
            "executed": result.executed,
            "escalated": result.escalated,
            "stopped": result.stopped,
            "holdout_withheld": result.holdout_withheld,
        }


@celery_app.task(name="revrec.simulate_outcomes", bind=True, max_retries=3)
def simulate_outcomes_task(
    self: Any, merchant_id: str, seed: int, limit: int = 2000
) -> dict[str, Any]:
    """Reveal outcomes for cases awaiting observation."""
    correlation_id_var.set(new_correlation_id())
    with session_scope() as session:
        summary = simulator.simulate_outcomes(
            session,
            uuid.UUID(merchant_id),
            seed=seed,
            now=datetime.now(UTC),
            limit=limit,
        )
        return {
            "observed": summary.cases_observed,
            "recovered": summary.recovered,
            "recovered_amount_paise": summary.recovered_amount.paise,
        }
