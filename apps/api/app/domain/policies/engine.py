"""The policy engine (spec section 12).

**This module has final authority over what the agent does.** A model may rank
options and an LLM may explain them, but nothing acts unless the engine says so.

The engine is a pure function: it takes a case snapshot plus scored candidates
and returns a :class:`PolicyDecision`. No database, no clock of its own, no I/O.
That is what makes it exhaustively testable, and it is the reason the "LLM cannot
bypass the policy engine" guarantee is structural rather than aspirational.

Rules are evaluated in the fixed order given in spec section 12. Order matters:
``do_not_contact`` must be checked before expected value, or a profitable case
belonging to an opted-out customer would be actioned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.money import Money, SignedMoney
from app.domain.enums import CaseState, InterventionStrategy, Recoverability
from app.domain.policies.config import PolicyConfig
from app.domain.scoring.service import ScoredStrategy


class RuleId:
    """Stable identifiers, written into the audit trail.

    Stable because an operator reading a six-week-old audit event needs the rule
    id to still mean the same thing.
    """

    ALREADY_RECOVERED = "R01_ALREADY_RECOVERED"
    DO_NOT_CONTACT = "R02_DO_NOT_CONTACT"
    TERMINAL_STATE = "R03_TERMINAL_STATE"
    MAX_ATTEMPTS = "R04_MAX_ATTEMPTS_REACHED"
    HIGH_VALUE_LOW_CONFIDENCE = "R05_HIGH_VALUE_LOW_CONFIDENCE"
    LOW_CONFIDENCE = "R06_BELOW_CONFIDENCE_THRESHOLD"
    NEGATIVE_EXPECTED_VALUE = "R07_NON_POSITIVE_EXPECTED_VALUE"
    COOLDOWN_ACTIVE = "R08_COOLDOWN_ACTIVE"
    STRATEGY_DISABLED = "R09_STRATEGY_DISABLED_BY_MERCHANT"
    UNRECOVERABLE = "R10_DIAGNOSED_UNRECOVERABLE"
    TRANSIENT_RETRY = "R11_TRANSIENT_FAILURE_RETRY"
    ACTIONABLE_CONTACT = "R12_ACTIONABLE_FAILURE_CONTACT"
    DEFAULT_HUMAN = "R13_NO_CONFIDENT_AUTOMATED_OPTION"


@dataclass(frozen=True, slots=True)
class CaseSnapshot:
    """Everything the engine is allowed to see.

    A narrow, explicit input rather than the ORM object: it keeps the engine
    pure, makes every test case trivial to construct, and means adding a column
    to the model cannot silently change a policy outcome.
    """

    state: CaseState
    amount_at_risk: Money
    recovered_amount: Money
    recoverability: Recoverability
    attempt_count: int
    do_not_contact: bool
    last_action_at: datetime | None
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class BlockedStrategy:
    strategy: InterventionStrategy
    rule_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The engine's verdict, in the shape spec section 12 requires."""

    recommended_strategy: InterventionStrategy
    #: The state to move to, or ``None`` to leave the case exactly where it is.
    #: Deferral is a real outcome -- a cooldown means "do not act yet", which is
    #: not the same as any transition. Modelling it as None keeps RETRY_ELIGIBLE
    #: meaning what FR-6 says it means: acted, observed, may retry.
    next_state: CaseState | None
    requires_human: bool
    allowed: list[ScoredStrategy]
    blocked: list[BlockedStrategy]
    applied_rules: list[str]
    explanation: str
    expected_net: SignedMoney
    retry_after: datetime | None = None
    #: Set when the decision is driven entirely by a hard rule rather than by
    #: expected value, e.g. an opt-out. Useful for the UI to explain itself.
    decisive_rule: str | None = None
    _snapshot_fields: dict[str, object] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.next_state in {CaseState.STOPPED, CaseState.RECOVERED, CaseState.EXHAUSTED}

    @property
    def is_deferred(self) -> bool:
        """True when the engine declined to act without changing the case."""
        return self.next_state is None

    def to_audit_payload(self) -> dict[str, object]:
        """Flat, JSON-safe snapshot for the audit trail (spec FR-8)."""
        return {
            "recommended_strategy": str(self.recommended_strategy),
            "next_state": str(self.next_state) if self.next_state else None,
            "deferred": self.is_deferred,
            "requires_human": self.requires_human,
            "applied_rules": list(self.applied_rules),
            "decisive_rule": self.decisive_rule,
            "expected_net_paise": self.expected_net.paise,
            "retry_after": self.retry_after.isoformat() if self.retry_after else None,
            "allowed": [
                {
                    "strategy": str(s.strategy),
                    "probability": str(s.probability),
                    "expected_gross_paise": s.expected_gross.paise,
                    "cost_paise": s.cost.paise,
                    "expected_net_paise": s.expected_net.paise,
                    "model_version": s.model_version,
                }
                for s in self.allowed
            ],
            "blocked": [
                {"strategy": str(b.strategy), "rule_id": b.rule_id, "reason": b.reason}
                for b in self.blocked
            ],
        }


def _stop(
    rule_id: str, explanation: str, blocked: list[BlockedStrategy], applied: list[str]
) -> PolicyDecision:
    return PolicyDecision(
        recommended_strategy=InterventionStrategy.STOP_RECOVERY,
        next_state=CaseState.STOPPED,
        requires_human=False,
        allowed=[],
        blocked=blocked,
        applied_rules=applied,
        explanation=explanation,
        expected_net=SignedMoney(0),
        decisive_rule=rule_id,
    )


def _escalate(
    rule_id: str,
    explanation: str,
    allowed: list[ScoredStrategy],
    blocked: list[BlockedStrategy],
    applied: list[str],
    next_state: CaseState = CaseState.ESCALATED,
) -> PolicyDecision:
    return PolicyDecision(
        recommended_strategy=InterventionStrategy.ESCALATE_HUMAN,
        next_state=next_state,
        requires_human=True,
        allowed=allowed,
        blocked=blocked,
        applied_rules=applied,
        explanation=explanation,
        expected_net=allowed[0].expected_net if allowed else SignedMoney(0),
        decisive_rule=rule_id,
    )


def decide(
    snapshot: CaseSnapshot,
    scored: list[ScoredStrategy],
    config: PolicyConfig,
) -> PolicyDecision:
    """Choose the highest-value action that clears every gate.

    Returns a decision in all cases -- there is no path where the engine declines
    to answer. "Do nothing" is expressed as an explicit STOP or WAIT, never as an
    absent decision.
    """
    applied: list[str] = []
    blocked: list[BlockedStrategy] = []

    # --- Rule 1: money already recovered. Nothing further may be spent. -------
    if snapshot.recovered_amount.paise > 0 or snapshot.state == CaseState.RECOVERED:
        applied.append(RuleId.ALREADY_RECOVERED)
        return _stop(
            RuleId.ALREADY_RECOVERED,
            f"Already recovered {snapshot.recovered_amount.format_inr()}; no further action.",
            blocked,
            applied,
        )

    # --- Rule 2: opt-out is absolute, and is checked before economics. --------
    if snapshot.do_not_contact:
        applied.append(RuleId.DO_NOT_CONTACT)
        blocked = [
            BlockedStrategy(s.strategy, RuleId.DO_NOT_CONTACT, "Customer has opted out of contact")
            for s in scored
        ]
        return _stop(
            RuleId.DO_NOT_CONTACT,
            "Customer is flagged do-not-contact; recovery stopped regardless of value.",
            blocked,
            applied,
        )

    # --- Rule 3: a terminal case is closed. ----------------------------------
    if snapshot.state in {CaseState.STOPPED, CaseState.EXHAUSTED}:
        applied.append(RuleId.TERMINAL_STATE)
        return _stop(
            RuleId.TERMINAL_STATE,
            f"Case is in terminal state {snapshot.state}; no further action.",
            blocked,
            applied,
        )

    # --- Rule 4: attempt budget exhausted -> hand to a human, do not retry. ---
    if snapshot.attempt_count >= config.max_automated_attempts:
        applied.append(RuleId.MAX_ATTEMPTS)
        return _escalate(
            RuleId.MAX_ATTEMPTS,
            (
                f"Reached the automated attempt limit "
                f"({snapshot.attempt_count}/{config.max_automated_attempts}); "
                "handing to a human reviewer."
            ),
            [],
            blocked,
            applied,
            next_state=CaseState.ESCALATED,
        )

    # --- Rule 5: a diagnosed-unrecoverable case is not worth pursuing. --------
    if snapshot.recoverability == Recoverability.UNRECOVERABLE:
        applied.append(RuleId.UNRECOVERABLE)
        return _stop(
            RuleId.UNRECOVERABLE,
            "Failure diagnosed as unrecoverable; stopping rather than spending on contact.",
            [
                BlockedStrategy(s.strategy, RuleId.UNRECOVERABLE, "Diagnosis is unrecoverable")
                for s in scored
            ],
            applied,
        )

    # --- Merchant-disabled strategies are removed before ranking. ------------
    candidates: list[ScoredStrategy] = []
    for option in scored:
        if option.strategy not in config.enabled_strategies:
            blocked.append(
                BlockedStrategy(
                    option.strategy,
                    RuleId.STRATEGY_DISABLED,
                    "Strategy is not enabled in merchant policy",
                )
            )
            continue
        candidates.append(option)
    if blocked:
        applied.append(RuleId.STRATEGY_DISABLED)

    # --- Rule 6: every remaining option loses money -> stop. -----------------
    viable = [
        option
        for option in candidates
        if option.expected_net.paise > config.min_expected_net_recovery_paise
    ]
    for option in candidates:
        if option not in viable:
            blocked.append(
                BlockedStrategy(
                    option.strategy,
                    RuleId.NEGATIVE_EXPECTED_VALUE,
                    (
                        f"Expected net {option.expected_net} does not clear the "
                        f"{Money(config.min_expected_net_recovery_paise).format_inr()} bar"
                    ),
                )
            )
    if not viable:
        applied.append(RuleId.NEGATIVE_EXPECTED_VALUE)
        return _stop(
            RuleId.NEGATIVE_EXPECTED_VALUE,
            "No intervention has positive expected net recovery; stopping to avoid "
            "spending more than the case is worth.",
            blocked,
            applied,
        )

    best = viable[0]

    # --- Rule 7: high-value and low-confidence -> human, before acting. ------
    is_high_value = snapshot.amount_at_risk.paise >= config.high_value_threshold_paise
    below_confidence = best.probability < Decimal(str(config.min_auto_action_confidence))

    if is_high_value and below_confidence:
        applied.append(RuleId.HIGH_VALUE_LOW_CONFIDENCE)
        return _escalate(
            RuleId.HIGH_VALUE_LOW_CONFIDENCE,
            (
                f"{snapshot.amount_at_risk.format_inr()} is at or above the high-value "
                f"threshold and best confidence is {best.probability:.2f}, below the "
                f"{config.min_auto_action_confidence:.2f} automation bar."
            ),
            viable,
            blocked,
            applied,
        )

    if below_confidence:
        applied.append(RuleId.LOW_CONFIDENCE)
        return _escalate(
            RuleId.LOW_CONFIDENCE,
            (
                f"Best available confidence is {best.probability:.2f}, below the "
                f"{config.min_auto_action_confidence:.2f} automation bar; routing to a human."
            ),
            viable,
            blocked,
            applied,
        )

    # --- Rule 8: cooldown -> wait rather than contact again too soon. --------
    if snapshot.last_action_at is not None and config.cooldown_hours > 0:
        cooldown_ends = snapshot.last_action_at + timedelta(hours=config.cooldown_hours)
        if snapshot.evaluated_at < cooldown_ends:
            applied.append(RuleId.COOLDOWN_ACTIVE)
            return PolicyDecision(
                recommended_strategy=InterventionStrategy.WAIT_AND_RETRY,
                next_state=None,
                requires_human=False,
                allowed=viable,
                blocked=blocked,
                applied_rules=applied,
                explanation=(
                    f"Cooldown active until {cooldown_ends.isoformat()}; waiting rather "
                    "than contacting the customer again."
                ),
                expected_net=best.expected_net,
                retry_after=cooldown_ends,
                decisive_rule=RuleId.COOLDOWN_ACTIVE,
            )

    # --- Rules 9/10: act, labelling why this class of action was chosen. -----
    if snapshot.recoverability == Recoverability.TRANSIENT:
        applied.append(RuleId.TRANSIENT_RETRY)
    else:
        applied.append(RuleId.ACTIONABLE_CONTACT)

    retry_after = None
    backoff = config.backoff_hours_for_attempt(snapshot.attempt_count)
    if backoff > 0:
        retry_after = snapshot.evaluated_at + timedelta(hours=backoff)

    return PolicyDecision(
        recommended_strategy=best.strategy,
        next_state=CaseState.ACTION_SELECTED,
        requires_human=False,
        allowed=viable,
        blocked=blocked,
        applied_rules=applied,
        explanation=(
            f"{best.strategy} has the highest expected net recovery "
            f"({best.expected_net}) at {best.probability:.2f} confidence on a "
            f"{snapshot.recoverability.lower()} failure."
        ),
        expected_net=best.expected_net,
        retry_after=retry_after,
    )
