# Razorpay Buildathon — AI Revenue Recovery

## 0. Document purpose

This file is the single source of truth for implementation decisions. Claude Code should read it before planning or modifying the repository.

The goal is to build a production-shaped hackathon system, not a chatbot demo: an AI-assisted revenue recovery agent that detects revenue at risk, diagnoses likely causes, chooses a bounded recovery intervention, executes or simulates that intervention through Razorpay test-mode-compatible workflows, measures money recovered across a batch, enforces compliance/stopping rules, and produces a full audit trail.

**Human control rule:** the repository owner remains the final decision-maker. Claude may create/edit local files and suggest commands, but must never push to GitHub, merge branches, change repository settings, rotate credentials, or perform destructive operations without explicit user approval. Only the user performs `git push`.

---

## 1. Product definition

### 1.1 Problem statement
Merchants lose revenue through failed payments, abandoned checkouts, failed subscription renewals, overdue receivables, and repeated payment degradation. Most systems detect individual failures but do not close the loop from detection to recovery.

### 1.2 Product thesis
Build a **Revenue Recovery Agent** that runs a closed, bounded loop:

`Detect → Diagnose → Prioritize → Select intervention → Execute → Observe outcome → Stop / Retry / Escalate → Measure → Audit`

The agent must optimize for **expected recovered money**, not merely model accuracy.

### 1.3 Primary hackathon use case
Focus the MVP on **failed-payment + failed-subscription recovery** because it is straightforward to demonstrate using Razorpay test mode, APIs, Payment Links, and webhooks.

Secondary/synthetic flows may include:
- checkout abandonment;
- overdue B2B invoice recovery;
- promise-to-pay tracking.

Do not dilute the MVP by trying to fully implement every direction.

### 1.4 What judges should see
A batch of at least 50 synthetic/test-mode revenue-risk cases enters the system. The system:
1. identifies which cases are recoverable;
2. explains likely failure/root cause;
3. estimates probability and value of recovery;
4. chooses a policy-compliant intervention;
5. executes or simulates the intervention;
6. ingests the resulting event/webhook;
7. records recovered amount;
8. stops, retries, or escalates according to policy;
9. displays aggregate metrics and individual audit trails.

The final demo headline should resemble:
- Revenue at risk: ₹X
- Eligible for automated recovery: ₹Y
- Revenue recovered: ₹Z
- Recovery rate: N%
- Net expected benefit after intervention cost: ₹Q
- Automated actions / escalations / stopped cases

---

## 2. Scope and non-goals

### 2.1 MVP scope
Implement these end-to-end flows:

**Flow A — Failed payment recovery**
- receive/import payment failure event;
- classify/root-cause failure;
- estimate recovery probability;
- score expected recoverable value;
- select action from bounded action set;
- generate Payment Link or mock retry/reminder action;
- ingest success/failure outcome;
- update campaign state and recovered amount.

**Flow B — Failed subscription recovery**
- ingest subscription pending/failure state;
- identify retry/reminder eligibility;
- choose timing/action;
- create recovery instruction/link where appropriate;
- ingest `subscription.charged` or relevant test-mode webhook;
- close recovery case.

**Flow C — Human escalation**
- cases breaching confidence/risk/rule thresholds become human-review tasks;
- reviewer can approve, modify, skip, or stop an intervention;
- every override is audited.

### 2.2 Nice-to-have after MVP
- abandoned checkout model;
- B2B invoice receivables chaser;
- promise-to-pay tracker;
- Hinglish message/voice generation;
- channel optimization;
- contextual bandit for intervention selection;
- merchant-configurable policy builder.

### 2.3 Non-goals
- collecting or storing real card data;
- operating with Razorpay live keys;
- uncontrolled autonomous communication to real customers;
- offensive security/fraud behavior;
- unbounded LLM tool calling;
- claiming causal uplift without appropriate experimental design;
- building a generic chat assistant as the core product.

---

## 3. Core user personas

### Merchant operator
Wants to know what revenue is at risk, what the system is doing, why, and how much was recovered.

### Finance/revenue manager
Wants aggregate recovery metrics, campaign performance, intervention cost, failure reasons, and exception lists.

### Human reviewer
Wants a queue of ambiguous/high-value/high-risk cases and the ability to approve/override actions.

### Developer/admin
Needs test-mode integration health, webhook status, configuration, feature flags, and audit logs.

---

## 4. Functional requirements

### FR-1 Case ingestion
Support:
- Razorpay webhook ingestion;
- test-mode API synchronization where needed;
- CSV/JSON synthetic batch upload for 50+ cases;
- seeded demo dataset.

Normalize each incoming event into a `RecoveryCase`.

### FR-2 Failure diagnosis
Produce a structured diagnosis:
- `failure_category`;
- `failure_reason_code`;
- `recoverability`;
- `confidence`;
- human-readable explanation;
- evidence/features used.

LLMs may summarize/explain; deterministic mappings should be preferred when reason codes are known.

### FR-3 Recovery scoring
For each eligible case calculate:
- `p_recovery` = estimated probability that a given recovery strategy succeeds;
- `amount_at_risk`;
- expected gross recovery = `p_recovery × amount_at_risk`;
- estimated intervention cost;
- expected net recovery;
- priority score.

### FR-4 Intervention selection
Initial action space:
- `WAIT_AND_RETRY`;
- `CREATE_PAYMENT_LINK`;
- `SEND_REMINDER_SIMULATED`;
- `REQUEST_ALTERNATE_METHOD`;
- `ESCALATE_HUMAN`;
- `DO_NOT_CONTACT`;
- `STOP_RECOVERY`.

Selection is constrained by policy engine. The LLM cannot invent new money-moving actions.

### FR-5 Execution
Create an explicit `Intervention` object before execution. Execution layer uses allow-listed adapters only.

Razorpay adapter responsibilities:
- create/fetch/cancel Payment Links where applicable;
- fetch payment/subscription state;
- process test-mode outcomes;
- verify webhook signatures.

Communication adapter for hackathon MVP is **simulation-first** unless a compliant messaging provider is deliberately integrated later. The UI should show what message/channel would have been used.

### FR-6 State machine
Every recovery case must follow a deterministic state machine:

`NEW → DIAGNOSED → SCORED → ACTION_SELECTED → ACTION_PENDING → ACTION_EXECUTED → OBSERVING → RECOVERED | RETRY_ELIGIBLE | ESCALATED | EXHAUSTED | STOPPED`

Illegal transitions must be rejected.

### FR-7 Stopping rules
Default configurable rules:
- max automated attempts per case: 3;
- max reminders per 24h: 1;
- no action after successful recovery;
- stop when expected net recovery <= 0;
- stop after explicit opt-out/do-not-contact flag;
- escalate high-value cases above merchant threshold;
- escalate below-confidence threshold;
- exponential/capped backoff for retries;
- idempotency for every action.

### FR-8 Audit trail
Record append-only events for:
- ingestion;
- model version/features;
- diagnosis;
- score;
- policy checks;
- selected intervention;
- human approval/override;
- API request metadata (never secrets);
- result/webhook;
- state transition;
- recovered amount.

### FR-9 Dashboard
Must include:
- KPI cards;
- revenue recovery funnel;
- revenue-at-risk over time;
- failure reason distribution;
- intervention performance;
- recovered amount by intervention;
- case table with filters;
- human review queue;
- case detail audit timeline;
- experiment/model metrics page;
- system/integration health.

### FR-10 Explainability
For each automated decision show:
- why the case was considered recoverable;
- score components;
- why this intervention was selected;
- which policy constraints applied;
- why the system stopped/escalated if it did.

---

## 5. Non-functional requirements

### Reliability
- webhook endpoint is idempotent;
- duplicate events do not duplicate actions;
- background jobs retry safely;
- state transitions happen transactionally;
- external API failures use bounded retries;
- all money amounts stored as integer paise, never floating point.

### Security
- no secrets committed to git;
- `.env.example` contains names only;
- server-side Razorpay credentials only;
- webhook HMAC/signature validation;
- redact PII from logs;
- API validation with typed schemas;
- rate-limit sensitive endpoints;
- use least privilege;
- test mode by default and explicit `RAZORPAY_MODE=test` safety gate.

### Observability
- structured JSON logs;
- correlation IDs for cases/webhooks/jobs;
- health/readiness endpoint;
- error capture;
- job metrics;
- audit logs separate from debug logs.

### Performance
Hackathon target, not global scale:
- dashboard API p95 < 500 ms for common reads on seeded data;
- ingest 1,000 synthetic cases without blocking request thread;
- use queues/background workers for orchestration.

### Testing
- unit tests for policy/state machine/scoring;
- API integration tests;
- webhook signature/idempotency tests;
- seeded deterministic model test;
- end-to-end happy path and failed-action path;
- at least one demo test proving recovered amount across a batch.

---

## 6. System architecture

### 6.1 Architecture choice
Use a **modular monolith + background worker** for the hackathon. Do not prematurely split into microservices.

Reasons:
- faster development;
- simpler deployment;
- still supports clean boundaries;
- enough robustness for 50–10,000 demo records;
- easier tracing and transaction handling.

### 6.2 Logical architecture

```text
                    ┌─────────────────────────────┐
                    │ Next.js Merchant Dashboard  │
                    └──────────────┬──────────────┘
                                   │ HTTPS / REST
                    ┌──────────────▼──────────────┐
                    │ FastAPI Application          │
                    │                              │
Razorpay Webhooks ─►│ Webhook Ingestion            │
CSV/Test Data ─────►│ Case Service                 │
                    │ Diagnosis Service            │
                    │ Recovery Scoring             │
                    │ Policy Engine                │
                    │ Intervention Orchestrator    │
                    │ Audit Service                │
                    └──────┬─────────────┬─────────┘
                           │             │
                    ┌──────▼─────┐ ┌────▼──────────┐
                    │ PostgreSQL │ │ Redis / Queue │
                    └────────────┘ └────┬──────────┘
                                       │
                                ┌──────▼──────────┐
                                │ Background      │
                                │ Worker          │
                                └──────┬──────────┘
                                       │
                         ┌─────────────▼──────────────┐
                         │ Allow-listed Tool Adapters │
                         │ Razorpay / Simulator / LLM │
                         └────────────────────────────┘
```

### 6.3 Decision boundaries
The AI/LLM is **not** the source of truth for state, money, or permissions.

Deterministic code owns:
- case states;
- policy rules;
- maximum retries;
- amount calculations;
- eligibility gates;
- idempotency;
- API execution;
- stopping rules.

ML owns:
- probability estimates;
- rankings;
- optional failure/risk classification when deterministic signals are insufficient.

LLM owns:
- explanation/summarization;
- structured diagnosis assistance for unstructured notes;
- personalized but policy-bounded message drafting;
- optional agent planning within an allow-listed action schema.

---

## 7. Technology stack

### Frontend
- **Next.js 15+** with App Router
- **TypeScript** strict mode
- **Tailwind CSS**
- **shadcn/ui** as local component baseline
- **21st.dev MCP** for discovering/generating high-quality UI components and design directions
- **TanStack Query** for server state
- **TanStack Table** for case/review tables
- **Recharts** for dashboard charts
- **React Hook Form + Zod** for forms and validation

### Backend
- **Python 3.12+**
- **FastAPI**
- **Pydantic v2**
- **SQLAlchemy 2.x**
- **Alembic** migrations
- **PostgreSQL 16+**
- **Redis**
- **Celery** for background orchestration (RQ/Arq acceptable only if repo already strongly favors them)
- **httpx** for external HTTP clients
- official Razorpay Python SDK where useful, with a thin internal adapter around it

### ML/Data
- **pandas / NumPy**
- **scikit-learn**
- **XGBoost or LightGBM** optional after baseline
- **MLflow** optional/local for experiment tracking if setup cost stays reasonable
- **SHAP** for explainability if tree model is used
- **joblib** for demo artifact serialization

### LLM / agent layer
Provider abstraction so model can be changed without touching business logic.

Suggested interface:
- `LLMProvider.generate_structured()`
- JSON-schema/Pydantic validated outputs
- low temperature for decisions/explanations
- no direct credentials or database access from prompts
- no direct free-form tool execution

Claude can be used during development; the deployed demo may use any configured provider. The system must still function in a deterministic degraded mode if no LLM key is configured.

### DevOps
- Docker + Docker Compose for local services
- GitHub repository controlled by user
- GitHub Actions: lint + typecheck + tests only
- deploy frontend/backend using a simple hackathon-friendly platform chosen after MVP is stable
- PostgreSQL may be managed in deployment

### Quality tooling
Frontend:
- ESLint
- Prettier
- TypeScript

Backend:
- Ruff
- mypy
- pytest
- pytest-asyncio as needed

Repository:
- pre-commit hooks optional but recommended

---

## 8. 21st.dev MCP design rules

Claude should use 21st.dev MCP as a **design/component accelerator**, not as a replacement for product thinking.

Use it to:
- search for dashboard shells, data tables, timelines, command menus, stat cards, filters, empty states, review queues, side panels;
- inspect actual generated/component code before adopting it;
- generate 2–3 variants only for high-impact screens when uncertainty exists;
- keep installed component code local and editable.

Design principles:
- financial operations UI, not flashy consumer fintech;
- dense but readable data presentation;
- strong visual hierarchy for `At Risk`, `Recovered`, `Escalated`, `Stopped`;
- amount and state should never rely on color alone;
- every automated action needs visible rationale;
- audit timeline is first-class;
- responsive desktop-first dashboard;
- keyboard-accessible controls;
- WCAG AA-minded contrast/focus states;
- consistent spacing and typography tokens;
- no unnecessary gradients/glassmorphism/animations;
- loading, empty, error, stale-data, and partial-data states are explicitly designed.

Key screens:
1. Overview dashboard
2. Recovery Cases
3. Case Detail + Audit Timeline
4. Human Review Queue
5. Experiments & Model Metrics
6. Policies / Stopping Rules
7. Integrations / Webhook Health
8. Batch Simulator / Demo Control Panel

Claude should create a small design system in code:
- typography tokens;
- spacing tokens;
- semantic status variants;
- consistent cards/tables/drawers;
- `MoneyAmount`, `StatusBadge`, `ConfidenceBadge`, `DecisionReason`, `AuditTimeline`, `MetricCard`, `CaseTable` reusable components.

---

## 9. Data model

Suggested entities:

### Merchant
- id
- name
- currency
- timezone
- high_value_threshold
- policy_config
- created_at

### Customer
Use synthetic/demo-safe identifiers.
- id
- external_ref
- segment
- contactability flags
- preferred_language (optional)

### RecoveryCase
- id UUID
- merchant_id
- customer_id nullable
- source_type (`PAYMENT`, `SUBSCRIPTION`, `CHECKOUT`, `INVOICE`)
- source_external_id
- amount_at_risk_paise
- currency
- failure_category
- failure_reason_code
- detected_at
- current_state
- recoverability_score
- priority_score
- model_version
- last_action_at
- attempt_count
- recovered_amount_paise
- recovered_at nullable
- do_not_contact boolean
- created_at / updated_at

### Intervention
- id
- recovery_case_id
- strategy
- channel
- status
- planned_at
- executed_at
- idempotency_key
- policy_snapshot JSONB
- decision_snapshot JSONB
- estimated_cost_paise
- result JSONB

### RecoveryOutcome
- id
- recovery_case_id
- intervention_id nullable
- outcome_type
- amount_recovered_paise
- external_event_id
- observed_at

### AuditEvent
- id
- recovery_case_id
- actor_type (`SYSTEM`, `MODEL`, `HUMAN`, `WEBHOOK`)
- actor_id/version
- event_type
- before_state
- after_state
- payload JSONB redacted
- created_at

### WebhookEvent
- provider
- event_id/dedup key
- event_type
- signature_valid
- raw payload (redacted or encrypted if needed)
- processing_status
- received_at

### ModelPrediction
- case_id
- model_name
- model_version
- intervention candidate
- probability
- calibrated_probability
- feature snapshot
- explanation
- created_at

### HumanReview
- case_id
- reason
- status
- reviewer
- decision
- notes
- resolved_at

---

## 10. ML strategy

### 10.1 What is the ML problem?
Avoid a vague “AI predicts recovery” statement. Formulate specific supervised tasks.

**Task A: Recoverability classification**
Predict whether a failed revenue event will be recovered within a defined horizon (for demo: e.g. 72h/simulated horizon).

Target:
`y = 1` if case recovers; else `0`.

**Task B: Intervention-specific response model**
For each candidate intervention estimate:
`P(recovery | case features, intervention)`.

For MVP, train a baseline binary classifier and apply deterministic intervention rules. If enough synthetic data is available, add intervention-specific estimates.

**Task C: Prioritization**
Rank cases by expected net recovery:

`EV(action, case) = P(recovery | action, case) × amount_at_risk − intervention_cost − risk_penalty`

Choose the highest-EV action that passes policy gates.

### 10.2 Candidate features
- amount;
- payment method category;
- failure reason/code;
- hour/day;
- attempt count;
- prior successful payments;
- customer tenure/segment;
- subscription age;
- invoice age/days overdue;
- previous recovery responses;
- retry interval;
- checkout completion stage (synthetic flow);
- merchant/customer history aggregates.

Do not use highly sensitive attributes.

### 10.3 Baseline models
Start simple and measurable:
1. Logistic Regression baseline
2. Random Forest / HistGradientBoosting
3. XGBoost/LightGBM only if it materially improves held-out metrics

Do not use a neural network simply to sound advanced.

### 10.4 Train/validation/test split
Prefer temporal split where timestamps matter:
- train: oldest 70%
- validation: next 15%
- test: newest 15%

Otherwise use stratified split with fixed random seed.

Never tune on final test data.

### 10.5 Metrics
Classification metrics:
- Precision
- Recall
- F1
- ROC-AUC
- PR-AUC (important if recovery positives are imbalanced)
- Log Loss / Brier Score
- calibration curve

Business metrics (more important for this track):
- gross recovered amount;
- recovery rate by count;
- recovery rate by value;
- incremental simulated uplift vs baseline policy;
- cost per ₹ recovered;
- net recovered amount;
- automated resolution rate;
- escalation rate;
- attempts per recovery;
- time-to-recovery.

### 10.6 Calibration
A probability such as 0.8 should mean approximately 80% recovery frequency among comparable predictions. Use Platt scaling or isotonic calibration if needed.

### 10.7 Class imbalance
Use:
- class weights;
- PR-AUC;
- threshold tuning based on business value;
- avoid optimizing accuracy alone.

### 10.8 Thresholding
Do not hardcode `0.5` as universal threshold. Choose thresholds from validation data to optimize expected net recovery while respecting escalation capacity and false-action cost.

### 10.9 Explainability
- coefficients for logistic baseline;
- feature importance/SHAP for tree model;
- user-facing explanations should translate signals into plain language;
- never imply causality from feature importance.

### 10.10 Drift and monitoring
For demo, calculate:
- prediction distribution;
- feature missingness;
- recovery rate by recent batch;
- model version.

Document production concepts:
- data drift;
- concept drift;
- calibration drift;
- retraining triggers.

### 10.11 Synthetic data
Because real merchant/customer data is unavailable, generate realistic synthetic cases with controlled outcome logic.

The generator must:
- use fixed seed for reproducibility;
- produce at least 1,000 training examples and 100+ demo/test cases;
- model different failure types and recovery propensities;
- create counterfactual-ish action outcomes for simulator;
- clearly label generated data as synthetic.

Do not present synthetic-model accuracy as evidence of real-world production performance. In the pitch, emphasize architecture and measured demo performance on the controlled simulation/test environment.

### 10.12 Causal caveat
Observed recovery after a reminder does not automatically prove the reminder caused recovery. A proper production system should use randomized holdouts/A-B testing or causal methods. For hackathon demo:
- include a small baseline/holdout simulator;
- call results “simulated incremental recovery” rather than causal production uplift.

### 10.13 Future learning policy
After MVP, intervention choice can evolve into:
- multi-armed bandit;
- contextual bandit;
- uplift modeling;
- reinforcement learning only after safety and sufficient real outcome data.

Do **not** implement unconstrained RL for the hackathon.

---

## 11. Agent/orchestration design

### Principle
Use an **agent-shaped workflow with deterministic guardrails**, not an autonomous general-purpose agent.

Pseudo-flow:

```python
case = ingest(event)
diagnosis = diagnose(case)
predictions = scorer.predict(case, allowed_actions)
decision = policy_engine.choose(case, predictions)

if decision.requires_human:
    create_review(case, decision)
elif decision.action == STOP_RECOVERY:
    stop(case)
else:
    intervention = create_intervention(case, decision)
    enqueue(intervention)
```

### LLM structured contract
Example `DecisionExplanation` schema:
- summary: string
- evidence: list[string]
- uncertainty: string
- recommended_copy: optional string

The LLM does not select an action outside the allowed candidate list and cannot bypass policy engine output.

### Prompt injection defense
If external notes/messages are passed to an LLM:
- treat them as untrusted data;
- delimit content;
- never allow customer-provided text to redefine tool permissions/system policy;
- structured output validation is mandatory.

---

## 12. Policy engine

Rules are deterministic and merchant-configurable.

Example policy order:
1. if recovered → STOP
2. if do_not_contact → STOP
3. if attempts >= max_attempts → ESCALATE/EXHAUST
4. if high value and confidence below threshold → HUMAN
5. if expected_net_value <= 0 → STOP
6. if cooldown active → WAIT
7. if known transient failure → WAIT_AND_RETRY
8. if actionable failure and contact allowed → CREATE_PAYMENT_LINK / REMINDER
9. otherwise → HUMAN

Every policy evaluation produces a `PolicyDecision` containing:
- allowed actions;
- blocked actions + reason;
- applied rule IDs;
- final recommendation.

---

## 13. Razorpay integration plan

Use **test mode only**.

### Payment Links
Use Payment Links as a visible recovery action where relevant. The application can create a payment link and associate its external ID with an intervention.

### Webhooks
Primary mechanism for asynchronous outcomes. Validate `X-Razorpay-Signature` against the **raw request body**. Store dedup information and return quickly; enqueue processing.

Relevant event families to explore during implementation:
- payment failures/successes;
- order paid;
- payment link paid/expired/cancelled;
- subscription pending/charged/activated as supported by the chosen test flow.

Exact event names and payload fields must be checked against current Razorpay docs during implementation rather than guessed.

### Test-mode constraints
- all integration demos use test keys;
- never accept live secrets in local demo configuration;
- public HTTPS endpoint is required for externally delivered webhooks;
- if localhost tunneling is used, follow current Razorpay-supported guidance instead of assuming common tunnel domains work.

### Adapter interface

```python
class PaymentProvider(Protocol):
    async def create_recovery_link(...): ...
    async def fetch_payment(...): ...
    async def fetch_subscription(...): ...
    async def verify_webhook(...): ...
```

Keep provider-specific code out of domain services.

---

## 14. API design

Suggested REST endpoints:

### Cases
- `GET /api/v1/cases`
- `GET /api/v1/cases/{id}`
- `POST /api/v1/cases/batch`
- `POST /api/v1/cases/{id}/evaluate`
- `POST /api/v1/cases/{id}/stop`

### Reviews
- `GET /api/v1/reviews`
- `POST /api/v1/reviews/{id}/approve`
- `POST /api/v1/reviews/{id}/override`
- `POST /api/v1/reviews/{id}/reject`

### Metrics
- `GET /api/v1/metrics/overview`
- `GET /api/v1/metrics/interventions`
- `GET /api/v1/metrics/models`

### Policies
- `GET /api/v1/policies`
- `PUT /api/v1/policies`

### Webhooks
- `POST /api/v1/webhooks/razorpay`

### Demo
- `POST /api/v1/demo/reset`
- `POST /api/v1/demo/seed`
- `POST /api/v1/demo/run-batch`
- `POST /api/v1/demo/simulate-outcomes`

Demo endpoints must be disabled or protected outside demo environment.

---

## 15. Repository structure

Prefer monorepo:

```text
/
├── apps/
│   ├── web/                    # Next.js frontend
│   └── api/                    # FastAPI backend
├── packages/
│   └── contracts/              # optional generated/shared API schemas
├── ml/
│   ├── data/
│   ├── notebooks/              # exploration only, not production logic
│   ├── src/
│   │   ├── generate_data.py
│   │   ├── features.py
│   │   ├── train.py
│   │   ├── evaluate.py
│   │   └── inference.py
│   └── artifacts/              # gitignored model binaries where appropriate
├── infra/
│   ├── docker/
│   └── scripts/
├── docs/
│   ├── architecture.md
│   ├── demo-script.md
│   ├── api.md
│   └── model-card.md
├── .github/workflows/
├── docker-compose.yml
├── .env.example
├── Makefile
├── README.md
└── project-spec.md
```

Backend module boundaries:

```text
apps/api/app/
├── api/
├── core/
├── domain/
│   ├── cases/
│   ├── interventions/
│   ├── policies/
│   └── audit/
├── integrations/
│   ├── razorpay/
│   └── llm/
├── ml/
├── workers/
├── db/
└── tests/
```

---

## 16. UI screen specifications

### Overview
Top KPIs:
- Revenue at Risk
- Recovered Revenue
- Value Recovery Rate
- Net Recovery
- Automated Resolution Rate
- Human Escalations

Charts:
- recovery funnel;
- cumulative recovered value;
- failure reasons;
- action effectiveness.

### Recovery Cases
Columns:
- case ID
- source
- amount at risk
- reason
- recovery probability
- priority
- state
- attempts
- recommended/current action
- last update

Filters:
- state
- amount
- failure reason
- source type
- intervention
- confidence

### Case Detail
Three-column-ish information hierarchy:
- financial/context summary;
- decision card with probability, EV, explanation, policy rules;
- audit timeline.

Actions for human reviewer:
- Approve
- Change strategy
- Stop
- Escalate

### Model Metrics
Show:
- model version;
- test set size;
- PR-AUC/ROC-AUC/F1;
- Brier/calibration;
- threshold;
- business-value curve;
- important features;
- explicit “Synthetic demo data” banner.

### Policy settings
Editable but validated:
- max attempts;
- cooldown;
- auto-action confidence;
- high-value threshold;
- minimum expected net recovery;
- enabled action types.

---

## 17. Demo simulator

A reliable demo cannot depend entirely on external event timing.

Build a deterministic simulator capable of:
- generating batch cases;
- selecting outcome based on seeded probability tables/model outputs;
- triggering synthetic “webhook-like” events through the same internal event processing path;
- showing recovered revenue accumulating live;
- deliberately demonstrating one failure path and one human escalation.

The simulator must be clearly marked as synthetic/demo mode. Razorpay test-mode actions should still be integrated where practical, especially Payment Links/webhook handling.

Suggested demo scenario:
1. seed 120 cases worth ₹8–12 lakh at risk;
2. run diagnosis/scoring;
3. show prioritization;
4. agent executes allowed interventions;
5. simulate/receive outcomes;
6. show ₹ recovered;
7. open one case's full audit trail;
8. show a high-value low-confidence human escalation;
9. show a case stopped because max attempts/cooldown/negative EV applies.

---

## 18. Evaluation and experimentation

Create a baseline policy:
- transient failures: retry;
- everything else: one generic reminder;
- then stop.

Compare agent policy vs baseline on the same seeded batch.

Report:
- recovered value;
- net recovery;
- number of contacts/actions;
- time-to-recovery;
- escalations;
- stopped cases.

For a strong demo, show something like:
`Agent policy recovered 18% more simulated value than baseline while sending 22% fewer unnecessary interventions.`

Only show numbers actually produced by the implemented simulator/evaluation. Never hardcode fake performance claims into the UI.

---

## 19. Error handling scenarios that must be demonstrated/tested

At minimum:
- duplicate webhook;
- invalid webhook signature;
- Razorpay API timeout/failure;
- model unavailable → deterministic fallback;
- LLM unavailable → template explanation/copy fallback;
- attempted duplicate intervention blocked by idempotency;
- stale case already recovered before queued action executes;
- action blocked by cooldown;
- action blocked by do-not-contact;
- max attempts reached;
- human override.

---

## 20. Git/GitHub control policy

The user already owns a GitHub repository.

Claude Code may:
- inspect repo;
- create/edit/delete local project files when required by the approved task;
- run tests/linters/builds;
- run `git status`, `git diff`, `git log`;
- create local commits **only if the user explicitly asks**;
- provide exact commands for the user to run.

Claude Code must NOT:
- run `git push`;
- force push;
- merge/publish PRs;
- change GitHub remote/repository settings;
- add collaborators;
- create/delete remote branches/tags;
- rewrite history with destructive git commands;
- use `git reset --hard`, `git clean -fd`, destructive checkout, or delete user work without explicit approval.

At each milestone, Claude must provide:

```bash
git status
git diff --stat
git diff
# suggested, user-run only:
git add <specific-files>
git commit -m "<message>"
git push origin <branch>
```

Claude should never say that code is “pushed” unless the user confirms it.

---

## 21. Claude Code operating procedure

For every substantial task:
1. Read `project-spec.md` and relevant existing files.
2. Inspect current repo state before editing.
3. State assumptions and propose a small implementation plan.
4. Prefer incremental changes that leave the repo runnable.
5. Do not overwrite user code casually.
6. Implement one vertical slice at a time.
7. Add/update tests with implementation.
8. Run appropriate checks.
9. Summarize exactly what changed, what remains, and known risks.
10. Show relevant `git diff --stat` and user-run git commands.
11. Stop for user decision at architecture-changing or destructive choices.

### Decisions Claude may make autonomously
- names of internal helper functions;
- minor refactors;
- test organization;
- UI spacing/details consistent with design system;
- implementation details that do not change architecture/contracts.

### Decisions requiring user approval
- changing main framework/database/queue;
- adding a paid/external service;
- changing the primary use case;
- schema changes that discard existing data;
- large dependency additions;
- introducing auth provider;
- changing deployment platform;
- deleting significant existing code;
- Git history/remote operations.

---

## 22. Implementation milestones

### Milestone 0 — Repo audit and bootstrap
- inspect current repository;
- document existing files/dependencies;
- set up monorepo only if repo is not already structured;
- `.env.example`;
- Docker Compose Postgres + Redis;
- CI skeleton;
- health endpoints/pages.

### Milestone 1 — Domain core
- DB schema/migrations;
- RecoveryCase state machine;
- policy engine;
- audit event model;
- unit tests.

### Milestone 2 — Synthetic data + ML baseline
- deterministic data generator;
- feature pipeline;
- logistic baseline;
- evaluation report/model card;
- inference adapter;
- business-value scoring.

### Milestone 3 — Case APIs + dashboard foundation
- list/detail endpoints;
- overview metrics;
- dashboard shell with 21st.dev-assisted components;
- recovery case table;
- audit timeline.

### Milestone 4 — Orchestrator + simulator
- intervention selection;
- Celery jobs;
- deterministic simulator;
- batch run and outcome ingestion;
- baseline vs agent evaluation.

### Milestone 5 — Razorpay test-mode integration
- Payment Link adapter;
- webhook endpoint/signature verification;
- idempotency;
- subscription/payment test flow where feasible;
- integration health panel.

### Milestone 6 — Human controls + safety
- review queue;
- approval/override;
- policy page;
- stop/cooldown/max-attempt behavior;
- LLM structured explanation/message generation with fallback.

### Milestone 7 — Polish and demo hardening
- responsive/loading/error states;
- end-to-end tests;
- seeded 100+ case demo;
- README/setup;
- architecture diagram;
- model card;
- 3–5 minute demo script;
- final metrics screenshots/data.

---

## 23. Definition of done

The project is hackathon-ready when:
- a new developer can run it from README;
- 50+ cases can be processed in one batch;
- the system reports actual computed recovered money;
- at least one Razorpay test-mode workflow works end-to-end;
- webhooks are validated and idempotent;
- the policy engine demonstrably blocks unsafe/unbounded actions;
- stopping rules are visible in UI and tests;
- a human can inspect/override a decision;
- every case has an audit trail;
- model and business metrics are visible;
- the demo survives LLM/API degradation via fallbacks;
- no live secrets/data exist in repository;
- lint/typecheck/tests pass;
- user retains sole control of GitHub pushing.

---

## 24. Key terminology glossary

**Revenue at risk:** money associated with a failed/abandoned/overdue revenue event that might still be recovered.

**Recovery rate (count):** recovered cases / eligible cases.

**Recovery rate (value):** recovered amount / eligible amount at risk.

**Expected value (EV):** probability-weighted financial benefit minus estimated costs/penalties.

**Precision:** of cases predicted positive, fraction actually positive.

**Recall:** of actual positive cases, fraction identified.

**PR-AUC:** area under precision-recall curve; often useful for imbalanced classification.

**ROC-AUC:** ranking metric across classification thresholds.

**Calibration:** agreement between predicted probabilities and observed frequencies.

**Brier score:** mean squared error of probabilistic predictions; lower is better.

**Feature engineering:** constructing model inputs from raw data.

**Data leakage:** using information during training that would not be available at prediction time.

**Concept drift:** relationship between features and outcome changes over time.

**Data drift:** input feature distribution changes over time.

**SHAP:** method for explaining contribution of features to model predictions.

**Idempotency:** repeating the same request/event does not cause duplicate effects.

**Webhook:** asynchronous server-to-server event notification.

**State machine:** explicit set of allowed states and transitions.

**Guardrail:** deterministic rule preventing unsafe or invalid agent behavior.

**Human-in-the-loop (HITL):** ambiguous/high-risk decisions are reviewed by a person.

**Contextual bandit:** online learning method that chooses among actions using context while balancing exploration/exploitation.

**Uplift modeling:** predicts incremental effect of an intervention compared with no intervention/alternative.

**Holdout group:** cases deliberately not receiving intervention to estimate baseline behavior.

**Audit trail:** immutable chronological record of what happened, why, and by whom/which model.

---

## 25. Final engineering principles

1. **Money calculations are deterministic.**
2. **LLMs explain and assist; policies authorize.**
3. **Every action must be bounded, idempotent, and auditable.**
4. **Optimize business value, not vanity ML accuracy.**
5. **Show unresolved exceptions honestly.**
6. **Synthetic results are labeled synthetic.**
7. **A robust demo has deterministic fallbacks.**
8. **One strong end-to-end recovery loop beats seven half-built directions.**
9. **The user controls architectural pivots and all remote Git operations.**
10. **No `git push` by Claude Code. Ever.**
