# AI Revenue Recovery Agent

A bounded, auditable agent that detects revenue at risk, diagnoses why it failed,
estimates recoverability, selects a policy-compliant intervention, measures money
actually recovered, and keeps a complete audit trail.

Built for the Razorpay Buildathon. See [`project-spec.md`](project-spec.md) for the
architectural source of truth.

> **All demo data is synthetic.** The system ships with a deterministic generator
> and labels every generated record `is_synthetic`. Metrics computed on it describe
> a controlled simulation, never real-world production performance.

---

## Current status

Slice 1 of the build is complete: **a case can be ingested, holds a lawful state,
and is fully auditable, end to end.**

| Area | State |
|---|---|
| Money as integer paise, float-refusing `Money` type | Done |
| `RecoveryCase` state machine, illegal transitions rejected | Done |
| Append-only audit trail with redaction + correlation IDs | Done |
| Deterministic synthetic generator (fixed seed) | Done |
| Case ingestion / list / detail APIs, idempotent batch | Done |
| Policy engine: 13 rules, stopping rules, EV gates, cooldown, backoff | Done |
| Recovery scoring with deterministic baseline + ML seam | Done |
| ML baseline: logistic regression, isotonic calibration, EV thresholding | Done |
| Model card, leakage guards, graceful degradation | Done |
| Overview + intervention metrics endpoints | Done |
| Dashboard: overview, case table, case detail + audit timeline, model page | Done |
| Interventions with DB-enforced idempotency and execution-time re-checks | Done |
| Deterministic simulator, randomised holdout, incremental measurement | Done |
| Agent vs baseline comparison; Celery worker wrapping the orchestrator | Done |
| Razorpay test mode: Payment Links, webhook verification, dedup, health | Done |
| Human review queue: approve / override / reject, fully audited | Done |
| Policy settings API and screen, validated not clamped | Done |
| LLM explanations with structured output and template fallback | Done |
| Evaluate / stop / evaluate-batch endpoints | Done |
| PostgreSQL schema + Alembic migration | Done |
| 233 tests, ruff clean, mypy strict clean, frontend builds clean | Done |
| Demo hardening: E2E test, demo script, final metrics | Not yet — see [Roadmap](#roadmap) |

---

## Prerequisites

- **Python 3.12+** (this repo is developed against 3.13)
- **Docker Desktop** (for PostgreSQL 16 + Redis)
- **Node.js 20+** (for the dashboard, arriving in Slice 2)

## Quick start

**1. Start the datastores**

```bash
docker compose up -d
```

This brings up PostgreSQL 16 on `5432` and Redis on `6379`, and creates both the
`revrec` and `revrec_test` databases. Wait for both to report healthy:

```bash
docker compose ps
```

**2. Configure the environment**

```bash
cp .env.example apps/api/.env
```

The defaults in `.env.example` match the Compose services, so it works as-is for
local development. No secrets are required to run Slice 1 — Razorpay and LLM keys
are optional and the system runs in deterministic degraded mode without them.

**3. Create the backend virtualenv and install**

On Windows, pin the interpreter explicitly — a bare `python` may resolve to an
older version:

```bash
py -3.13 -m venv apps/api/.venv
```

```bash
apps/api/.venv/Scripts/python.exe -m pip install -e "apps/api[dev]" -e .
```

On macOS/Linux:

```bash
python3.13 -m venv apps/api/.venv && apps/api/.venv/bin/python -m pip install -e "apps/api[dev]" -e .
```

**4. Apply migrations**

```bash
cd apps/api && .venv/Scripts/python.exe -m alembic upgrade head
```

**5. Run the API**

```bash
cd apps/api && .venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
```

Interactive API docs: <http://localhost:8000/docs>

**6. Seed a demo batch**

```bash
curl -X POST http://localhost:8000/api/v1/demo/seed -H "Content-Type: application/json" -d "{\"count\":120,\"reset\":true}"
```

This creates 120 synthetic cases worth roughly ₹8.9 lakh at risk. The same seed
always produces the same cases, so the demo is reproducible.

**7. Run the agent over the batch**

```bash
curl -X POST http://localhost:8000/api/v1/cases/evaluate-batch -H "Content-Type: application/json" -d "{\"limit\":200}"
```

Each case is diagnosed, scored, and put through the policy engine. A representative
run on the default seed:

```text
at risk         : INR 8,90,932.32
expected net    : INR 3,22,506.13
action selected : 75
escalated       : 39
stopped         :  6

R02_DO_NOT_CONTACT                6    opted-out customers, stopped regardless of value
R04_MAX_ATTEMPTS_REACHED         11    attempt budget spent, handed to a human
R05_HIGH_VALUE_LOW_CONFIDENCE     1    large ticket the agent will not touch alone
R06_BELOW_CONFIDENCE_THRESHOLD   27    routed to a human
R11_TRANSIENT_FAILURE_RETRY      28    retried
R12_ACTIONABLE_FAILURE_CONTACT   47    payment link / alternate method
```

These figures are produced by the code, not hardcoded. Re-running with the same
seed reproduces them exactly.

---

## Verifying it works

```bash
curl http://localhost:8000/health
```

```bash
curl "http://localhost:8000/api/v1/cases?limit=5"
```

Fetch one case with its full audit trail and legal next states:

```bash
curl "http://localhost:8000/api/v1/cases/<case-id>"
```

## Running the checks

Tests default to SQLite in-memory, so they need no running container:

```bash
cd apps/api && .venv/Scripts/python.exe -m pytest -q
```

To exercise the real PostgreSQL dialect (JSONB, CHECK constraints) as CI does, set
`TEST_DATABASE_URL` first:

```bash
cd apps/api && TEST_DATABASE_URL=postgresql+psycopg://revrec:revrec@localhost:5432/revrec_test .venv/Scripts/python.exe -m pytest -q
```

Lint and typecheck:

```bash
cd apps/api && .venv/Scripts/python.exe -m ruff check app tests ../../ml && .venv/Scripts/python.exe -m mypy app
```

## Running the dashboard

With the API running, in a second terminal:

```bash
cd apps/web && npm install && npm run dev
```

Then open <http://localhost:3000>. Three screens:

- **Overview** — KPI cards, recovery funnel, revenue at risk by failure reason,
  detected-vs-recovered over time. Buttons to seed data and run the agent.
- **Recovery cases** — filterable, paginated table with amount, diagnosis,
  recovery probability, state and attempts.
- **Case detail** — financial context, model assessment, operator actions, and
  the complete append-only audit timeline.
- **Model metrics** — the training report, including calibration.

The frontend performs no money arithmetic and holds no policy logic. Amounts
arrive pre-formatted from the backend, and every state, rule and probability the
UI shows was computed server-side.

## Human review and policy

Escalated cases land in the review queue at <http://localhost:3000/reviews>,
ordered by money at stake. A reviewer can **approve** the agent's proposal,
**override** it with another permitted strategy, or **reject** and stop the case.
Notes are mandatory and every decision is audited against the reviewer.

Safety bounds are editable at <http://localhost:3000/policies> — attempt caps,
cooldown, confidence floor, high-value threshold, backoff, and which actions are
enabled at all. Values are **validated, not clamped**: an out-of-range setting is
refused with the reason, so a limit you set is the limit you get.

### LLM explanations (optional)

Explanations default to deterministic templates. To use an LLM instead, set
`LLM_PROVIDER=anthropic`, `LLM_API_KEY` and optionally `LLM_MODEL`. No SDK is
required — the integration uses `httpx`, which is already a dependency.

The LLM writes prose only. Its response schema has no field for an action, so it
cannot change what the agent does, and any failure falls back to the template.

## Razorpay test mode

The system runs fully without Razorpay credentials — the agent executes through
the simulated provider. To exercise the real test-mode integration, set
`RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` and `RAZORPAY_WEBHOOK_SECRET` in
`apps/api/.env`. `RAZORPAY_MODE` must be `test`; the app refuses to boot
otherwise.

Check what is configured (credentials are never returned):

```bash
curl http://localhost:8000/api/v1/integrations/health
```

Webhooks arrive at `POST /api/v1/webhooks/razorpay`. Signature is verified as
HMAC-SHA256 over the **raw** request body, duplicates are rejected via the
`x-razorpay-event-id` header, and PII is redacted before the event is stored.
Handled events: `payment.failed`, `payment_link.paid`, `subscription.charged`,
`subscription.pending`, `subscription.halted`. Anything else is stored and
acknowledged with a 200 — refusing it would make Razorpay retry for 24 hours.

For local delivery you need a public HTTPS URL. Follow Razorpay's current
guidance on tunnelling rather than assuming a particular tunnel domain works.

## Training the model

```bash
apps/api/.venv/Scripts/python.exe -m ml.src.train --count 6000
```

Deterministic given the seed. Artifacts land in `ml/artifacts/` (gitignored —
rebuild rather than commit binaries). **The system runs without this step**: with
no artifact present it falls back to the deterministic baseline scorer, which is
the normal state of a fresh clone.

Full metrics, calibration, the threshold trade-off and the causal caveat are in
[`docs/model-card.md`](docs/model-card.md). Headline, on held-out synthetic test
data: PR-AUC **0.675**, Brier **0.203**, ECE **0.052**, with the interpretable
logistic baseline beating gradient boosting by 0.02 PR-AUC.

Inspect the synthetic population without touching a database:

```bash
apps/api/.venv/Scripts/python.exe -m ml.src.generate_data --count 1000 --summary
```

---

## API surface (Slice 1)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness plus dependency status |
| `GET` | `/api/v1/cases` | Filtered, paginated case list |
| `GET` | `/api/v1/cases/{id}` | Case detail with audit trail and legal transitions |
| `POST` | `/api/v1/cases/batch` | Idempotent batch ingestion |
| `POST` | `/api/v1/cases/{id}/evaluate` | Run diagnose → score → policy on one case |
| `POST` | `/api/v1/cases/{id}/stop` | Operator kill switch |
| `POST` | `/api/v1/cases/evaluate-batch` | Run the agent across all pending cases |
| `GET` | `/api/v1/metrics/overview` | KPIs, funnel, failure reasons, time series |
| `GET` | `/api/v1/metrics/interventions` | Per-strategy performance from the audit trail |
| `GET` | `/api/v1/metrics/models` | Training report; `trained: false` when untrained |
| `GET` | `/api/v1/metrics/experiments` | Agent vs baseline, and holdout-based incremental recovery |
| `POST` | `/api/v1/demo/run-batch` | One orchestration pass (gated) |
| `POST` | `/api/v1/demo/simulate-outcomes` | Reveal deterministic outcomes (gated) |
| `POST` | `/api/v1/demo/run-full-cycle` | Act → observe → retry to convergence (gated) |
| `POST` | `/api/v1/webhooks/razorpay` | Signed webhook intake, deduplicated |
| `GET` | `/api/v1/integrations/health` | Integration status; never returns credentials |
| `GET` | `/api/v1/reviews` | Human review queue, largest exposure first |
| `POST` | `/api/v1/reviews/{id}/approve` | Let the agent's proposal stand |
| `POST` | `/api/v1/reviews/{id}/override` | Substitute a permitted strategy |
| `POST` | `/api/v1/reviews/{id}/reject` | Stop the case |
| `GET` `PUT` | `/api/v1/policies` | Read and update the agent's safety bounds |
| `POST` | `/api/v1/demo/seed` | Seed deterministic synthetic cases (gated) |
| `POST` | `/api/v1/demo/reset` | Delete synthetic cases only (gated) |

Demo endpoints return 404 unless `DEMO_ENDPOINTS_ENABLED=true`.

## Repository layout

```text
apps/api/          FastAPI backend (domain logic is framework-independent)
  app/core/          money, settings, structured logging
  app/domain/        state machine, case service, audit service
  app/db/            SQLAlchemy models and session management
  app/api/           HTTP layer only — no business logic
ml/src/            shared synthetic generator; ML pipeline lands here in M2
infra/             Docker init scripts
docs/              architecture, model card, demo script
```

## Design rules this codebase enforces

These are not stylistic preferences; they are checked by tests.

- **Money is never a float.** `Money` refuses float construction and float scaling
  outright, and every monetary database column is `BigInteger` paise.
- **Illegal state transitions are rejected**, and the rejected *attempt* is itself
  written to the audit trail.
- **The audit trail is append-only.** The audit module exposes no update or delete
  verb, and payloads are redacted before they are written.
- **Ingestion is idempotent** per `(merchant, source_external_id)`, so a redelivered
  webhook cannot double-count revenue.
- **Deterministic mappings beat model guesses** where a reason code is known.
- **The policy engine has final authority.** It is a pure function with no I/O; a
  model ranks options and an LLM explains them, but neither can act.
- **Opt-out is checked before economics**, so a profitable case belonging to an
  opted-out customer is still stopped. This ordering is tested explicitly.
- **Outcome fields are never model features.** A leakage guard fails the training
  run if one appears, and a test asserts the guard itself fires.
- **The system degrades rather than crashes.** A missing, corrupt, or
  unimportable model falls back to the deterministic scorer; an unavailable LLM
  falls back to deterministic templates.
- **Guardrails bind humans too.** A reviewer can overrule the agent's judgement
  but not an opt-out, a settled case, or a merchant-disabled strategy.
- **The LLM has no field in which to name an action.** It writes explanations;
  the policy engine decides.
- **Synthetic data is labelled everywhere** it appears, including in API responses.

## Roadmap

Slice 1 is done. Remaining milestones, in order:

1. **Demo hardening** — end-to-end tests, demo script, final metrics

## Licence and data

No real customer data, card data, or live credentials exist in this repository.
The `Customer` model deliberately stores no email address, phone number, or payment
instrument — only an opaque reference and contactability flags.
