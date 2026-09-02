# Architecture

Living document, updated at each milestone. Reflects what is **built**, not what is
planned; planned work is marked as such.

## 1. Shape of the system

A modular monolith plus a background worker, per spec section 6.1. Not
microservices — the coordination cost would not buy anything at this size, and a
single transaction boundary makes "state transition and its audit event land
atomically" trivially true rather than a distributed-systems problem.

```text
  Next.js dashboard  (Slice 2)
          |  REST
  +-------v---------------------------+
  | FastAPI application               |
  |   api/      HTTP only             |
  |   domain/   business rules        |  <- no FastAPI imports here
  |   db/       persistence           |
  |   core/     money, config, logs   |
  +-------+---------------------+-----+
          |                     |
    PostgreSQL 16            Redis  (worker lands in M4)
```

## 2. The decision boundary

This is the load-bearing architectural idea of the project. Three kinds of
component, with strictly different authority:

| Layer | Owns | May never |
|---|---|---|
| **Deterministic code** | case state, money arithmetic, policy rules, retry limits, eligibility, idempotency, API execution | — |
| **ML** | recovery probability, ranking, response estimates | move money, change state, bypass policy |
| **LLM** | explanations, message drafts, structured reading of unstructured notes | select an action outside the allow-list, or override the policy engine |

The agent is *bounded* precisely because the action space is a closed enum
(`InterventionStrategy`) that the policy engine filters. An LLM can rank those
options or explain them; it cannot invent a new one. That property is enforced by
types, not by prompt instructions.

## 3. Module boundaries

`app/domain/` imports nothing from `app/api/`. The state machine
(`domain/cases/state_machine.py`) imports nothing at all beyond the enums — it is a
pure function over `(source, target, reason)`. That is what makes the exhaustive
144-pair transition test possible and cheap.

The API layer translates HTTP to domain calls and back. It holds no business rules;
the one thing it decides is paging.

## 4. Money

Spec NFR Reliability requires integer paise and forbids floating point. Rather than
rely on discipline, `core/money.py` makes the wrong thing impossible:

- `Money.from_rupees()` **raises** on `float` input.
- `Money.scale()` **raises** on `float` factors.
- `Money` cannot be negative; `SignedMoney` exists separately for expected-value
  arithmetic, where negative net value is a real and meaningful outcome (it is the
  signal that triggers a stop).
- Every monetary database column is `BigInteger`. There is no `Numeric` or `Float`
  money column in the schema.
- Rounding is explicit `ROUND_HALF_UP`, and only in `scale()`, where the result is
  an estimate. Exact paths refuse to round.

`bool` is rejected as an amount too — it is a subclass of `int` in Python, so
accepting it would be a silent bug.

## 5. The state machine

States and the happy path come from spec FR-6. Three edges are permitted from
*every* non-terminal state, and the reasoning matters:

- **`-> RECOVERED`**: a payment can succeed out of band at any moment — the customer
  retries on their own, or an old link is finally paid. Refusing this edge would
  force us either to drop a real recovery or to record it in the wrong state.
- **`-> STOPPED`**: a do-not-contact flag or an operator kill switch must always be
  honourable, immediately.
- **`-> ESCALATED`**: any step can hit a condition a human must adjudicate.

What is *not* legal: skipping forward (`NEW -> ACTION_EXECUTED` would mean acting on
a case that was never scored), moving backwards, or leaving a terminal state.
`ESCALATED` is deliberately **not** terminal — a human must be able to hand a case
back to the agent.

A retry re-enters at `ACTION_SELECTED`, never at `ACTION_PENDING` or
`ACTION_EXECUTED`. This is what forces every retry to be re-evaluated against policy
rather than blindly repeated.

## 6. Audit trail

`domain/audit/service.py` exposes exactly one write verb, `record()`, and no update
or delete verb. Append-only is therefore a property of the API surface, not a
convention someone has to remember.

Each event carries a per-case monotonic `sequence`, so history replays in exact
order even when two events share a timestamp. The `(case_id, sequence)` unique
constraint is the real concurrency guard: two writers picking the same number means
one fails loudly instead of silently interleaving history.

Payloads are redacted before write, recursively, because the trail is rendered to
humans in the UI and must never become a secret-leak vector.

Rejected transitions are audited *before* the exception propagates. An attempt to do
something illegal is exactly what an operator needs to be able to see afterwards.

## 6a. The policy engine

`domain/policies/engine.py` is a **pure function**: `(snapshot, scored, config) ->
decision`. No database, no clock of its own, no I/O. That is not stylistic — it is
what makes "the LLM cannot bypass the policy engine" a structural guarantee rather
than a prompt instruction. There is no code path by which a model output reaches an
action without passing through `decide()`.

The 13 rules run in the fixed order of spec section 12. **Order is load-bearing.**
`do_not_contact` is checked before expected value, so a large, high-confidence,
obviously profitable case belonging to an opted-out customer is still stopped.
That ordering has its own test, because getting it backwards would be a
compliance failure that a naive test suite would never notice.

Every decision carries the rules that fired (stable ids like
`R02_DO_NOT_CONTACT`), the strategies that were blocked and why, and the full
economics of every candidate. That payload goes into the audit trail, which is what
makes spec FR-10 explainability achievable from stored data alone.

**Deferral is not a transition.** A cooldown means "do not act yet", which is
genuinely different from any state change. The engine expresses it as
`next_state=None` and the evaluator performs no transition. The alternative —
moving the case to `RETRY_ELIGIBLE` — was tried and rejected: the state machine
correctly refused `SCORED -> RETRY_ELIGIBLE`, because FR-6 defines
`RETRY_ELIGIBLE` as *acted, observed, may retry*, which a deferred case has not
done. The state machine caught the modelling error; the fix belonged in the engine.

## 6b. Scoring and the ML seam

`domain/scoring/service.py` separates two things that are easy to conflate:

* **Probability** is a model output, and uncertain.
* **Expected value** is deterministic arithmetic over that probability, in integer
  paise. Money is never computed by a model.

`RecoveryScorer` is a Protocol with two implementations: `DeterministicScorer`
(interpretable heuristic) and `MLScorer` (the trained classifier). Swapping the
model in changed **no policy code** — but it did require widening the protocol
from three loose arguments to a single `CaseFeatures` object, because the
original signature was too narrow to serve a real model. That is the seam
working as intended: the change was contained to the scoring module and its
adapter.

The deterministic scorer is not scaffolding to delete. It is the permanent
fallback for a missing, corrupt, or unimportable model, which the spec requires
the demo to survive.

**Dependency direction is one-way.** `apps/api/app/ml/scorer.py` imports
`ml.src`; nothing under `ml/` imports the app. The ML pipeline stays runnable on
its own, and the domain takes no import-time dependency on scikit-learn — a
deployment without it still boots on the baseline.

**Anti-skew measure.** `ml/src/features.py` is the only place features are
defined, and both training (`build_feature_frame`) and serving (`context_to_row`)
funnel through it. Training/serving skew is silent when it happens — the model
just predicts badly — so it is prevented structurally and asserted by a test that
compares the serving row's columns against the training columns.

Intervention costs are explicit per strategy, so the engine optimises expected
*net* recovery. `ESCALATE_HUMAN` is by far the most expensive, because operator
attention is the scarcest resource in a recovery operation and the system should
not spend it casually.

## 6c. The dashboard

Next.js 16 (App Router), TypeScript strict, Tailwind v4, TanStack Query,
Recharts. Three rules shape it:

**The frontend holds no domain logic.** It performs no money arithmetic — amounts
arrive as `{ paise, formatted }` and the UI renders `formatted`. It does not
decide which state transitions are legal; `allowed_transitions` comes off the
wire. It does not evaluate policy; it renders the engine's own explanation and
rule ids. This is the spec's "do not duplicate domain logic" rule applied
literally.

**State is never conveyed by colour alone.** Every status badge carries a text
label, and terminal states additionally carry a heavier border, so the interface
survives greyscale and colour-blindness. Focus rings are explicit.

**Tailwind class names are never interpolated.** Tailwind extracts classes by
scanning source text, so a name assembled at runtime is invisible to it and the
style silently never ships — a bug that looks like a design mistake. Every tone
variant is written out literally in a lookup map.

The visual language is deliberately plain: dense tables, tabular figures aligned
on the decimal, a muted palette. The spec rules out crypto-dashboard and
gradient-heavy treatments, and an operations screen showing forty rows should
not look like a warning light.

A note on component sourcing: the 21st.dev catalog was searched for a dashboard
shell and table, and the available options were consumer-fintech or
marketing-styled rather than operations software. They were not adopted. The UI
is built on the design tokens above instead.

## 7. Idempotency

Two layers, both already in place:

1. **Ingestion** is unique on `(merchant_id, source_external_id)`. A redelivered
   provider event raises `DuplicateCaseError` rather than creating a second case.
   The batch endpoint reports these as `duplicates` and treats it as success.
2. **Recovery recording** refuses to run twice on an already-`RECOVERED` case. This
   is what stops a duplicate webhook from double-counting revenue in the headline
   number.

Intervention-level idempotency keys arrive with the orchestrator in M4.

## 8. Configuration and safety gates

Settings come from the environment only (`pydantic-settings`); no credential is ever
written to a tracked file, including `alembic.ini`, which takes its URL from
settings at runtime.

Two hard gates:

- `RAZORPAY_MODE` must be exactly `test`. The setting has a validator that **refuses
  to boot** otherwise. Failing at startup is much safer than discovering it at the
  first API call.
- `DEMO_ENDPOINTS_ENABLED` must be true or `/api/v1/demo/*` returns 404. Those
  endpoints create and destroy data.

`demo/reset` deletes only rows flagged `is_synthetic`, so a reset can never destroy
data that arrived from a real Razorpay test-mode webhook.

## 9. Observability

Structured JSON logs via `python-json-logger`. A correlation ID is attached per
request through a `ContextVar`, honouring an inbound `X-Correlation-ID` header so a
webhook delivery can be traced from the provider's id all the way into the audit
trail, and echoed back on the response. Log payloads pass through the same redaction
used by the audit trail.

## 10. Synthetic data

We have no real merchant data and must never imply we do. `ml/src/generate_data.py`
produces a controlled population whose ground-truth recovery propensity is known, so
the ML baseline has a target to learn and the simulator has an outcome to reveal.

Design choices worth noting:

- Everything is driven by one seed; the same seed gives byte-identical output. The
  clock is never read — the caller supplies `reference_time`.
- Recovery probability is a transparent, monotone function of a few drivers
  (history, attempt number, ticket size, hour of day, segment). Intentionally
  *learnable but not trivial*: a logistic baseline should capture most of it,
  leaving headroom for a tree model to justify itself.
- `recovered` and `recovery_horizon_hours` are ground truth and must never be fed to
  the model as features — that would be textbook leakage. The horizon is null for
  non-recovered cases precisely so it cannot leak the label.
- Ticket sizes are log-normal, so value-weighted and count-weighted recovery rates
  genuinely differ. The dashboard is required to show both, and they should not be
  the same number.
- ~5% of customers carry `do_not_contact`. The guardrail needs a population to act
  on, or the demo cannot demonstrate it working.

## 11. Testing strategy

The suite runs against SQLite in-memory by default for speed, and against real
PostgreSQL when `TEST_DATABASE_URL` is set — which CI does, because JSONB behaviour
and CHECK constraints are dialect-specific and SQLite would let some things through.

Models use SQLAlchemy's dialect-agnostic `Uuid` type, so Python-side values are real
`uuid.UUID` objects on both backends and the SQLite run exercises the same types as
production.

Test sessions bind with `join_transaction_mode="create_savepoint"`, so a
`session.commit()` inside the API dependency releases a savepoint instead of
committing the outer test transaction. Without it, tests would leak data into each
other.

## 12. Decisions taken, with reasons

| Decision | Why |
|---|---|
| Modular monolith | Single transaction boundary; state change + audit land atomically without distributed coordination |
| Integer paise everywhere | Rounding error in money is a reporting error in the headline metric |
| `ml` as an installable package | The generator is shared between API seeding and ML training; one implementation, no `sys.path` manipulation |
| Four tables in Slice 1, not nine | No table should exist without code that writes to it |
| Single-tenant merchant resolution | Auth is out of scope pending owner approval; every query is already merchant-scoped, so adding auth later is a routing change, not a schema change |
| `ESCALATED` non-terminal | A human must be able to return a case to the agent |
| B008 lint rule disabled | `Depends(...)` in a signature default *is* the FastAPI idiom; the rule fires on correct code |

## 13. Known gaps

Named honestly rather than left to be discovered:

- The model measures **gross** recovery among actioned cases, not incremental
  uplift. There is no randomised holdout yet, so no causal claim can be made; the
  simulator in M4 adds one. This is stated at length in the model card.
- The decision threshold (0.07) actions nearly every case, because the assumed
  intervention cost is small relative to the ticket. The policy engine's
  independent confidence floor is what actually restrains action.
- Per-strategy probabilities are the learned case-level probability times a
  hand-specified strategy-fit multiplier. The multiplier is assumed, not learned,
  because the synthetic data has no counterfactuals.
- Nothing executes an intervention yet. The loop stops at `ACTION_SELECTED`;
  `ACTION_PENDING` onward arrives with the orchestrator in M4.
- The "max reminders per 24h" rule is enforced structurally by the cooldown (at
  most one action per window) rather than by counting reminders, because per-channel
  counting needs the `Intervention` table that M4 introduces.
- No authentication. Single seeded merchant only.
- No worker, no queue usage. Redis is running but unused until M4.
- No Razorpay calls of any kind yet. The mode gate exists; the adapter does not.
- `Intervention`, `RecoveryOutcome`, `WebhookEvent`, `ModelPrediction` and
  `HumanReview` tables are specified but not yet created.
