# API reference

Base URL `http://localhost:8000`. Interactive docs at `/docs`.

**Money crosses the boundary as integer paise.** Every monetary field is
`{ "paise": int, "formatted": "INR 1,23,456.78" }`. The frontend renders
`formatted` and never does arithmetic — every figure a user sees was computed
server-side in integer paise.

**Correlation IDs.** Send `X-Correlation-ID` and it is echoed back and attached
to every log line and audit event for that request. Omit it and one is
generated.

---

## System

### `GET /health`

Liveness plus dependency status. Returns `503` with `status: "degraded"` if the
database is unreachable — it degrades visibly rather than disappearing.

```json
{
  "status": "ok",
  "app_env": "local",
  "database": "up",
  "razorpay_mode": "test",
  "llm_enabled": false,
  "demo_endpoints_enabled": true
}
```

### `GET /api/v1/integrations/health`

Reports what is *configured*, never a credential value.

---

## Cases

### `GET /api/v1/cases`

| Query | Notes |
|---|---|
| `state` | Repeatable. Any `CaseState`. |
| `source_type` | Repeatable. `PAYMENT`, `SUBSCRIPTION`, `CHECKOUT`, `INVOICE`. |
| `failure_category` | Repeatable. |
| `min_amount_paise`, `max_amount_paise` | Inverted range returns `422`. |
| `limit`, `offset` | Default 50 / 0. |

`contains_synthetic` on the response drives the UI's synthetic banner.

### `GET /api/v1/cases/{id}`

One case with its complete audit trail and `allowed_transitions` — the legal
next states, read from the state machine rather than re-derived by the client.

### `POST /api/v1/cases/batch`

Ingest cases. **Idempotent** per `(merchant, source_external_id)`: repeats come
back in `duplicates` rather than double-counting revenue at risk.

`detected_at` must be timezone-aware — a naive timestamp is rejected with `422`,
because cooldown windows depend on it being right.

### `POST /api/v1/cases/{id}/evaluate`

Runs diagnose → score → policy and applies the resulting transition. Returns the
full decision: recommended strategy, every candidate's economics, the rules that
fired, and which one was decisive.

`409` if the case is terminal.

### `POST /api/v1/cases/{id}/stop`

Operator kill switch. `{"reason": "...", "reviewer": "..."}`.

### `POST /api/v1/cases/evaluate-batch`

Runs the agent across pending cases. Returns aggregate counts plus `by_rule` —
which policy rules fired and how often.

---

## Metrics

### `GET /api/v1/metrics/overview`

KPIs plus the four dashboard charts: `funnel`, `by_state`, `by_failure_reason`,
`over_time`.

`estimated_intervention_cost` is *estimated* from attempts and the cost table,
not measured — the field name says so on purpose.

### `GET /api/v1/metrics/interventions`

Per-strategy performance, reconstructed from `INTERVENTION_SELECTED` audit
events rather than a summary column, so reporting inherits the same
auditability guarantee as cases.

### `GET /api/v1/metrics/models`

Training report. Returns `{"trained": false, ...}` with guidance rather than
`404` when no artifact exists — "not trained yet" is information the page should
render.

### `GET /api/v1/metrics/experiments`

Agent vs baseline, plus holdout-based incremental recovery.

The `incremental` block always carries `caveat` text. **Render it with the
number.** Both arms are restricted to cases the agent *intended* to act on;
the raw arm totals are not comparable, because the treated arm accumulates
escalated and stopped cases that were never actioned.

---

## Reviews

### `GET /api/v1/reviews?status=PENDING`

The queue, ordered by money at stake. Also returns `pending`,
`value_awaiting_review` and `by_reason`.

### `POST /api/v1/reviews/{id}/approve`

Let the agent's proposal stand. `{"reviewer": "...", "notes": "..."}` — notes
are mandatory.

`409` if the case is opted out, already recovered, terminal, or the review has
no proposal (a max-attempts escalation has nothing to approve).

### `POST /api/v1/reviews/{id}/override`

Adds `"strategy"`. **Must be one the merchant has enabled** — otherwise `409`.
A human choosing a disabled strategy would make the policy engine advisory.

### `POST /api/v1/reviews/{id}/reject`

Stops the case. Always available, including where approve is blocked — stopping
is the safe direction and is never the thing refused.

---

## Policies

### `GET /api/v1/policies` · `PUT /api/v1/policies`

Read and update the agent's safety bounds. `PUT` is a partial update;
unsupplied fields are preserved, unknown fields are rejected.

Values are **validated, not clamped**. An out-of-range setting returns `422`
listing the offending field, so a merchant who sets a limit gets that limit or
an explanation — never a silently different one.

---

## Webhooks

### `POST /api/v1/webhooks/razorpay`

| Behaviour | Reason |
|---|---|
| Signature is HMAC-SHA256 over the **raw body** | Docs say do not parse before verifying; re-serialising changes the bytes |
| Deduped on `x-razorpay-event-id` | Razorpay delivers at-least-once |
| Invalid signature → `400`, still recorded | A rejected request, not a delivery failure. A forged delivery is evidence. |
| Unrecognised event → `200` | A non-2xx earns 24 hours of retries for something we will never handle |
| No secret configured → `503` | Without a secret we cannot tell a real event from anyone's POST |
| PII redacted before storage | `email`, `contact`, card fields are not needed to recover a payment |

Handled: `payment.failed`, `payment_link.paid`, `subscription.charged`,
`subscription.pending`, `subscription.halted`.

---

## Demo (gated)

All return `404` unless `DEMO_ENDPOINTS_ENABLED=true`. They create and destroy
data.

| Endpoint | Notes |
|---|---|
| `POST /api/v1/demo/seed` | Deterministic. `reset` deletes **synthetic rows only** — never data from a real webhook. |
| `POST /api/v1/demo/reset` | Deletes synthetic cases. |
| `POST /api/v1/demo/run-batch` | One orchestration pass. |
| `POST /api/v1/demo/simulate-outcomes` | Reveals deterministic outcomes. |
| `POST /api/v1/demo/run-full-cycle` | Act → observe → retry to convergence. What the demo calls. |

---

## Errors

| Status | Meaning |
|---|---|
| `400` | Webhook signature verification failed |
| `404` | Case not found, or a demo endpoint while disabled |
| `409` | Illegal state transition, terminal case, or a refused review action |
| `422` | Validation failure — naive timestamp, inverted range, invalid policy |
| `503` | Database unreachable, or webhook secret unset |

A `409` from an illegal transition names both states and why it was refused.
