"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api, type AuditEventOut } from "@/lib/api";
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
