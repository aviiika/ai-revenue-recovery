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
| Payment Link execution from the dashboard, with simulated/live labelling | Done |
| Incremental impact screen: holdout arms, agent vs baseline, caveats | Done |
| Integration health strip: which execution path the demo is on | Done |
| Interventions with DB-enforced idempotency and execution-time re-checks | Done |
| Deterministic simulator, randomised holdout, incremental measurement | Done |
| Agent vs baseline comparison; Celery worker wrapping the orchestrator | Done |
| Razorpay test mode: Payment Links, webhook verification, dedup, health | Done |
| Human review queue: approve / override / reject, fully audited | Done |
| Policy settings API and screen, validated not clamped | Done |
| LLM explanations with structured output and template fallback | Done |
| End-to-end demo test, demo script, API reference | Done |
| Evaluate / stop / evaluate-batch endpoints | Done |
| PostgreSQL schema + Alembic migration | Done |
| 260 backend + 7 frontend tests, ruff clean, mypy strict clean, builds clean | Done |
| **All seven milestones complete** | — |

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

Frontend tests, lint, typecheck and build:

```bash
cd apps/web && npm test && npx eslint src --max-warnings=0 && npx tsc --noEmit && npx next build
```

CI runs all of the above on every push.

## Running the dashboard

With the API running, in a second terminal:

```bash
cd apps/web && npm install && npm run dev
```

Then open <http://localhost:3000>. Six screens:

- **Overview** — KPI cards, recovery funnel, revenue at risk by failure reason,
  detected-vs-recovered over time. Buttons to seed data and run the agent.
- **Recovery cases** — filterable, paginated table with amount, diagnosis,
  recovery probability, state and attempts.
- **Case detail** — financial context, model assessment, operator actions, and
  the complete append-only audit timeline.
- **Model metrics** — the training report, including calibration.
- **Incremental impact** — the randomised holdout arms side by side, the causal
  lift, the agent-versus-baseline comparison, and the caveats that belong with
  them. This is the page that answers "would they have paid anyway?".
- **Policies** — merchant thresholds, editable and validated server-side.

The Overview also carries an execution-path strip reading either
`SIMULATED EXECUTION` or `RAZORPAY TEST MODE`, so nobody has to guess which path
a demo is on.

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

### Creating a Payment Link from the dashboard

This is the flow judges should see. It works with or without credentials, and
the UI says which path it took rather than making you infer it.

1. Open a case whose policy decision is `CREATE_PAYMENT_LINK`. The **Operator
   actions** panel shows the selected intervention and its expected net
   recovery; the button appears only when policy actually authorised that
   action.
2. Press **Create Razorpay Payment Link**.
3. A **Razorpay Payment Link** panel appears, badged either `RAZORPAY TEST MODE`
   with an **Open Payment Link** button, or `SIMULATED` with an explanation and
   no link. A URL is only ever rendered when the provider returned one — the
   app never constructs a payment URL itself.
4. The case moves to `OBSERVING` and recovered revenue stays at ₹0. **Creating a
   link is not a recovery.** The panel says so.
5. Pay the link in Razorpay test mode, or post a signed `payment_link.paid`
   webhook. Only then does the case become `RECOVERED`, the amount count toward
   revenue recovered, and `RECOVERY_RECORDED` appear on the audit timeline.

The link's `reference_id` carries the intervention's idempotency key, which is
how a payment ties back to the exact attempt that created it. Pressing the
button twice cannot create a second link: the key has a UNIQUE constraint.

If policy refuses — a cooldown, an escalation, an already-recovered case — the
panel reports the engine's own explanation and decisive rule ID instead of a
generic failure. A refusal and a provider error are shown differently, because
they mean opposite things.

### Preparing a fresh case for this demo

Running the full cycle puts every case into its 24-hour cooldown, so there may
be nothing left to execute. Two helper commands mint a case and settle it,
which avoids hand-escaping JSON and hand-computing an HMAC signature in front
of an audience. Run both from `apps/api`:

```bash
.venv\Scripts\python.exe -m scripts.demo new demo_paylink_004
```

That ingests the case, evaluates it, and prints its dashboard URL along with the
strategy policy chose. Press **Create Razorpay Payment Link** on that page, then:

```bash
.venv\Scripts\python.exe -m scripts.demo pay demo_paylink_004
```

`pay` signs a `payment_link.paid` webhook with `RAZORPAY_WEBHOOK_SECRET` and
posts it to the same endpoint Razorpay would, so the case settles through the
real verification path. It refuses if no intervention exists yet — a payment
cannot arrive for an attempt that was never made.

Use a new id each time; ingestion is idempotent and reports a duplicate rather
than creating a second case. With real credentials configured you can pay the
link in Razorpay's test mode instead and skip `pay` entirely.

> These commands are PowerShell. In PowerShell `curl` is an alias for
> `Invoke-WebRequest` and will not accept curl's flags — if you prefer raw HTTP,
> call `curl.exe` explicitly.

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

## API surface

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness plus dependency status |
| `GET` | `/api/v1/cases` | Filtered, paginated case list |
| `GET` | `/api/v1/cases/{id}` | Case detail with audit trail and legal transitions |
| `POST` | `/api/v1/cases/batch` | Idempotent batch ingestion |
| `POST` | `/api/v1/cases/{id}/evaluate` | Run diagnose → score → policy on one case |
| `POST` | `/api/v1/cases/{id}/execute` | Execute the selected intervention; creates the Payment Link |
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

## Measured results

From an actual run on seed `20260902`, 600 synthetic cases, measured through the
API rather than transcribed by hand. The same seed gives the same population.

| | |
|---|---|
| Revenue at risk | ₹47,24,978.30 |
| Revenue recovered (gross) | ₹12,38,986.52 — 26.2% by value |
| **Incremental recovery (holdout-measured)** | **+19.2 pp, ₹4,44,582.44** |
| Net of intervention spend | ₹4,43,345.94 |
| Holdout arm | 83 cases, **zero** actions executed against it |
| Cases recovered / escalated / stopped | 173 / 193 / 88 |
| Agent actions vs naive baseline | 249 fewer interventions |

**Read the incremental figure, not the gross one.** Gross recovery includes
customers who would have paid anyway. Twenty percent of cases are randomised
into a holdout that is never contacted, and the gap between arms is what the
agent actually caused.

**Two estimators, and why the count-based one leads.** The incremental figure is
the difference in the *share of cases recovered* between arms — 45.7% treated
against 26.5% withheld. The value-weighted difference on the same run reads
−22.8 pp, and the dashboard shows it saying so. That is not a contradiction: the
value rate is a ratio of sums, case amounts are heavy-tailed by design (a B2B
invoice runs 3–9× a consumer payment), and a handful of large withheld cases
settling one way moves it by tens of points. The count difference measures the
effect on whether a case recovers at all, and is what gets monetised against the
treated arm's value at risk. Both are on the Incremental impact screen with the
caveat attached; neither is quoted alone.

Everything above is measured inside a simulation and carries no confidence
interval. It is not evidence of real-world uplift.

**The agent scores below the baseline on raw expected value**, and we have not
tuned the cost assumptions to hide that. At ₹5 per message against a four-figure
ticket, contacting everyone is arithmetically optimal. What the agent buys is
249 fewer customer contacts, human oversight on every low-confidence decision,
and guardrails that hold.

See [`docs/demo-script.md`](docs/demo-script.md) for the 4-minute walkthrough
and [`docs/api.md`](docs/api.md) for the endpoint reference.

## Roadmap

All seven milestones from the spec are complete. Natural next steps, none
started:

1. **A live Razorpay call** — the adapter is exercised against a mock
   transport running the real client code, but no request has yet reached
   Razorpay's servers. Add test-mode keys to close this.
2. **Authentication** — reviewer identity is currently self-asserted
3. **Power-analysed holdout** — the 20% share is fixed, not sized. At 600 cases
   the holdout is 83, which is enough for the count-based estimate and still
   not enough for the value-weighted one
4. **A `ModelPrediction` table** — prediction history currently lives in
   `CASE_SCORED` audit payloads, which is auditable but not queryable for drift
5. **Per-strategy response modelling** — needs counterfactual outcome data
6. **Deployment** — no hosting is configured

## Licence and data

No real customer data, card data, or live credentials exist in this repository.
The `Customer` model deliberately stores no email address, phone number, or payment
instrument — only an opaque reference and contactability flags.
