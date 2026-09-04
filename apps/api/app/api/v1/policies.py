"""Merchant policy settings (spec section 14, section 16).

Editable but validated. `PolicyConfig` rejects an out-of-range value rather than
clamping it: a merchant who thinks they capped attempts at 2 must never silently
get 3, and a validation error is the only way they find out they were wrong.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.schemas import PolicyResponse, PolicyUpdateRequest
from app.db.models import Merchant
from app.domain.enums import InterventionStrategy
from app.domain.policies.config import PolicyConfig

router = APIRouter(prefix="/policies", tags=["policies"])


def _merchant(session: Session) -> Merchant:
    merchant = session.execute(select(Merchant).order_by(Merchant.created_at)).scalars().first()
    if merchant is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No merchant configured")
    return merchant


def _to_response(merchant: Merchant, config: PolicyConfig) -> PolicyResponse:
    return PolicyResponse(
        merchant_id=merchant.id,
        max_automated_attempts=config.max_automated_attempts,
        cooldown_hours=config.cooldown_hours,
        min_auto_action_confidence=config.min_auto_action_confidence,
        high_value_threshold_paise=config.high_value_threshold_paise,
        min_expected_net_recovery_paise=config.min_expected_net_recovery_paise,
        backoff_base_hours=config.backoff_base_hours,
        backoff_cap_hours=config.backoff_cap_hours,
        enabled_strategies=sorted(config.enabled_strategies),
        available_strategies=sorted(
            [
                InterventionStrategy.WAIT_AND_RETRY,
                InterventionStrategy.SEND_REMINDER_SIMULATED,
                InterventionStrategy.REQUEST_ALTERNATE_METHOD,
                InterventionStrategy.CREATE_PAYMENT_LINK,
            ]
        ),
    )


@router.get("", response_model=PolicyResponse)
def get_policies(session: Session = Depends(get_db)) -> PolicyResponse:
    """Current stopping rules and bounds."""
    merchant = _merchant(session)
    config = PolicyConfig.from_merchant(merchant.policy_config, merchant.high_value_threshold_paise)
    return _to_response(merchant, config)


@router.put("", response_model=PolicyResponse)
def update_policies(
    payload: PolicyUpdateRequest, session: Session = Depends(get_db)
) -> PolicyResponse:
    """Update the merchant's policy, validating before it takes effect.

    The change is audited on the merchant, not on any single case, because it
    alters how every future decision is made.
    """
    merchant = _merchant(session)
    current = PolicyConfig.from_merchant(
        merchant.policy_config, merchant.high_value_threshold_paise
    )

    merged: dict[str, Any] = current.model_dump()
    merged.update(payload.model_dump(exclude_unset=True))

    try:
        updated = PolicyConfig.model_validate(merged)
    except ValidationError as exc:
        # Surface exactly which bound was violated rather than a generic 400.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[
                {"field": ".".join(str(p) for p in e["loc"]), "error": e["msg"]}
                for e in exc.errors()
            ],
        ) from exc

    stored = updated.model_dump()
    # The threshold lives in its own column; keep the JSONB free of it so there
    # is exactly one source of truth.
    stored.pop("high_value_threshold_paise", None)
    stored["enabled_strategies"] = sorted(str(s) for s in updated.enabled_strategies)
    # No metadata in the blob: PolicyConfig forbids unknown keys, so an
    # `updated_at` written here would make the very next read fail validation.
    # When change history is needed it belongs in the audit trail, not the
    # config object.

    merchant.policy_config = stored
    merchant.high_value_threshold_paise = updated.high_value_threshold_paise
    session.add(merchant)
    session.flush()

    return _to_response(merchant, updated)
