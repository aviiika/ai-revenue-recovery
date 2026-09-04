"""Human review queue (spec FR-3 Flow C, Milestone 6).

The policy engine escalates; this is where those cases become work a person can
actually do. Three verbs, matching the spec: approve, override, reject.

Every one of them is audited with the reviewer's identity. The spec is explicit
that "every override is audited" — a human decision that cannot be traced back
to a person is no better than an unexplained automated one.

Authority ordering, which matters: a reviewer chooses **among the strategies the
policy engine permits**. Override lets a person pick a different action, not an
arbitrary one, and the chosen strategy is validated against the merchant's
enabled set. The guardrails bind humans too — a reviewer cannot contact an
opted-out customer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.core.money import Money
from app.db.models import HumanReview, Merchant, RecoveryCase
from app.domain.audit import service as audit
from app.domain.enums import (
    ActorType,
    AuditEventType,
    CaseState,
    InterventionStrategy,
    ReviewReason,
    ReviewStatus,
)
from app.domain.policies.config import PolicyConfig
from app.domain.policies.engine import PolicyDecision, RuleId


class ReviewError(Exception):
    """Raised when a review action is not permitted."""


#: Which escalating rule produced the review. Used to explain the queue.
_RULE_TO_REASON: dict[str, ReviewReason] = {
    RuleId.HIGH_VALUE_LOW_CONFIDENCE: ReviewReason.HIGH_VALUE_LOW_CONFIDENCE,
    RuleId.LOW_CONFIDENCE: ReviewReason.BELOW_CONFIDENCE_THRESHOLD,
    RuleId.MAX_ATTEMPTS: ReviewReason.MAX_ATTEMPTS_REACHED,
}


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    review: HumanReview
    case: RecoveryCase
    action: str


def open_review(
    session: Session,
    case: RecoveryCase,
    decision: PolicyDecision,
    *,
    now: datetime,
    explanation: str | None = None,
) -> HumanReview:
    """Create or refresh the review task for an escalated case.

    Idempotent per case: a second escalation updates the open review rather than
    stacking duplicates, so the queue length reflects cases needing attention
    rather than how many times the agent looked at them.
    """
    existing = session.execute(
        select(HumanReview).where(
            HumanReview.recovery_case_id == case.id,
            HumanReview.status == ReviewStatus.PENDING,
        )
    ).scalar_one_or_none()

    reason = _RULE_TO_REASON.get(decision.decisive_rule or "", ReviewReason.MANUAL)
    # The strategy the agent would have run had it been allowed to act alone.
    proposed = decision.allowed[0].strategy if decision.allowed else None

    if existing is not None:
        existing.reason = reason
        existing.proposed_strategy = proposed
        existing.decision_snapshot = decision.to_audit_payload()
        existing.amount_at_risk_paise = case.amount_at_risk_paise
        if explanation:
            existing.explanation = explanation
        session.add(existing)
        session.flush()
        return existing

    review = HumanReview(
        id=uuid.uuid4(),
        recovery_case_id=case.id,
        reason=reason,
        status=ReviewStatus.PENDING,
        proposed_strategy=proposed,
        decision_snapshot=decision.to_audit_payload(),
        explanation=explanation,
        amount_at_risk_paise=case.amount_at_risk_paise,
    )
    session.add(review)
    session.flush()

    audit.record(
        session,
        case_id=case.id,
        event_type=AuditEventType.REVIEW_CREATED,
        summary=f"Escalated for human review: {reason}",
        payload={
            "reason": str(reason),
            "proposed_strategy": str(proposed) if proposed else None,
            "decisive_rule": decision.decisive_rule,
            "amount_at_risk_paise": case.amount_at_risk_paise,
        },
    )
    return review


def build_queue_query(
    *, status: ReviewStatus | None = ReviewStatus.PENDING
) -> Select[tuple[HumanReview]]:
    """Queue ordered by money at stake, largest first.

    Reviewer attention is the scarcest resource in the system, so it is spent on
    the biggest exposure first.
    """
    query = select(HumanReview)
    if status is not None:
        query = query.where(HumanReview.status == status)
    return query.order_by(HumanReview.amount_at_risk_paise.desc(), HumanReview.created_at)


def _load(session: Session, review_id: uuid.UUID) -> tuple[HumanReview, RecoveryCase]:
    review = session.get(HumanReview, review_id)
    if review is None:
        raise ReviewError(f"Review {review_id} not found")
    # Coerced, not compared by identity: a String column round-trips as a
    # plain str, so `is not ReviewStatus.PENDING` is always true once the row
    # has been through the database.
    if ReviewStatus(review.status) is not ReviewStatus.PENDING:
        raise ReviewError(
            f"Review {review_id} is already {review.status}; it cannot be decided twice"
        )
    case = session.get(RecoveryCase, review.recovery_case_id)
    if case is None:
        raise ReviewError(f"Review {review_id} has no case")
    return review, case


def _guardrails(case: RecoveryCase) -> str | None:
    """Limits that bind a human reviewer as well as the agent.

    A person may overrule the agent's *judgement*. They may not overrule an
    opt-out or resurrect a settled case — those are compliance and correctness
    boundaries, not matters of opinion.
    """
    if case.do_not_contact:
        return "customer has opted out of contact"
    if case.current_state == CaseState.RECOVERED:
        return "case is already recovered"
    if case.current_state in {CaseState.STOPPED, CaseState.EXHAUSTED}:
        return f"case is in terminal state {case.current_state}"
    return None


def _resolve(
    session: Session,
    review: HumanReview,
    case: RecoveryCase,
    *,
    status: ReviewStatus,
    reviewer: str,
    notes: str,
    now: datetime,
    chosen: InterventionStrategy | None = None,
) -> None:
    review.status = status
    review.reviewer = reviewer
    review.decision_notes = notes
    review.chosen_strategy = chosen
    review.resolved_at = now
    session.add(review)

    audit.record(
        session,
        case_id=case.id,
        event_type=AuditEventType.HUMAN_DECISION,
        summary=f"Reviewer {reviewer} chose {status}: {notes}",
        actor_type=ActorType.HUMAN,
        actor_id=reviewer,
        payload={
            "review_id": str(review.id),
            "decision": str(status),
            "chosen_strategy": str(chosen) if chosen else None,
            "proposed_strategy": (
                str(review.proposed_strategy) if review.proposed_strategy else None
            ),
            "notes": notes,
        },
    )


def approve(
    session: Session,
    review_id: uuid.UUID,
    *,
    reviewer: str,
    notes: str,
    now: datetime,
) -> ReviewOutcome:
    """Let the agent's proposed action stand and return the case to the loop."""
    review, case = _load(session, review_id)

    blocked = _guardrails(case)
    if blocked is not None:
        raise ReviewError(f"Cannot approve: {blocked}")
    if review.proposed_strategy is None:
        raise ReviewError("This review has no proposed action to approve")

    _resolve(
        session,
        review,
        case,
        status=ReviewStatus.APPROVED,
        reviewer=reviewer,
        notes=notes,
        now=now,
        chosen=InterventionStrategy(review.proposed_strategy),
    )

    from app.domain.cases import service as cases

    cases.transition(
        session,
        case,
        CaseState.ACTION_SELECTED,
        f"Human approved {review.proposed_strategy}",
        actor_type=ActorType.HUMAN,
        actor_id=reviewer,
    )
    session.flush()
    return ReviewOutcome(review=review, case=case, action=f"approved {review.proposed_strategy}")


def override(
    session: Session,
    review_id: uuid.UUID,
    merchant: Merchant,
    *,
    strategy: InterventionStrategy,
    reviewer: str,
    notes: str,
    now: datetime,
) -> ReviewOutcome:
    """Substitute a different strategy, within what policy permits.

    A reviewer picks among *allowed* actions. Letting a human choose a strategy
    the merchant has disabled would make the policy engine advisory, which is
    exactly the property the spec says must not hold.
    """
    review, case = _load(session, review_id)

    blocked = _guardrails(case)
    if blocked is not None:
        raise ReviewError(f"Cannot override: {blocked}")

    config = PolicyConfig.from_merchant(merchant.policy_config, merchant.high_value_threshold_paise)
    if strategy not in config.enabled_strategies:
        raise ReviewError(
            f"{strategy} is not enabled in merchant policy; "
            f"permitted: {', '.join(sorted(config.enabled_strategies))}"
        )

    _resolve(
        session,
        review,
        case,
        status=ReviewStatus.OVERRIDDEN,
        reviewer=reviewer,
        notes=notes,
        now=now,
        chosen=strategy,
    )

    from app.domain.cases import service as cases

    cases.transition(
        session,
        case,
        CaseState.ACTION_SELECTED,
        f"Human overrode {review.proposed_strategy} with {strategy}",
        actor_type=ActorType.HUMAN,
        actor_id=reviewer,
    )
    session.flush()
    return ReviewOutcome(review=review, case=case, action=f"overridden to {strategy}")


def reject(
    session: Session,
    review_id: uuid.UUID,
    *,
    reviewer: str,
    notes: str,
    now: datetime,
) -> ReviewOutcome:
    """Stop the case. Always available — this is the human kill switch."""
    review, case = _load(session, review_id)

    _resolve(
        session,
        review,
        case,
        status=ReviewStatus.REJECTED,
        reviewer=reviewer,
        notes=notes,
        now=now,
    )

    from app.domain.cases import service as cases

    if case.current_state not in {CaseState.STOPPED, CaseState.EXHAUSTED, CaseState.RECOVERED}:
        cases.transition(
            session,
            case,
            CaseState.STOPPED,
            f"Human rejected recovery: {notes}",
            actor_type=ActorType.HUMAN,
            actor_id=reviewer,
        )
    session.flush()
    return ReviewOutcome(review=review, case=case, action="rejected; case stopped")


@dataclass(frozen=True, slots=True)
class QueueSummary:
    """Headline numbers for the review queue.

    A typed object rather than a loose dict, so callers are not forced to cast
    their way back to ``int`` at every use site.
    """

    pending: int
    value_awaiting_review: Money
    by_reason: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "pending": self.pending,
            "value_awaiting_review": self.value_awaiting_review.format_inr(),
            "value_awaiting_review_paise": self.value_awaiting_review.paise,
            "by_reason": self.by_reason,
        }


def queue_summary(session: Session) -> QueueSummary:
    """Pending count and the money waiting on a person."""
    pending = list(session.execute(build_queue_query()).scalars().all())
    by_reason: dict[str, int] = {}
    for review in pending:
        key = str(review.reason)
        by_reason[key] = by_reason.get(key, 0) + 1

    return QueueSummary(
        pending=len(pending),
        value_awaiting_review=Money(sum(r.amount_at_risk_paise for r in pending)),
        by_reason=dict(sorted(by_reason.items())),
    )
