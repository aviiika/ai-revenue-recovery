# Model card — recovery classifier `recovery-clf-v1`

> ## ⚠️ Trained entirely on synthetic data
>
> No real Razorpay merchant or customer data was used, or is available to this
> project. Every number below describes a **controlled simulation** whose
> outcome process we wrote ourselves. **None of it is evidence of real-world
> production performance**, and it should not be presented as such.
>
> What the numbers do support: that the pipeline is correctly constructed, that
> the model learns the signal present in the data, and that acting on it is
> profitable *within the simulation*.

Regenerate everything here with:

```bash
python -m ml.src.train --count 6000
```

---

## 1. What the model predicts

**Task A from spec §10.1 — recoverability classification.** Given a failed
payment or failed subscription renewal, predict whether it will be recovered
within the 72-hour horizon.

```
y = 1  if the case recovers within the horizon
y = 0  otherwise
```

This is a **case-level** prediction: *will this case recover at all*. It is
deliberately **not** a per-strategy model. The synthetic data contains no
counterfactual outcomes — we never observe what would have happened had a
different intervention been chosen — so a per-strategy model would be fitting
noise. Spec §10.1 sanctions this for the MVP: *"train a baseline binary
classifier and apply deterministic intervention rules."*

### What is learned vs. what is assumed

This split is the most important thing on this page, and the system keeps the
two visibly separate rather than blending them into one opaque number:

| Component | Source | Where |
|---|---|---|
| P(recovery \| case) | **Learned** from data | `MLScorer.probability` |
| Strategy fit multiplier | **Assumed** — hand-specified domain priors | `strategy_fit()` |
| Expected value arithmetic | **Deterministic** integer paise | `score_strategy()` |
| Whether to act | **Policy engine**, not the model | `policies/engine.py` |

The final per-strategy probability is `learned_probability × assumed_fit`. The
assumed half has not been validated against data and is an engineering judgement.

## 2. Intended use and out-of-scope use

**Intended:** ranking revenue-at-risk cases by expected recoverable value, and
supplying a probability to the policy engine, which decides whether to act.

**Out of scope:**

- Any use on real customer data without retraining and revalidation.
- Deciding to contact a customer *on its own* — the policy engine holds that
  authority, and independently blocks do-not-contact, attempt-capped, and
  low-confidence cases regardless of what the model says.
- Any causal claim. See §7.
- Credit decisions, or any judgement about a person rather than a transaction.

## 3. Data

Deterministic synthetic population from `ml/src/generate_data.py`, seed `20260902`.

| | |
|---|---|
| Total cases | 6,000 |
| Train / validation / test | 4,200 / 900 / 900 |
| Split | **Temporal**, by `detected_at` (oldest 70% / next 15% / newest 15%) |
| Positive rate (train) | 0.4426 |
| Positive rate (test) | 0.4356 |

The split is temporal, not random, because a random split would let the model
learn from cases occurring *after* the ones it is tested on — information
production never has (spec §10.4).

### Features

Twelve numeric and four categorical inputs, defined once in `ml/src/features.py`
and shared by training and serving:

`amount_at_risk_rupees`, `log_amount`, `attempt_count`, `customer_tenure_days`,
`prior_successful_payments`, `prior_failed_payments`, `subscription_age_days`,
`hour_of_day`, `day_of_week`, `is_overnight`, `has_payment_history`,
`failure_ratio`, plus one-hot `failure_category`, `payment_method`,
`customer_segment`, `source_type`.

**Excluded by construction:** `recovered`, `recovery_horizon_hours`,
`true_recovery_probability`. These are outcomes, unknown when the prediction is
made. `assert_no_leakage()` fails the training run if one ever appears, and a
test asserts the guard itself fires.

**No sensitive attributes.** No name, email, phone, card data, age, gender,
location, or any proxy for them. The `Customer` model does not store them.

## 4. Model selection

Two candidates, with the rule stated *before* the numbers were seen: keep the
interpretable baseline unless the challenger improves test PR-AUC by ≥ 0.02
absolute.

| | Logistic regression | HistGradientBoosting |
|---|---|---|
| PR-AUC | **0.6751** | 0.6550 |
| ROC-AUC | **0.7495** | 0.7311 |
| F1 | **0.6090** | 0.6057 |
| Brier | **0.2026** | 0.2118 |

**Selected: logistic regression.** The tree was 0.0201 PR-AUC *worse*, so the
interpretable model wins on merit rather than on preference. Complexity did not
earn its place here.

Both use `class_weight="balanced"` and isotonic calibration fitted on the
validation split.

## 5. Metrics (held-out test set, n = 900)

PR-AUC is the headline ranking metric: recovery positives are the minority class
and ROC-AUC flatters imbalanced problems (spec §10.7).

| Metric | Value |
|---|---|
| PR-AUC | 0.6751 |
| ROC-AUC | 0.7495 |
| Precision @ threshold | 0.4383 |
| Recall @ threshold | 0.9974 |
| F1 | 0.6090 |
| Log loss | 0.6294 |
| Brier score | 0.2026 |
| Expected calibration error | 0.0523 |

### Calibration

A predicted 0.8 should mean roughly 80% observed recovery. Reliability on test:

| Predicted | Observed | n |
|---|---|---|
| 0.047 | 0.087 | 23 |
| 0.145 | 0.226 | 217 |
| 0.231 | 0.250 | 52 |
| 0.327 | 0.338 | 222 |
| 0.456 | 0.521 | 165 |
| 0.632 | 0.685 | 92 |
| 0.733 | 0.800 | 115 |
| 0.833 | 0.667 | 6 |

ECE 0.0523. The model is **mildly under-confident** in the mid range — it
predicts 0.46 where 0.52 occurs. Under-confidence is the safer direction here: it
makes the agent act less often than optimal rather than more. The two highest
bins hold 6 cases each and should not be read as evidence of anything.

## 6. The decision threshold — and an honest caveat

The threshold is **0.07**, chosen to maximise expected net recovery on the
validation split. It is not 0.5, and the spec explicitly forbids defaulting to
0.5 (§10.8).

**But 0.07 actions 892 of 900 cases, which is very nearly "contact everyone".**
This is worth being direct about rather than burying.

It is a real consequence of the assumed economics, not a modelling error. At an
assumed intervention cost of ₹5 against a mean ticket around ₹8,600, even a 5%
recovery chance is worth pursuing. Expected value genuinely says act. The honest
reading is that **at these costs the model's value is in *ranking*, not in
filtering** — PR-AUC 0.675 means it orders the queue well, which is what drives
prioritisation, even though it screens out little.

Two things stop this being reckless in practice:

1. The **policy engine applies an independent confidence floor**
   (`min_auto_action_confidence`, default 0.35). Cases below it are escalated to
   a human, not actioned, whatever the model's expected value says. On the demo
   batch this routes 27 cases to review.
2. The threshold sweep below is reported so an operator can choose a more
   selective operating point deliberately.

| Threshold | Actioned | Share | Precision | Recall | Net (₹) |
|---|---|---|---|---|---|
| 0.10 | 877 | 97.4% | 0.4447 | 0.9949 | 32,59,609 |
| 0.20 | 660 | 73.3% | 0.5167 | 0.8699 | 26,48,639 |
| 0.30 | 608 | 67.6% | 0.5395 | 0.8367 | 26,06,068 |
| 0.40 | 386 | 42.9% | 0.6554 | 0.6454 | 19,99,791 |
| 0.50 | 221 | 24.6% | 0.7557 | 0.4260 | 13,55,141 |
| 0.70 | 127 | 14.1% | 0.8031 | 0.2602 | 8,49,431 |
| 0.80 | 12 | 1.3% | 0.8333 | 0.0255 | 56,205 |

Precision nearly doubles from 0.44 to 0.83 across the sweep, at the cost of most
of the recovered value. That trade-off belongs to the merchant, not the model.

## 7. Causal caveat — read this before quoting any recovery figure

**Observed recovery after an intervention does not prove the intervention caused
it** (spec §10.12).

The business metrics above credit the full recovered amount to every actioned
case that recovered. Some of those customers would have paid anyway. There is no
holdout group in this evaluation, so **the reported ₹32.7 lakh is gross recovery
among actioned cases, not incremental uplift.** The true causal effect is
smaller, and this pipeline does not measure it.

This also explains the low threshold: with no counterfactual, "action everything"
maximises measured recovery almost by definition.

Measuring incremental uplift properly needs a randomised holdout — deliberately
not contacting a random subset and comparing. That is planned for the simulator
(Milestone 4) and will be reported as *simulated incremental recovery*. Until
then, any figure quoted from this model card must be described as gross recovery
within a simulation.

## 8. Feature influence

Logistic coefficients on standardised inputs, largest magnitude first. These are
**correlational, not causal** (spec §10.9) — a large coefficient does not mean
changing that feature would change the outcome.

| Coefficient | Feature |
|---|---|
| +1.5678 | `failure_category = NETWORK_TIMEOUT` |
| −1.4728 | `failure_category = MANDATE_REVOKED` |
| +1.1529 | `failure_category = TECHNICAL_ERROR` |
| −0.9409 | `failure_category = CUSTOMER_ABANDONED` |
| +0.8315 | `failure_category = AUTHENTICATION_FAILED` |
| −0.8113 | `failure_category = CARD_EXPIRED` |
| −0.5330 | `failure_category = ISSUER_DECLINED` |
| −0.3667 | `attempt_count` |

These match the generator's ground-truth process — transient failures recover,
structural ones do not, and each retry is worth less. That agreement is a
**sanity check that the pipeline works**, not independent evidence: we wrote the
process the model recovered.

## 9. Failure modes and degradation

| Condition | Behaviour |
|---|---|
| Artifact missing (fresh clone) | Falls back to `DeterministicScorer`. Normal, not an error — binaries are gitignored. |
| Artifact corrupt | Logged, falls back. Broad exception catch is deliberate; deserialisation fails in unbounded ways. |
| scikit-learn not installed | Import guarded; falls back. |
| Unseen category at serving | `handle_unknown="ignore"` — degrades, does not crash. |
| Case with no linked customer | Neutral defaults; `failure_ratio` 0.5 so "unknown" ≠ "never failed". |

Every path above is covered by a test in `apps/api/tests/test_ml.py`.

## 10. Monitoring (documented, not implemented)

For the demo we record prediction distribution, feature availability, recovery
rate per batch, and model version on every case and audit event.

A production deployment would additionally need: data drift on the input
distribution, concept drift as failure-reason mixes shift, calibration drift,
and a retraining trigger. None of these are implemented, and the demo does not
claim them.

## 11. Reproducing

```bash
python -m ml.src.train --count 6000
```

Deterministic given the seed. Artifacts land in `ml/artifacts/`
(gitignored — rebuild rather than commit binaries). Live metrics are served at
`GET /api/v1/metrics/models`, which reports `trained: false` rather than 404
when no artifact exists.
