"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api, type ReviewOut, type ReviewStatus } from "@/lib/api";
import {
  ConfidenceBadge,
  DecisionReason,
  EmptyState,
  ErrorState,
  LoadingRows,
  MetricCard,
  MoneyAmount,
  RecoveryStatusBadge,
  SyntheticBanner,
} from "@/components/domain";

/**
 * The human review queue (spec FR-3 Flow C, section 16).
 *
 * A reviewer needs three things on one screen to decide without digging: what
 * is at stake, why the agent stopped, and what it proposed. Actions are
 * deliberately at the row level — sending someone into a detail page to approve
 * a case makes a queue of forty unworkable.
 *
 * Notes are required on every action. An override with no recorded reason is
 * exactly the kind of unexplained decision the audit trail exists to prevent.
 */
export default function ReviewsPage() {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<ReviewStatus>("PENDING");
  const [reviewer, setReviewer] = useState("ops@example");
  const [active, setActive] = useState<string | null>(null);
  const [notes, setNotes] = useState("");
  const [strategy, setStrategy] = useState("CREATE_PAYMENT_LINK");

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["reviews", status],
    queryFn: () => api.listReviews(status),
  });

  const settle = () => {
    setActive(null);
    setNotes("");
    queryClient.invalidateQueries();
  };

  const approve = useMutation({
    mutationFn: (id: string) => api.approveReview(id, reviewer, notes),
    onSuccess: settle,
  });
  const override = useMutation({
    mutationFn: (id: string) => api.overrideReview(id, reviewer, notes, strategy),
    onSuccess: settle,
  });
  const reject = useMutation({
    mutationFn: (id: string) => api.rejectReview(id, reviewer, notes),
    onSuccess: settle,
  });

  const pending = approve.isPending || override.isPending || reject.isPending;
  const actionError =
    (approve.error as Error | null) ??
    (override.error as Error | null) ??
    (reject.error as Error | null);

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Review queue</h1>
          <p className="text-xs text-[var(--color-ink-secondary)]">
            Cases the agent escalated rather than acting on alone. Ordered by
            money at stake.
          </p>
        </div>
        <label className="flex flex-col gap-1">
          <span className="text-2xs font-medium uppercase tracking-wide text-[var(--color-ink-muted)]">
            Reviewing as
          </span>
          <input
            value={reviewer}
            onChange={(event) => setReviewer(event.target.value)}
            className="w-48 rounded border border-[var(--color-border-strong)] px-2 py-1 text-sm"
          />
        </label>
      </header>

      <SyntheticBanner />

      <section className="grid gap-3 sm:grid-cols-3">
        <MetricCard
          label="Awaiting review"
          value={
            <span className="tabular text-2xl font-semibold">
              {data?.pending ?? 0}
            </span>
          }
          emphasis
        />
        <MetricCard
          label="Value awaiting review"
          value={
            data ? <MoneyAmount value={data.value_awaiting_review} size="lg" /> : null
          }
          emphasis
        />
        <MetricCard
          label="By reason"
          value={
            <div className="space-y-0.5 text-2xs">
              {Object.entries(data?.by_reason ?? {}).map(([reason, count]) => (
                <div key={reason} className="flex justify-between gap-2">
                  <span>{reason.replace(/_/g, " ").toLowerCase()}</span>
                  <span className="tabular font-medium">{count}</span>
                </div>
              ))}
              {Object.keys(data?.by_reason ?? {}).length === 0 ? (
                <span className="text-[var(--color-ink-muted)]">—</span>
              ) : null}
            </div>
          }
        />
      </section>

      <div className="flex gap-1.5">
        {(["PENDING", "APPROVED", "OVERRIDDEN", "REJECTED"] as ReviewStatus[]).map(
          (value) => (
            <button
              key={value}
              type="button"
              onClick={() => setStatus(value)}
              aria-pressed={status === value}
              className={
                status === value
                  ? "rounded border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2.5 py-1 text-xs font-medium"
                  : "rounded border border-transparent px-2.5 py-1 text-xs text-[var(--color-ink-secondary)] hover:bg-[var(--color-surface-sunken)]"
              }
            >
              {value.toLowerCase()}
            </button>
          ),
        )}
      </div>

      {actionError ? <ErrorState error={actionError} /> : null}

      {isLoading ? (
        <LoadingRows rows={5} />
      ) : error ? (
        <ErrorState error={error as Error} onRetry={() => refetch()} />
      ) : !data || data.items.length === 0 ? (
        <EmptyState
          title={
            status === "PENDING"
              ? "Nothing awaiting review"
              : `No ${status.toLowerCase()} reviews`
          }
          description={
            status === "PENDING"
              ? "The agent has not escalated any cases. Run it from the Overview page to generate work."
              : "Decided reviews will appear here once reviewers have worked the queue."
          }
        />
      ) : (
        <ul className="space-y-3">
          {data.items.map((review) => (
            <ReviewCard
              key={review.id}
              review={review}
              isOpen={active === review.id}
              onToggle={() => {
                setActive(active === review.id ? null : review.id);
                setNotes("");
              }}
              notes={notes}
              onNotes={setNotes}
              strategy={strategy}
              onStrategy={setStrategy}
              disabled={pending}
              onApprove={() => approve.mutate(review.id)}
              onOverride={() => override.mutate(review.id)}
              onReject={() => reject.mutate(review.id)}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

const STRATEGIES = [
  "WAIT_AND_RETRY",
  "SEND_REMINDER_SIMULATED",
  "REQUEST_ALTERNATE_METHOD",
  "CREATE_PAYMENT_LINK",
];

function ReviewCard({
  review,
  isOpen,
  onToggle,
  notes,
  onNotes,
  strategy,
  onStrategy,
  disabled,
  onApprove,
  onOverride,
  onReject,
}: {
  review: ReviewOut;
  isOpen: boolean;
  onToggle: () => void;
  notes: string;
  onNotes: (value: string) => void;
  strategy: string;
  onStrategy: (value: string) => void;
  disabled: boolean;
  onApprove: () => void;
  onOverride: () => void;
  onReject: () => void;
}) {
  const decided = review.status !== "PENDING";
  // A max-attempts escalation has no proposal to approve -- the agent ran out
  // of budget rather than choosing something. Offering Approve there would just
  // produce a 409, so the button is disabled and the reason is stated.
  const canApprove = review.proposed_strategy !== null;

  return (
    <li className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Link
            href={`/cases/${review.recovery_case_id}`}
            className="font-mono text-sm font-medium text-[var(--color-accent)] hover:underline"
          >
            {review.source_external_id}
          </Link>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            {review.current_state ? (
              <RecoveryStatusBadge state={review.current_state} />
            ) : null}
            <span className="text-2xs text-[var(--color-ink-muted)]">
              {review.reason.replace(/_/g, " ").toLowerCase()}
            </span>
            <span className="text-2xs text-[var(--color-ink-muted)]">
              · {review.attempt_count} attempt(s)
            </span>
          </div>
        </div>
        <div className="text-right">
          <MoneyAmount value={review.amount_at_risk} size="lg" />
          <div className="mt-0.5">
            <ConfidenceBadge value={review.recoverability_score} />
          </div>
        </div>
      </div>

      {review.explanation ? (
        <div className="mt-3">
          <DecisionReason
            explanation={review.explanation}
            appliedRules={review.applied_rules}
          />
        </div>
      ) : null}

      <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
        <p className="text-2xs text-[var(--color-ink-secondary)]">
          {decided ? (
            <>
              {review.status.toLowerCase()} by {review.reviewer} —{" "}
              {review.decision_notes}
            </>
          ) : (
            <>
              Agent proposed{" "}
              <span className="font-medium">
                {review.proposed_strategy?.replace(/_/g, " ").toLowerCase() ??
                  "no action"}
              </span>
            </>
          )}
        </p>
        {!decided ? (
          <button
            type="button"
            onClick={onToggle}
            className="rounded border border-[var(--color-border-strong)] px-2.5 py-1 text-xs font-medium hover:bg-[var(--color-surface-sunken)]"
          >
            {isOpen ? "Cancel" : "Decide"}
          </button>
        ) : null}
      </div>

      {isOpen && !decided ? (
        <div className="mt-3 space-y-2 border-t border-[var(--color-border)] pt-3">
          <label className="block">
            <span className="text-2xs font-medium uppercase tracking-wide text-[var(--color-ink-muted)]">
              Reason for this decision (required)
            </span>
            <input
              value={notes}
              onChange={(event) => onNotes(event.target.value)}
              placeholder="Why are you deciding this way?"
              className="mt-1 w-full rounded border border-[var(--color-border-strong)] px-2 py-1 text-sm"
            />
          </label>

          <div className="flex flex-wrap items-end gap-2">
            <button
              type="button"
              onClick={onApprove}
              disabled={disabled || !notes.trim() || !canApprove}
              title={
                canApprove
                  ? undefined
                  : "The agent proposed no action for this case, so there is nothing to approve. Override or reject instead."
              }
              className="rounded border border-[var(--color-recovered-border)] bg-[var(--color-recovered-surface)] px-3 py-1.5 text-xs font-medium text-[var(--color-recovered)] disabled:opacity-40"
            >
              Approve
            </button>

            <div className="flex items-end gap-1.5">
              <label className="flex flex-col">
                <span className="text-2xs text-[var(--color-ink-muted)]">
                  Override with
                </span>
                <select
                  value={strategy}
                  onChange={(event) => onStrategy(event.target.value)}
                  className="rounded border border-[var(--color-border-strong)] px-2 py-1 text-xs"
                >
                  {STRATEGIES.map((value) => (
                    <option key={value} value={value}>
                      {value.replace(/_/g, " ").toLowerCase()}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                onClick={onOverride}
                disabled={disabled || !notes.trim()}
                className="rounded border border-[var(--color-border-strong)] px-3 py-1.5 text-xs font-medium disabled:opacity-40"
              >
                Override
              </button>
            </div>

            <button
              type="button"
              onClick={onReject}
              disabled={disabled || !notes.trim()}
              className="rounded border border-[var(--color-risk-border)] bg-[var(--color-risk-surface)] px-3 py-1.5 text-xs font-medium text-[var(--color-risk)] disabled:opacity-40"
            >
              Reject &amp; stop
            </button>
          </div>
          <p className="text-2xs text-[var(--color-ink-muted)]">
            {canApprove
              ? "Overrides are limited to strategies the merchant has enabled, and a case whose customer has opted out cannot be approved."
              : "This case exhausted its automated attempts, so there is no proposal to approve — choose an action yourself, or stop the case."}
          </p>
        </div>
      ) : null}
    </li>
  );
}
