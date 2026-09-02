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
| PostgreSQL schema + Alembic migration | Done |
| 76 tests, ruff clean, mypy strict clean | Done |
| Policy engine, ML scoring, interventions, Razorpay, dashboard | Not yet — see [Roadmap](#roadmap) |

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

This creates 120 synthetic cases worth roughly ₹8.8 lakh at risk. The same seed
always produces the same cases, so the demo is reproducible.

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
- **Synthetic data is labelled everywhere** it appears, including in API responses.

## Roadmap

Slice 1 is done. Remaining milestones, in order:

1. **Policy engine** — stopping rules, cooldowns, do-not-contact, EV gates
2. **ML baseline** — logistic regression, calibration, business-value thresholding
3. **Dashboard** — Next.js, case table, audit timeline, KPI cards
4. **Orchestrator + simulator** — intervention execution, batch runs, agent vs baseline
5. **Razorpay test mode** — Payment Links, webhook signature verification, idempotency
6. **Human review queue** — approve, override, stop, escalate
7. **Demo hardening** — E2E tests, model card, demo script

## Licence and data

No real customer data, card data, or live credentials exist in this repository.
The `Customer` model deliberately stores no email address, phone number, or payment
instrument — only an opaque reference and contactability flags.
