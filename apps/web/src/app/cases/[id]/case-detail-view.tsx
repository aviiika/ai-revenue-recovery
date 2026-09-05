"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import {
  api,
  ApiError,
  type AuditEventOut,
  type CaseDetail,
  type InterventionOut,
} from "@/lib/api";
import {
  ActorBadge,
  ConfidenceBadge,
  DecisionReason,
  ErrorState,
  LoadingRows,
  MoneyAmount,
  RecoverabilityTag,
  RecoveryStatusBadge,
  SyntheticBanner,
} from "@/components/domain";

export function CaseDetailView({ caseId }: { caseId: string }) {
  const queryClient = useQueryClient();
  const [stopReason, setStopReason] = useState("");

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["case", caseId],
    queryFn: () => api.getCase(caseId),
  });

  const evaluate = useMutation({
    mutationFn: () => api.evaluateCase(caseId),
    onSuccess: () => queryClient.invalidateQueries(),
  });

  const execute = useMutation({
    mutationFn: () => api.executeIntervention(caseId),
    onSuccess: () => queryClient.invalidateQueries(),
  });

  const stop = useMutation({
    mutationFn: (reason: string) => api.stopCase(caseId, reason, "operator"),
    onSuccess: () => {
      setStopReason("");
      queryClient.invalidateQueries();
    },
  });

  if (isLoading) return <LoadingRows rows={10} />;
  if (error) {
    return <ErrorState error={error as Error} onRetry={() => refetch()} />;
  }
  if (!data) return null;

  const isTerminal = ["RECOVERED", "STOPPED", "EXHAUSTED"].includes(
    data.current_state,
  );
  const selected = data.selected_strategy;
  // Execution is offered only where policy has chosen an action and the case is
  // still live. An already-recovered case must never get a second link.
  const canExecute = Boolean(selected) && !isTerminal;
  // The most recent attempt that actually produced a provider result.
  const paymentLink =
    [...data.interventions]
      .reverse()
      .find((i) => i.status === "EXECUTED" || i.status === "FAILED") ?? null;

  return (
    <div className="space-y-4">
      <div className="text-xs">
        <Link
          href="/cases"
          className="text-[var(--color-accent)] hover:underline"
        >
          ← All cases
        </Link>
      </div>

      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="font-mono text-lg font-semibold tracking-tight">
            {data.source_external_id}
          </h1>
          <div className="mt-1 flex items-center gap-2">
            <RecoveryStatusBadge state={data.current_state} />
            <span className="text-2xs text-[var(--color-ink-muted)]">
              {data.source_type.toLowerCase()} · detected{" "}
              {new Date(data.detected_at).toLocaleString("en-IN")}
            </span>
          </div>
        </div>
      </header>

      {data.is_synthetic ? <SyntheticBanner /> : null}

      {/* Three-column information hierarchy (spec section 16):
          financial context · decision · audit timeline */}
      <div className="grid gap-4 lg:grid-cols-3">
        <section className="space-y-3">
          <Panel title="Financial context">
            <dl className="space-y-2.5 text-sm">
              <Row label="Amount at risk">
                <MoneyAmount value={data.amount_at_risk} size="lg" />
              </Row>
              <Row label="Recovered">
                {data.recovered_amount.paise > 0 ? (
                  <MoneyAmount value={data.recovered_amount} size="lg" />
                ) : (
                  <span className="text-sm text-[var(--color-ink-muted)]">
                    Not recovered
                  </span>
                )}
              </Row>
              <Row label="Attempts">
                <span className="tabular text-sm">{data.attempt_count}</span>
              </Row>
              <Row label="Contactable">
                <span className="text-sm">
                  {data.do_not_contact ? "No — opted out" : "Yes"}
                </span>
              </Row>
            </dl>
          </Panel>

          <Panel title="Diagnosis">
            <dl className="space-y-2.5 text-sm">
              <Row label="Failure category">
                <span className="text-sm">
                  {data.failure_category.replace(/_/g, " ").toLowerCase()}
                </span>
              </Row>
              <Row label="Reason code">
                <code className="font-mono text-xs">
                  {data.failure_reason_code ?? "—"}
                </code>
              </Row>
              <Row label="Recoverability">
                <RecoverabilityTag value={data.recoverability} />
              </Row>
            </dl>
          </Panel>
        </section>

        <section className="space-y-3">
          <Panel title="Model assessment">
            <dl className="space-y-2.5 text-sm">
              <Row label="Recovery probability">
                <ConfidenceBadge value={data.recoverability_score} />
              </Row>
              <Row label="Model version">
                <code className="font-mono text-xs">
                  {data.model_version ?? "not scored"}
                </code>
              </Row>
              <Row label="Priority (expected net, paise)">
                <span className="tabular text-sm">
                  {data.priority_score !== null
                    ? Math.round(data.priority_score).toLocaleString("en-IN")
                    : "—"}
                </span>
              </Row>
            </dl>
          </Panel>

          {evaluate.data ? (
            <Panel title="Latest decision">
              <DecisionReason
                explanation={evaluate.data.decision.explanation}
                appliedRules={evaluate.data.decision.applied_rules}
                decisiveRule={evaluate.data.decision.decisive_rule}
              />
              {evaluate.data.decision.blocked.length > 0 ? (
                <div className="mt-3">
                  <h3 className="text-2xs font-medium uppercase tracking-wide text-[var(--color-ink-muted)]">
                    Blocked strategies
                  </h3>
                  <ul className="mt-1.5 space-y-1">
                    {evaluate.data.decision.blocked.map((item) => (
                      <li key={item.strategy} className="text-2xs">
                        <span className="font-medium">{item.strategy}</span> —{" "}
                        {item.reason}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </Panel>
          ) : null}

          {paymentLink ? <PaymentLinkPanel case={data} intervention={paymentLink} /> : null}

          <Panel title="Operator actions">
            <p className="text-2xs text-[var(--color-ink-secondary)]">
              Legal next states:{" "}
              {data.allowed_transitions.length > 0
                ? data.allowed_transitions
                    .map((s) => s.replace(/_/g, " ").toLowerCase())
                    .join(", ")
                : "none — this case is terminal"}
            </p>

            <div className="mt-3 space-y-2">
              {selected ? (
                <div className="rounded border border-[var(--color-border)] bg-[var(--color-surface-sunken)] p-2.5">
                  <div className="text-2xs uppercase tracking-wide text-[var(--color-ink-muted)]">
                    Selected intervention
                  </div>
                  <div className="mt-0.5 text-sm font-medium">
                    {selected.replace(/_/g, " ")}
                  </div>
                  {data.expected_net_paise !== null ? (
                    <>
                      <div className="mt-1.5 text-2xs uppercase tracking-wide text-[var(--color-ink-muted)]">
                        Expected net recovery
                      </div>
                      <div className="tabular text-sm">
                        {`INR ${(data.expected_net_paise / 100).toLocaleString("en-IN")}`}
                      </div>
                    </>
                  ) : null}
                </div>
              ) : null}

              {/* Only offered when policy has actually selected an action, so
                  the button can never imply an approval that did not happen. */}
              {canExecute ? (
                <button
                  type="button"
                  onClick={() => execute.mutate()}
                  disabled={execute.isPending}
                  className="w-full rounded border-2 border-[var(--color-progress-border)] bg-[var(--color-progress-surface)] px-3 py-2 text-xs font-semibold text-[var(--color-progress)] hover:opacity-90 disabled:opacity-40"
                >
                  {execute.isPending
                    ? "Creating…"
                    : selected === "CREATE_PAYMENT_LINK"
                      ? "Create Razorpay Payment Link"
                      : `Execute ${selected?.replace(/_/g, " ").toLowerCase()}`}
                </button>
              ) : null}

              <button
                type="button"
                onClick={() => evaluate.mutate()}
                disabled={isTerminal || evaluate.isPending}
                className="w-full rounded border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-3 py-1.5 text-xs font-medium hover:bg-[var(--color-surface-sunken)] disabled:opacity-40"
              >
                {evaluate.isPending ? "Evaluating…" : "Re-evaluate with policy engine"}
              </button>

              {!isTerminal ? (
                <div className="space-y-1.5">
                  <input
                    value={stopReason}
                    onChange={(event) => setStopReason(event.target.value)}
                    placeholder="Reason for stopping"
                    className="w-full rounded border border-[var(--color-border-strong)] px-2 py-1 text-xs"
                  />
                  <button
                    type="button"
                    onClick={() => stop.mutate(stopReason)}
                    disabled={!stopReason.trim() || stop.isPending}
                    className="w-full rounded border border-[var(--color-risk-border)] bg-[var(--color-risk-surface)] px-3 py-1.5 text-xs font-medium text-[var(--color-risk)] hover:opacity-90 disabled:opacity-40"
                  >
                    {stop.isPending ? "Stopping…" : "Stop recovery"}
                  </button>
                </div>
              ) : null}

              {evaluate.error ? (
                <p className="text-2xs text-[var(--color-risk)]">
                  {(evaluate.error as Error).message}
                </p>
              ) : null}
              {/* A 409 is the policy engine refusing — a cooldown, an
                  escalation, an already-recovered case. That is the agent
                  working, not Razorpay failing, and conflating the two would
                  tell the operator the opposite of what happened. */}
              {execute.error ? (
                execute.error instanceof ApiError &&
                execute.error.status === 409 ? (
                  <p className="text-2xs text-[var(--color-escalated)]">
                    Not executed — {execute.error.reason}
                  </p>
                ) : (
                  <p className="text-2xs text-[var(--color-risk)]">
                    Payment Link creation failed —{" "}
                    {execute.error instanceof ApiError
                      ? execute.error.reason
                      : (execute.error as Error).message}
                  </p>
                )
              ) : null}
              {execute.data && !execute.data.executed ? (
                <p className="text-2xs text-[var(--color-escalated)]">
                  Not executed — {execute.data.reason}
                </p>
              ) : null}
            </div>
          </Panel>
        </section>

        <section className="lg:col-span-1">
          <Panel
            title="Audit timeline"
            caption={`${data.audit_trail.length} events — append-only`}
          >
            <AuditTimeline events={data.audit_trail} />
          </Panel>
        </section>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

/**
 * The full history of a case (spec FR-8, section 16).
 *
 * Rendered oldest-first so it reads as a narrative. Each entry shows who acted
 * — system, model, human or webhook — because "who decided this" is a first
 * class question the audit trail exists to answer.
 */
function AuditTimeline({ events }: { events: AuditEventOut[] }) {
  if (events.length === 0) {
    return (
      <p className="text-xs text-[var(--color-ink-muted)]">No events yet.</p>
    );
  }

  return (
    <ol className="relative space-y-3 border-l border-[var(--color-border)] pl-4">
      {events.map((event) => (
        <li key={event.sequence} className="relative">
          <span
            aria-hidden
            className="absolute -left-[1.3rem] top-1.5 h-2 w-2 rounded-full border border-[var(--color-border-strong)] bg-[var(--color-surface)]"
          />
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="tabular text-2xs text-[var(--color-ink-muted)]">
              #{event.sequence}
            </span>
            <ActorBadge actor={event.actor_type} />
            <span className="text-2xs font-medium">
              {event.event_type.replace(/_/g, " ").toLowerCase()}
            </span>
          </div>
          <p className="mt-0.5 text-xs leading-relaxed text-[var(--color-ink-secondary)]">
            {event.summary}
          </p>
          <div className="mt-0.5 text-2xs text-[var(--color-ink-muted)]">
            {new Date(event.created_at).toLocaleString("en-IN")}
            {event.actor_id ? ` · ${event.actor_id}` : ""}
          </div>
        </li>
      ))}
    </ol>
  );
}

function Panel({
  title,
  caption,
  children,
}: {
  title: string;
  caption?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <h2 className="text-sm font-semibold">{title}</h2>
      {caption ? (
        <p className="text-2xs text-[var(--color-ink-muted)]">{caption}</p>
      ) : null}
      <div className="mt-3">{children}</div>
    </section>
  );
}

function Row({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-2xs uppercase tracking-wide text-[var(--color-ink-muted)]">
        {label}
      </dt>
      <dd className="text-right">{children}</dd>
    </div>
  );
}

/**
 * The Razorpay Payment Link, once one has been created.
 *
 * Two rules this component exists to enforce visually:
 *
 * 1. **A link is not a recovery.** Status reads "Awaiting payment" until a
 *    payment event actually settles the case. Creating the link changes
 *    nothing about recovered revenue, and the panel says so.
 * 2. **Never show a fabricated URL.** The link is rendered only when the
 *    provider returned a real `short_url`. A simulated execution says exactly
 *    that instead, so a demo cannot pass the simulator off as Razorpay.
 */
export function PaymentLinkPanel({
  case: detail,
  intervention,
}: {
  case: CaseDetail;
  intervention: InterventionOut;
}) {
  const result = intervention.result ?? {};
  const shortUrl =
    typeof result.short_url === "string" ? result.short_url : null;
  const linkId =
    typeof result.payment_link_id === "string" ? result.payment_link_id : null;
  const simulated = result.simulated !== false;
  const failed = intervention.status === "FAILED";
  const recovered = detail.current_state === "RECOVERED";

  if (failed) {
    return (
      <section className="rounded-md border border-[var(--color-risk-border)] bg-[var(--color-risk-surface)] p-4">
        <h2 className="text-sm font-semibold text-[var(--color-risk)]">
          Payment Link creation failed
        </h2>
        <p className="mt-1 text-xs text-[var(--color-ink-secondary)]">
          {String(result.error ?? "The provider rejected the request.")}
        </p>
        <p className="mt-2 text-2xs text-[var(--color-ink-muted)]">
          The failure is recorded in the audit timeline and the case was not
          advanced, so it can be retried.
        </p>
      </section>
    );
  }

  if (intervention.strategy !== "CREATE_PAYMENT_LINK") {
    return (
      <section className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
        <h2 className="text-sm font-semibold">Executed action</h2>
        <p className="mt-1 text-xs text-[var(--color-ink-secondary)]">
          {intervention.strategy.replace(/_/g, " ").toLowerCase()} ·{" "}
          {intervention.channel}
        </p>
        {typeof result.would_send === "string" ? (
          <p className="mt-2 text-2xs text-[var(--color-ink-muted)]">
            Would send: {result.would_send}
          </p>
        ) : null}
      </section>
    );
  }

  return (
    <section
      className={
        recovered
          ? "rounded-md border-2 border-[var(--color-recovered-border)] bg-[var(--color-recovered-surface)] p-4"
          : "rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] p-4"
      }
    >
      <div className="flex items-start justify-between gap-2">
        <h2 className="text-sm font-semibold">Razorpay Payment Link</h2>
        <span
          className={
            simulated
              ? "rounded border border-[var(--color-escalated-border)] bg-[var(--color-escalated-surface)] px-1.5 py-0.5 text-2xs font-medium text-[var(--color-escalated)]"
              : "rounded border border-[var(--color-progress-border)] bg-[var(--color-progress-surface)] px-1.5 py-0.5 text-2xs font-medium text-[var(--color-progress)]"
          }
        >
          {simulated ? "SIMULATED" : "RAZORPAY TEST MODE"}
        </span>
      </div>

      <dl className="mt-3 space-y-2 text-sm">
        <Row label="Status">
          <span
            className={
              recovered
                ? "text-sm font-semibold text-[var(--color-recovered)]"
                : "text-sm"
            }
          >
            {recovered ? "PAID" : "Awaiting payment"}
          </span>
        </Row>
        <Row label="Amount">
          <MoneyAmount value={detail.amount_at_risk} />
        </Row>
      </dl>

      {recovered ? (
        <p className="mt-3 rounded bg-[var(--color-surface)] px-2.5 py-2 text-sm font-semibold text-[var(--color-recovered)]">
          ✓ Revenue recovered: {detail.recovered_amount.formatted}
        </p>
      ) : null}

      {shortUrl ? (
        <a
          href={shortUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-3 block w-full rounded border-2 border-[var(--color-progress-border)] bg-[var(--color-progress-surface)] px-3 py-2 text-center text-xs font-semibold text-[var(--color-progress)] hover:opacity-90"
        >
          Open Payment Link
        </a>
      ) : (
        <p className="mt-3 rounded border border-dashed border-[var(--color-border-strong)] px-2.5 py-2 text-2xs text-[var(--color-ink-secondary)]">
          No live link: this ran through the simulator because Razorpay
          credentials are not configured. Set RAZORPAY_KEY_ID and
          RAZORPAY_KEY_SECRET to create a real test-mode link.
        </p>
      )}

      <dl className="mt-3 space-y-1.5">
        {linkId ? (
          <Row label="Payment Link ID">
            <code className="font-mono text-2xs">{linkId}</code>
          </Row>
        ) : null}
        <Row label="Created">
          <span className="text-2xs">
            {intervention.executed_at
              ? new Date(intervention.executed_at).toLocaleString("en-IN")
              : "—"}
          </span>
        </Row>
        <Row label="Attempt">
          <span className="tabular text-2xs">#{intervention.attempt_number}</span>
        </Row>
      </dl>

      {!recovered ? (
        <p className="mt-3 text-2xs text-[var(--color-ink-muted)]">
          Creating a link is not a recovery. This case becomes RECOVERED only
          when a payment event confirms the customer actually paid.
        </p>
      ) : null}
    </section>
  );
}
