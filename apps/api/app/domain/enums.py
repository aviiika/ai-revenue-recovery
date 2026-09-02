"""Domain vocabulary.

These names appear in the database, the API and the UI. They are defined once,
here, so the frontend never re-derives domain logic (spec QUALITY rule: "do not
duplicate domain logic between frontend and backend").
"""

from __future__ import annotations

from enum import StrEnum


class CaseState(StrEnum):
    """States from spec FR-6."""

    NEW = "NEW"
    DIAGNOSED = "DIAGNOSED"
    SCORED = "SCORED"
    ACTION_SELECTED = "ACTION_SELECTED"
    ACTION_PENDING = "ACTION_PENDING"
    ACTION_EXECUTED = "ACTION_EXECUTED"
    OBSERVING = "OBSERVING"
    RECOVERED = "RECOVERED"
    RETRY_ELIGIBLE = "RETRY_ELIGIBLE"
    ESCALATED = "ESCALATED"
    EXHAUSTED = "EXHAUSTED"
    STOPPED = "STOPPED"


#: Once a case reaches one of these, no further transition is legal.
#: ESCALATED is deliberately *not* terminal: a human can resume the case.
TERMINAL_STATES: frozenset[CaseState] = frozenset(
    {CaseState.RECOVERED, CaseState.EXHAUSTED, CaseState.STOPPED}
)


class SourceType(StrEnum):
    PAYMENT = "PAYMENT"
    SUBSCRIPTION = "SUBSCRIPTION"
    CHECKOUT = "CHECKOUT"
    INVOICE = "INVOICE"


class FailureCategory(StrEnum):
    """Coarse root-cause grouping that drives deterministic policy routing.

    Recoverability differs sharply between these, which is why the category --
    not the raw gateway string -- is what the policy engine keys off.
    """

    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    CARD_EXPIRED = "CARD_EXPIRED"
    ISSUER_DECLINED = "ISSUER_DECLINED"
    TECHNICAL_ERROR = "TECHNICAL_ERROR"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    CUSTOMER_ABANDONED = "CUSTOMER_ABANDONED"
    UNKNOWN = "UNKNOWN"


class Recoverability(StrEnum):
    """Deterministic prior attached to a failure category."""

    TRANSIENT = "TRANSIENT"  # likely to succeed on a plain retry
    ACTIONABLE = "ACTIONABLE"  # needs customer action (new method, top-up)
    STRUCTURAL = "STRUCTURAL"  # mandate/card dead; needs re-authorisation
    UNRECOVERABLE = "UNRECOVERABLE"  # do not spend money chasing this


class InterventionStrategy(StrEnum):
    """The complete, bounded action space from spec FR-4.

    The LLM may *rank* or *explain* these. It may never invent a new one --
    that is what makes the agent bounded.
    """

    WAIT_AND_RETRY = "WAIT_AND_RETRY"
    CREATE_PAYMENT_LINK = "CREATE_PAYMENT_LINK"
    SEND_REMINDER_SIMULATED = "SEND_REMINDER_SIMULATED"
    REQUEST_ALTERNATE_METHOD = "REQUEST_ALTERNATE_METHOD"
    ESCALATE_HUMAN = "ESCALATE_HUMAN"
    DO_NOT_CONTACT = "DO_NOT_CONTACT"
    STOP_RECOVERY = "STOP_RECOVERY"


class ActorType(StrEnum):
    """Who caused an audit event. Every event has exactly one."""

    SYSTEM = "SYSTEM"
    MODEL = "MODEL"
    HUMAN = "HUMAN"
    WEBHOOK = "WEBHOOK"


class AuditEventType(StrEnum):
    CASE_INGESTED = "CASE_INGESTED"
    CASE_DIAGNOSED = "CASE_DIAGNOSED"
    CASE_SCORED = "CASE_SCORED"
    POLICY_EVALUATED = "POLICY_EVALUATED"
    INTERVENTION_SELECTED = "INTERVENTION_SELECTED"
    INTERVENTION_EXECUTED = "INTERVENTION_EXECUTED"
    OUTCOME_OBSERVED = "OUTCOME_OBSERVED"
    STATE_TRANSITIONED = "STATE_TRANSITIONED"
    HUMAN_DECISION = "HUMAN_DECISION"
    RECOVERY_RECORDED = "RECOVERY_RECORDED"
    TRANSITION_REJECTED = "TRANSITION_REJECTED"


#: Deterministic mapping from failure category to recoverability prior.
#: Preferred over an LLM guess whenever the reason code is known
#: (spec FR-2: "deterministic mappings should be preferred").
CATEGORY_RECOVERABILITY: dict[FailureCategory, Recoverability] = {
    FailureCategory.INSUFFICIENT_FUNDS: Recoverability.ACTIONABLE,
    FailureCategory.AUTHENTICATION_FAILED: Recoverability.TRANSIENT,
    FailureCategory.CARD_EXPIRED: Recoverability.STRUCTURAL,
    FailureCategory.ISSUER_DECLINED: Recoverability.ACTIONABLE,
    FailureCategory.TECHNICAL_ERROR: Recoverability.TRANSIENT,
    FailureCategory.NETWORK_TIMEOUT: Recoverability.TRANSIENT,
    FailureCategory.LIMIT_EXCEEDED: Recoverability.ACTIONABLE,
    FailureCategory.MANDATE_REVOKED: Recoverability.STRUCTURAL,
    FailureCategory.CUSTOMER_ABANDONED: Recoverability.ACTIONABLE,
    # An unknown reason code is treated as ACTIONABLE rather than
    # UNRECOVERABLE: we would rather route it to a human than silently write
    # off revenue we never diagnosed.
    FailureCategory.UNKNOWN: Recoverability.ACTIONABLE,
}
