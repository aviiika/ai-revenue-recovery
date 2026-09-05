"""Two demo commands, so a live walkthrough needs no hand-escaped JSON.

    python -m scripts.demo new  demo_paylink_004
    python -m scripts.demo pay  demo_paylink_004

``new`` ingests a fresh recovery case and evaluates it, leaving it in
ACTION_SELECTED / CREATE_PAYMENT_LINK and printing the dashboard URL. ``pay``
signs and posts a ``payment_link.paid`` webhook for that case's most recent
intervention, which is what actually records the recovery.

Why this exists: the equivalent curl one-liner needs JSON escaped through
PowerShell quoting, and a webhook additionally needs an HMAC-SHA256 signature
over the exact request bytes. Both are easy to get wrong in front of an
audience.

This talks to the running API over HTTP rather than to the database, so it
exercises the same path a real Razorpay delivery would. The only thing it reads
directly from the database is the intervention's idempotency key, which is not
exposed as a query parameter anywhere.

**It signs with the local webhook secret.** That is what makes it a stand-in for
Razorpay in test mode -- and the reason it is a demo script rather than
application code.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import urllib.error
import urllib.request
from typing import Any

from sqlalchemy import create_engine, text

from app.core.config import get_settings

DEFAULT_API = "http://localhost:8000"
DASHBOARD = "http://localhost:3000"

#: Mirrors the demo case in the README: a large insufficient-funds failure on a
#: subscription, which policy resolves to CREATE_PAYMENT_LINK.
DEFAULT_AMOUNT_PAISE = 2_928_835


def _post(url: str, body: bytes, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"API returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"Cannot reach the API at {url}. Is uvicorn running in apps/api?\n  {exc.reason}"
        ) from exc
    return payload if isinstance(payload, dict) else {}


def _get(url: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url) as response:
            payload = json.loads(response.read())
    except urllib.error.URLError as exc:
        raise SystemExit(f"Cannot reach the API at {url}: {exc.reason}") from exc
    return payload if isinstance(payload, dict) else {}


def _find_case(api: str, external_id: str) -> dict[str, Any]:
    listing = _get(f"{api}/api/v1/cases?limit=500")
    items: list[dict[str, Any]] = listing.get("items", [])
    for item in items:
        if item.get("source_external_id") == external_id:
            return item
    raise SystemExit(
        f"No case with source_external_id {external_id!r} in the most recent 500. "
        f"Create one first: python -m scripts.demo new {external_id}"
    )


def command_new(args: argparse.Namespace) -> None:
    body = json.dumps(
        {
            "cases": [
                {
                    "source_type": "SUBSCRIPTION",
                    "source_external_id": args.external_id,
                    "amount_at_risk_paise": args.amount_paise,
                    "detected_at": args.detected_at,
                    "failure_category": "INSUFFICIENT_FUNDS",
                    "failure_reason_code": "BAD_REQUEST_ERROR",
                    "is_synthetic": True,
                    "attempt_count": 0,
                }
            ]
        }
    ).encode()

    result = _post(f"{args.api}/api/v1/cases/batch", body, {"Content-Type": "application/json"})
    if result.get("duplicates"):
        # Ingestion is idempotent, so a repeated id is reported rather than
        # creating a second case. Stop here: the existing case has usually moved
        # on already, and re-evaluating a terminal one only raises a 409 that
        # reads like a failure of this script rather than the expected outcome.
        existing = _find_case(args.api, args.external_id)
        print(
            f"A case with id {args.external_id!r} already exists and is "
            f"{existing['current_state']}; nothing was created.\n"
            f"Use a different id (for example {args.external_id}_b) for a fresh one.\n"
            f"\nExisting case: {args.dashboard}/cases/{existing['id']}"
        )
        return

    print(f"Ingested {args.external_id}")

    case = _find_case(args.api, args.external_id)
    decision = _post(
        f"{args.api}/api/v1/cases/{case['id']}/evaluate", b"", {"Content-Type": "application/json"}
    ).get("decision", {})

    print(f"  amount      {case['amount_at_risk']['formatted']}")
    print(f"  strategy    {decision.get('recommended_strategy')}")
    print(f"  next state  {decision.get('next_state')}")
    if decision.get("decisive_rule"):
        print(f"  rule        {decision['decisive_rule']}")
    print(f"\nOpen: {args.dashboard}/cases/{case['id']}")

    if decision.get("next_state") != "ACTION_SELECTED":
        print(
            "\nPolicy did not select an executable action, so the Create Payment "
            "Link button will not appear on this case."
        )


def command_pay(args: argparse.Namespace) -> None:
    settings = get_settings()
    secret = settings.razorpay_webhook_secret
    if not secret:
        raise SystemExit(
            "RAZORPAY_WEBHOOK_SECRET is not set in apps/api/.env, so the API "
            "refuses unverified events and this script cannot sign one."
        )

    case = _find_case(args.api, args.external_id)
    case_id, amount = case["id"], case["amount_at_risk"]["paise"]

    # The reference_id that ties a payment to an attempt is the intervention's
    # idempotency key, which no endpoint exposes -- so read it directly.
    engine = create_engine(settings.database_url, future=True)
    with engine.connect() as connection:
        key = connection.execute(
            text(
                "SELECT idempotency_key FROM interventions "
                "WHERE recovery_case_id = :case_id "
                "ORDER BY attempt_number DESC LIMIT 1"
            ),
            {"case_id": case_id},
        ).scalar_one_or_none()
    engine.dispose()

    if key is None:
        raise SystemExit(
            f"Case {args.external_id} has no intervention yet. Press "
            '"Create Razorpay Payment Link" on the case first -- a payment '
            "cannot arrive for an attempt that was never made."
        )

    event = {
        "entity": "event",
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": f"plink_demo_{args.external_id}",
                    "amount": amount,
                    "amount_paid": amount,
                    "currency": "INR",
                    "status": "paid",
                    "reference_id": key,
                }
            },
            "payment": {"entity": {"id": f"pay_demo_{args.external_id}", "amount": amount}},
        },
    }
    # Signed over these exact bytes: re-serialising would change them and the
    # signature would fail, which is the whole point of the check.
    raw = json.dumps(event).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()

    result = _post(
        f"{args.api}/api/v1/webhooks/razorpay",
        raw,
        {
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
            "x-razorpay-event-id": args.event_id or f"evt_demo_{args.external_id}",
        },
    )
    print(f"Webhook accepted: {result.get('action') or result}")
    if result.get("duplicate"):
        print(
            "That event id was already delivered, so it was deduplicated and "
            "nothing changed -- pass --event-id to send a distinct one."
        )
    print(f"\nOpen: {args.dashboard}/cases/{case_id}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.demo", description=__doc__)
    parser.add_argument("--api", default=DEFAULT_API, help="API base URL")
    parser.add_argument("--dashboard", default=DASHBOARD, help="Dashboard base URL")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="ingest and evaluate a fresh payment-link case")
    new.add_argument("external_id")
    new.add_argument("--amount-paise", type=int, default=DEFAULT_AMOUNT_PAISE)
    new.add_argument("--detected-at", default="2026-09-05T06:00:00Z")
    new.set_defaults(func=command_new)

    pay = sub.add_parser("pay", help="sign and post a payment_link.paid webhook")
    pay.add_argument("external_id")
    pay.add_argument("--event-id", default=None, help="override the dedup key")
    pay.set_defaults(func=command_pay)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
