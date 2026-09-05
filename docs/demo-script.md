# Demo script — 4 minutes

Every figure below came from an actual run on seed `20260902`. Nothing is
hardcoded, and the same seed reproduces the same numbers. If a number on screen
disagrees with a number here, trust the screen and say so.

**The claim to lead with is incremental recovery, not gross.** Gross recovery
includes customers who would have paid anyway; the holdout is what separates
them. That distinction is the most defensible thing in this project, so it goes
first rather than being buried.

---

## Before you start (2 minutes, off camera)

```bash
docker compose up -d
```

```bash
cd apps/api && .venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
```

```bash
cd apps/web && npm run dev
```

Then reset to a known state:

```bash
curl -X POST http://localhost:8000/api/v1/demo/seed -H "Content-Type: application/json" -d "{\"count\":120,\"reset\":true}"
```

Leave the browser on <http://localhost:3000> showing an empty-ish Overview.
Have a second tab ready on `/reviews`.

**Checks:** `/health` says `ok`; `/api/v1/integrations/health` says
`razorpay_mode: test`. If either is wrong, fix it before presenting rather than
narrating around it.

---

## 0:00 — The problem (20 seconds)

> Merchants lose revenue to failed payments and failed subscription renewals.
> Most systems detect the failure. Very few close the loop from detection to
> money actually back in the account.
>
> This is a bounded agent that closes that loop: detect, diagnose, score,
> choose an action within policy, execute it, observe what happened, and measure
> what it recovered — with an audit trail for every decision.

Point at the sidebar. Five screens, no chat interface anywhere.

---

## 0:20 — Revenue at risk (30 seconds)

**Screen: Overview.** Click **Run agent on pending cases**.

> 120 synthetic cases, **₹8,98,632** at risk.

Point at the yellow **SYNTHETIC** banner.

> That banner is on every screen showing generated data. We have no real
> merchant data and we do not pretend otherwise — these numbers describe a
> controlled simulation.

Let the funnel and failure-reason charts land.

> The funnel is the loop: detected, diagnosed, scored, actioned, recovered.
> The bar chart is where the money is actually going — insufficient funds and
> issuer declines dominate, and they need different treatment from a network
> timeout.

---

## 0:50 — What the agent did (40 seconds)

The KPI row now shows the run.

> **₹2,65,231 recovered — 29.5% of the value at risk.** That figure is computed
> from stored rows in integer paise. There is no float anywhere in the money
> path; the type refuses to be constructed from one.

Then, before anyone asks:

> **35 recovered, 38 escalated to a human, 27 stopped.** The stopped and
> escalated cases matter as much as the recovered ones — that is the agent
> declining to act, which is the part most demos leave out.

---

## 1:30 — The number that survives scrutiny (60 seconds)

**This is the centre of the demo. Do not rush it.**

**Screen: Overview**, or read from `/api/v1/metrics/experiments`.

> The obvious question about any recovery number is: *would that money have come
> back anyway?* Usually you cannot answer it.
>
> So we randomise. **20% of cases go into a holdout arm and are never contacted
> at all** — the agent decides what it would have done, and we deliberately
> withhold it.

Then the comparison:

> Among cases the agent wanted to act on: the **treated arm recovered 49.5% by
> value, the holdout 36.7%**. The difference — **+12.8 percentage points** — is
> what the agent actually caused. Roughly **₹50,500** of genuine incremental
> recovery.
>
> The gap between that and the ₹2.65 lakh gross figure is customers who would
> have paid regardless. We report both, and we lead with the smaller one.

If asked how solid it is, answer plainly:

> On 120 cases the holdout is only 27, so that specific number moves between
> runs — on a 400-case batch it lands closer to +40 points. The API ships the
> caveat text alongside the number so a UI cannot render one without the other.
> It is measured inside a simulation, and it has no confidence interval.

---

## 2:30 — Restraint is the product (30 seconds)

> The agent took **57 actions. A naive "retry transient failures, remind
> everyone else" baseline takes 115** — it contacts twice as many customers.
>
> On raw expected value that baseline scores higher, because at ₹5 a message
> against an ₹8,600 ticket, spamming everyone is arithmetically optimal. We have
> not tuned the cost assumptions to make our agent win that comparison.
>
> What the agent buys instead is **half the customer contacts**, every low
> confidence decision in front of a human, and guardrails that hold.

This is a strength if you say it first. It is a weakness if a judge finds it.

---

## 3:00 — The guardrails, on screen (40 seconds)

**Screen: Recovery cases.** Filter **state = STOPPED**.

Find a row tagged `do not contact`:

> This customer opted out. The case was diagnosed and scored — we know exactly
> what it was worth — and then stopped. Opt-out is checked **before** economics,
> so a profitable case belonging to an opted-out customer is still stopped.
> That ordering has its own test.

**Screen: Review queue.**

> **38 cases worth ₹2,90,061 waiting for a person.** Ordered by money at stake,
> because reviewer attention is the scarcest thing here.

Open one card.

> Each one says what is at risk, why the agent stopped, and what it proposed —
> in prose, with the exact policy rules that fired.

Point at a max-attempts case where **Approve is disabled**:

> This one exhausted its attempt budget, so there is no proposal to approve. A
> reviewer can override or stop it, not rubber-stamp something that does not
> exist.

Reject it with a note. Then:

> Every human decision is audited against the reviewer. And the guardrails bind
> humans too — a reviewer cannot approve contacting an opted-out customer, or
> pick a strategy the merchant disabled.

---

## 3:40 — Auditability (20 seconds)

**Screen: any case detail.**

> Full trail: ingested, diagnosed, scored by a named model version, policy
> evaluated with the rules that fired, action executed, outcome observed.
> Monotonic sequence, append-only, and every event says who did it — system,
> model, human or webhook.

---

## 4:00 — Close

> The AI here is a component, not the product. A model estimates probability, an
> LLM writes explanations — and neither can act. The policy engine picks from a
> closed set of seven actions, and the explanation schema has no field in which
> to name one.
>
> Trained on synthetic data. Razorpay test mode only. Every number on screen was
> computed, not written.

---

## If you have 60 more seconds

**Model metrics.** PR-AUC 0.675, Brier 0.203, calibration table.

> The interpretable logistic baseline beat gradient boosting by 0.02 PR-AUC, so
> we kept the baseline. The decision threshold is 0.07, chosen to maximise
> expected net recovery rather than left at 0.5 — and we are open that at these
> costs it actions nearly everything, which is why the policy engine applies its
> own independent confidence floor on top.

**Webhooks**, if there is a Razorpay-minded judge:

```bash
curl -X POST http://localhost:8000/api/v1/webhooks/razorpay -H "X-Razorpay-Signature: forged" -d "{}"
```

> Rejected. Signature is HMAC-SHA256 over the raw body — we read the bytes
> before parsing, because re-serialising the JSON changes them and breaks
> verification. Razorpay delivers at-least-once, so we dedupe on their event id,
> and recovery recording independently refuses an already-recovered case.

---

## Questions you should expect

**"Is the recovered money real?"**
No. Synthetic data, deterministic simulator. What is real is the pipeline, the
guardrails, and the measurement design.

**"How do you know the agent caused it?"**
Randomised holdout. That is the +12.8 point figure. Gross recovery does not
support a causal claim and we do not make one.

**"What if the model or the LLM is down?"**
Both degrade to deterministic fallbacks and the loop still completes. There is a
test that runs the whole demo with the model artifact deleted.

**"Could it contact someone it shouldn't?"**
The action space is a closed enum, the policy engine filters it, and the executor
re-checks eligibility immediately before acting — so a case recovered or opted
out between planning and execution is skipped and audited.

**"Why is the agent worse than the baseline on expected value?"**
Because the baseline contacts everyone and interventions are cheap in our cost
model. We chose not to tune that assumption to flatter the result. The agent's
case is fewer contacts, human oversight and enforced guardrails.

---

## Failure recovery, live

| Problem | Do this |
|---|---|
| Numbers look wrong | Re-seed with `reset: true`; the seed is deterministic |
| API is down | Frontend shows a real error state — say so and restart uvicorn |
| Empty dashboard | Overview has a **Seed 120 synthetic cases** button |
| Postgres unreachable | `docker compose up -d`, wait for healthy |
| Someone asks for live Razorpay | It cannot run in live mode; the app refuses to boot unless `RAZORPAY_MODE=test` |
