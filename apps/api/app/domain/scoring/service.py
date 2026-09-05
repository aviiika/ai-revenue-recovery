"""Recovery scoring: probability, cost and expected value (spec FR-3).

Separation of concerns that matters here:

* **Probability** is a model output. It is uncertain, and at Milestone 2 it will
  come from a trained classifier.
* **Expected value** is deterministic arithmetic over that probability. It is
  money, so it is computed here in integer paise and never by a model.

The scorer is a Protocol so the ML model can be swapped in at Milestone 2 without
the policy engine changing at all. The deterministic baseline below is not a
placeholder to be deleted -- it is the permanent fallback for when the model is
unavailable, which the spec requires the demo to survive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.core.money import Money, SignedMoney
from app.domain.enums import (
    FailureCategory,
    InterventionStrategy,
    Recoverability,
)

#: Estimated cost of performing each intervention once, in paise.
#:
#: These are deliberate, arguable estimates rather than measured figures, and
#: they exist so that expected *net* value -- not gross -- drives the decision.
#: ESCALATE_HUMAN is by far the most expensive because it consumes an operator's
#: attention, which is the scarcest resource in a recovery operation.
INTERVENTION_COST_PAISE: dict[InterventionStrategy, int] = {
    InterventionStrategy.WAIT_AND_RETRY: 50,  # a gateway retry costs almost nothing
    InterventionStrategy.SEND_REMINDER_SIMULATED: 200,
    InterventionStrategy.REQUEST_ALTERNATE_METHOD: 300,
    InterventionStrategy.CREATE_PAYMENT_LINK: 500,
    InterventionStrategy.ESCALATE_HUMAN: 5000,  # ~INR 50 of operator time
    InterventionStrategy.DO_NOT_CONTACT: 0,
    InterventionStrategy.STOP_RECOVERY: 0,
}

#: How well each strategy suits each recoverability class, as an odds multiplier.
#:
#: The shape encodes real domain reasoning: a plain retry is excellent for a
#: transient gateway failure and near-useless for a revoked mandate, which needs
#: the customer to re-authorise. A payment link is the opposite.
_STRATEGY_FIT: dict[Recoverability, dict[InterventionStrategy, Decimal]] = {
    Recoverability.TRANSIENT: {
        InterventionStrategy.WAIT_AND_RETRY: Decimal("1.30"),
        InterventionStrategy.SEND_REMINDER_SIMULATED: Decimal("0.85"),
        InterventionStrategy.CREATE_PAYMENT_LINK: Decimal("0.95"),
        InterventionStrategy.REQUEST_ALTERNATE_METHOD: Decimal("0.70"),
    },
    Recoverability.ACTIONABLE: {
        InterventionStrategy.WAIT_AND_RETRY: Decimal("0.55"),
        InterventionStrategy.SEND_REMINDER_SIMULATED: Decimal("1.05"),
        InterventionStrategy.CREATE_PAYMENT_LINK: Decimal("1.35"),
        InterventionStrategy.REQUEST_ALTERNATE_METHOD: Decimal("1.15"),
    },
    Recoverability.STRUCTURAL: {
        InterventionStrategy.WAIT_AND_RETRY: Decimal("0.20"),
        InterventionStrategy.SEND_REMINDER_SIMULATED: Decimal("0.80"),
        InterventionStrategy.CREATE_PAYMENT_LINK: Decimal("1.10"),
        InterventionStrategy.REQUEST_ALTERNATE_METHOD: Decimal("1.45"),
    },
    Recoverability.UNRECOVERABLE: {
        InterventionStrategy.WAIT_AND_RETRY: Decimal("0.05"),
        InterventionStrategy.SEND_REMINDER_SIMULATED: Decimal("0.10"),
        InterventionStrategy.CREATE_PAYMENT_LINK: Decimal("0.10"),
        InterventionStrategy.REQUEST_ALTERNATE_METHOD: Decimal("0.15"),
    },
}

#: Base recovery probability per recoverability class, before strategy fit.
_BASE_PROBABILITY: dict[Recoverability, Decimal] = {
    Recoverability.TRANSIENT: Decimal("0.62"),
    Recoverability.ACTIONABLE: Decimal("0.38"),
    Recoverability.STRUCTURAL: Decimal("0.19"),
    Recoverability.UNRECOVERABLE: Decimal("0.04"),
}

#: Strategies that are candidates for actually recovering money. The remaining
#: members of the enum are control actions, not recovery attempts.
ACTIONABLE_STRATEGIES: tuple[InterventionStrategy, ...] = (
    InterventionStrategy.WAIT_AND_RETRY,
    InterventionStrategy.SEND_REMINDER_SIMULATED,
    InterventionStrategy.REQUEST_ALTERNATE_METHOD,
    InterventionStrategy.CREATE_PAYMENT_LINK,
)

#: Source types where there is no charge to retry.
#:
#: An abandoned checkout was never charged -- the customer left before paying --
#: and an overdue invoice has no failed authorisation behind it. Offering
#: WAIT_AND_RETRY for either would be scoring an action that cannot physically
#: happen, so those cases are only ever offered contact-based strategies.
NO_RETRY_SOURCES: frozenset[str] = frozenset({"CHECKOUT", "INVOICE"})


def candidate_strategies(source_type: str) -> tuple[InterventionStrategy, ...]:
    """The strategies worth scoring for a given kind of revenue at risk."""
    if source_type in NO_RETRY_SOURCES:
        return tuple(
            s for s in ACTIONABLE_STRATEGIES if s is not InterventionStrategy.WAIT_AND_RETRY
        )
    return ACTIONABLE_STRATEGIES


DETERMINISTIC_MODEL_VERSION = "deterministic-baseline-v1"


@dataclass(frozen=True, slots=True)
class ScoredStrategy:
    """One candidate action, with its probability and expected economics."""

    strategy: InterventionStrategy
    probability: Decimal
    amount_at_risk: Money
    expected_gross: Money
    cost: Money
    expected_net: SignedMoney
    model_version: str

    @property
    def is_worth_doing(self) -> bool:
        """Whether expected net recovery is strictly positive.

        Spec FR-7 stops the case when this is false for every candidate: the
        agent should not spend money chasing money it will not get.
        """
        return self.expected_net.is_positive


@dataclass(frozen=True, slots=True)
class CaseFeatures:
    """Everything a scorer may look at, in domain terms.

    Widened from the original three-argument signature once a real model needed
    real features. Keeping this a single explicit object -- rather than a growing
    argument list -- means adding a feature is one change here and one in the ML
    adapter, and the policy engine never sees it at all.

    Contains no outcome fields: everything here is knowable at the moment the
    decision is made.
    """

    recoverability: Recoverability
    failure_category: FailureCategory
    amount_at_risk: Money
    attempt_count: int
    source_type: str = "PAYMENT"
    payment_method: str | None = None
    customer_segment: str | None = None
    customer_tenure_days: int = 0
    prior_successful_payments: int = 0
    prior_failed_payments: int = 0
    subscription_age_days: int | None = None
    days_overdue: int | None = None
    checkout_stage: str | None = None
    detected_at: datetime | None = None

    @property
    def hour_of_day(self) -> int:
        return self.detected_at.hour if self.detected_at else 12

    @property
    def day_of_week(self) -> int:
        return self.detected_at.weekday() if self.detected_at else 0


class RecoveryScorer(Protocol):
    """Estimates P(recovery | case, strategy).

    The policy engine depends on this Protocol, never on a concrete scorer, so
    swapping the trained model in for the deterministic baseline changes no
    policy code.
    """

    @property
    def model_version(self) -> str: ...

    def probability(self, features: CaseFeatures, strategy: InterventionStrategy) -> Decimal: ...


class DeterministicScorer:
    """Interpretable, dependency-free baseline and permanent ML fallback.

    Deliberately simple and monotone: probability falls with each attempt, and is
    modulated by how well the strategy fits the diagnosed failure. It produces
    sane orderings without a trained model, which is what keeps the demo alive
    when the model artifact is missing.
    """

    @property
    def model_version(self) -> str:
        return DETERMINISTIC_MODEL_VERSION

    def probability(self, features: CaseFeatures, strategy: InterventionStrategy) -> Decimal:
        base = _BASE_PROBABILITY[features.recoverability]
        fit = strategy_fit(features.recoverability, strategy)

        # Each prior attempt makes the next one less likely to land. Capped so
        # the probability decays rather than collapsing to zero.
        decay = Decimal("0.72") ** max(features.attempt_count, 0)

        probability = base * fit * decay
        return min(max(probability, Decimal("0.01")), Decimal("0.95"))


def strategy_fit(recoverability: Recoverability, strategy: InterventionStrategy) -> Decimal:
    """How well a strategy suits a diagnosed failure class.

    Exposed because the ML scorer reuses it: the trained model predicts
    case-level recoverability, and this deterministic multiplier turns that into
    a per-strategy estimate. Spec 10.1 sanctions exactly this for the MVP --
    "train a baseline binary classifier and apply deterministic intervention
    rules" -- because the synthetic data has no counterfactual per-strategy
    outcomes to learn from.
    """
    return _STRATEGY_FIT[recoverability].get(strategy, Decimal("0.10"))


def intervention_cost(strategy: InterventionStrategy) -> Money:
    return Money(INTERVENTION_COST_PAISE[strategy])


def score_strategy(
    scorer: RecoveryScorer,
    *,
    strategy: InterventionStrategy,
    features: CaseFeatures,
) -> ScoredStrategy:
    """Score one candidate strategy.

    ``EV = P(recovery) x amount_at_risk - intervention_cost`` (spec 10.1 Task C).
    The probability comes from the model; every rupee figure below is computed
    deterministically here.
    """
    probability = scorer.probability(features, strategy)
    amount_at_risk = features.amount_at_risk
    expected_gross = amount_at_risk.scale(probability)
    cost = intervention_cost(strategy)
    expected_net = SignedMoney.of(expected_gross) - SignedMoney.of(cost)

    return ScoredStrategy(
        strategy=strategy,
        probability=probability,
        amount_at_risk=amount_at_risk,
        expected_gross=expected_gross,
        cost=cost,
        expected_net=expected_net,
        model_version=scorer.model_version,
    )


def score_all(
    scorer: RecoveryScorer,
    features: CaseFeatures,
    strategies: tuple[InterventionStrategy, ...] | None = None,
) -> list[ScoredStrategy]:
    """Score every candidate strategy, best expected net value first.

    Ranking here is advisory. The policy engine still has the final word and may
    reject the top-ranked option outright.

    When ``strategies`` is omitted the candidate set is chosen from the source
    type, so an abandoned checkout or an overdue invoice is never scored for a
    retry that has no charge behind it.
    """
    candidates = (
        strategies if strategies is not None else candidate_strategies(features.source_type)
    )
    scored = [
        score_strategy(scorer, strategy=strategy, features=features) for strategy in candidates
    ]
    # Ties broken by strategy name so the ordering is fully deterministic.
    scored.sort(key=lambda s: (-s.expected_net.paise, s.strategy))
    return scored
