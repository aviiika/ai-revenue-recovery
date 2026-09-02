"""Merchant-configurable policy configuration (spec section 16, Policy settings).

Loaded from ``Merchant.policy_config`` (JSONB) and validated on the way in. An
invalid stored config raises rather than silently falling back to defaults: a
merchant who thinks they capped attempts at 2 must never quietly get 3.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.domain.enums import InterventionStrategy


class PolicyConfig(BaseModel):
    """Bounds on what the agent may do without a human.

    Every field here is a safety limit. The defaults are the spec's FR-7 values.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    #: Spec FR-7: "max automated attempts per case: 3".
    max_automated_attempts: int = Field(default=3, ge=0, le=10)

    #: Minimum gap between two automated actions on the same case. Also enforces
    #: the "max reminders per 24h: 1" rule, since at most one action may be
    #: taken per cooldown window.
    cooldown_hours: int = Field(default=24, ge=0, le=720)

    #: Below this recovery probability, the agent must not act alone.
    min_auto_action_confidence: float = Field(default=0.35, ge=0.0, le=1.0)

    #: Cases at or above this amount always go to a human when confidence is
    #: below ``min_auto_action_confidence``. Mirrors the merchant column so the
    #: engine reads one object rather than two sources of truth.
    high_value_threshold_paise: int = Field(default=2_500_000, ge=0)

    #: Stop when expected net recovery does not clear this bar. Default 0 means
    #: "must be strictly positive" (spec FR-7).
    min_expected_net_recovery_paise: int = Field(default=0, ge=0)

    #: Retry backoff. Attempt N waits ``base * 2^(N-1)``, capped.
    backoff_base_hours: int = Field(default=6, ge=1, le=168)
    backoff_cap_hours: int = Field(default=72, ge=1, le=720)

    #: Strategies this merchant permits. A strategy absent here is blocked no
    #: matter how attractive its expected value.
    enabled_strategies: frozenset[InterventionStrategy] = Field(
        default=frozenset(
            {
                InterventionStrategy.WAIT_AND_RETRY,
                InterventionStrategy.SEND_REMINDER_SIMULATED,
                InterventionStrategy.REQUEST_ALTERNATE_METHOD,
                InterventionStrategy.CREATE_PAYMENT_LINK,
            }
        )
    )

    @field_validator("enabled_strategies", mode="before")
    @classmethod
    def _coerce_strategies(cls, value: Any) -> Any:
        # JSONB round-trips a set as a list, so accept either shape.
        if isinstance(value, list | set | tuple):
            return frozenset(InterventionStrategy(item) for item in value)
        return value

    @model_validator(mode="after")
    def _cap_must_not_undercut_base(self) -> PolicyConfig:
        if self.backoff_cap_hours < self.backoff_base_hours:
            raise ValueError(
                f"backoff_cap_hours ({self.backoff_cap_hours}) cannot be lower than "
                f"backoff_base_hours ({self.backoff_base_hours})"
            )
        return self

    def backoff_hours_for_attempt(self, attempt_count: int) -> int:
        """Capped exponential backoff before attempt number ``attempt_count + 1``.

        Spec FR-7 requires "exponential/capped backoff for retries". The first
        attempt waits nothing; each subsequent one doubles, up to the cap.
        """
        if attempt_count <= 0:
            return 0
        doubled: int = self.backoff_base_hours * (2 ** (attempt_count - 1))
        return min(doubled, self.backoff_cap_hours)

    @classmethod
    def from_merchant(
        cls, policy_config: dict[str, Any] | None, high_value_threshold_paise: int
    ) -> PolicyConfig:
        """Build from the merchant row.

        The threshold lives in its own column, so it is injected here rather than
        being duplicated in the JSONB blob.
        """
        data = dict(policy_config or {})
        data["high_value_threshold_paise"] = high_value_threshold_paise
        return cls.model_validate(data)
