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
from decimal import Decimal
from typing import Protocol

from app.core.money import Money, SignedMoney
from app.domain.enums import (
    CATEGORY_RECOVERABILITY,
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


class RecoveryScorer(Protocol):
    """Estimates P(recovery | case, strategy).

    Milestone 2 adds a trained implementation. The policy engine depends on this
    Protocol, never on a concrete scorer, so swapping the model in changes no
    policy code.
    """

    @property
    def model_version(self) -> str: ...

    def probability(
        self,
        *,
        recoverability: Recoverability,
        strategy: InterventionStrategy,
        attempt_count: int,
    ) -> Decimal: ...


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

    def probability(
        self,
        *,
        recoverability: Recoverability,
        strategy: InterventionStrategy,
        attempt_count: int,
    ) -> Decimal:
        base = _BASE_PROBABILITY[recoverability]
        fit = _STRATEGY_FIT[recoverability].get(strategy, Decimal("0.10"))

        # Each prior attempt makes the next one less likely to land. Capped so
        # the probability decays rather than collapsing to zero.
        decay = Decimal("0.72") ** max(attempt_count, 0)

        probability = base * fit * decay
        return min(max(probability, Decimal("0.01")), Decimal("0.95"))


def intervention_cost(strategy: InterventionStrategy) -> Money:
    return Money(INTERVENTION_COST_PAISE[strategy])


def score_strategy(
    scorer: RecoveryScorer,
    *,
    strategy: InterventionStrategy,
    recoverability: Recoverability,
    amount_at_risk: Money,
    attempt_count: int,
) -> ScoredStrategy:
    """Score one candidate strategy.

    ``EV = P(recovery) x amount_at_risk - intervention_cost`` (spec 10.1 Task C).
    The probability comes from the model; every rupee figure below is computed
    deterministically here.
    """
    probability = scorer.probability(
        recoverability=recoverability, strategy=strategy, attempt_count=attempt_count
    )
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
    *,
    failure_category: FailureCategory,
    amount_at_risk: Money,
    attempt_count: int,
    recoverability: Recoverability | None = None,
    strategies: tuple[InterventionStrategy, ...] = ACTIONABLE_STRATEGIES,
) -> list[ScoredStrategy]:
    """Score every candidate strategy, best expected net value first.

    Ranking here is advisory. The policy engine still has the final word and may
    reject the top-ranked option outright.
    """
    resolved = recoverability or CATEGORY_RECOVERABILITY[failure_category]
    scored = [
        score_strategy(
            scorer,
            strategy=strategy,
            recoverability=resolved,
            amount_at_risk=amount_at_risk,
            attempt_count=attempt_count,
        )
        for strategy in strategies
    ]
    # Ties broken by strategy name so the ordering is fully deterministic.
    scored.sort(key=lambda s: (-s.expected_net.paise, s.strategy))
    return scored
