/**
 * Domain UI primitives (spec section 8).
 *
 * These encode presentation rules, never business rules. `RecoveryStatusBadge`
 * decides how a state *looks*; it has no opinion about which states are legal —
 * that lives in the backend state machine and arrives on the wire.
 *
 * Accessibility rule applied throughout: **state is never conveyed by colour
 * alone**. Every badge carries a text label, and terminal states additionally
 * carry a distinguishing border weight so they remain separable in greyscale
 * and for colour-blind users.
 */

import type { ReactNode } from "react";
import type { CaseState, MoneyOut, Recoverability } from "@/lib/api";

function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

/* -------------------------------------------------------------------------- */

export function MoneyAmount({
  value,
  size = "base",
  muted = false,
}: {
  value: MoneyOut;
  size?: "sm" | "base" | "lg" | "xl";
  muted?: boolean;
}) {
  const sizes = {
    sm: "text-xs",
    base: "text-sm",
    lg: "text-lg font-semibold",
    xl: "text-3xl font-semibold tracking-tight",
  } as const;

  return (
    <span
      className={cx(
        "tabular",
        sizes[size],
        muted ? "text-[var(--color-ink-muted)]" : "text-[var(--color-ink)]",
      )}
      // The accessible name spells the amount out; the visual form is compact.
      title={value.formatted}
    >
      {value.formatted}
    </span>
  );
}

/* -------------------------------------------------------------------------- */

/**
 * Tone classes are written out in full rather than interpolated.
 *
 * Tailwind extracts classes by scanning source text, so a name built at runtime
 * (`bg-[var(--color-${tone})]`) is invisible to it and the style silently never
 * ships. Every variant therefore appears literally below.
 */
const TONE = {
  progress:
    "bg-[var(--color-progress-surface)] text-[var(--color-progress)] border-[var(--color-progress-border)]",
  escalated:
    "bg-[var(--color-escalated-surface)] text-[var(--color-escalated)] border-[var(--color-escalated-border)]",
  recovered:
    "bg-[var(--color-recovered-surface)] text-[var(--color-recovered)] border-[var(--color-recovered-border)]",
  stopped:
    "bg-[var(--color-stopped-surface)] text-[var(--color-stopped)] border-[var(--color-stopped-border)]",
  risk: "bg-[var(--color-risk-surface)] text-[var(--color-risk)] border-[var(--color-risk-border)]",
} as const;

type Tone = keyof typeof TONE;

const STATE_STYLE: Record<
  CaseState,
  { label: string; tone: Tone; terminal?: boolean }
> = {
  NEW: { label: "New", tone: "progress" },
  DIAGNOSED: { label: "Diagnosed", tone: "progress" },
  SCORED: { label: "Scored", tone: "progress" },
  ACTION_SELECTED: { label: "Action selected", tone: "progress" },
  ACTION_PENDING: { label: "Action pending", tone: "progress" },
  ACTION_EXECUTED: { label: "Action executed", tone: "progress" },
  OBSERVING: { label: "Observing", tone: "progress" },
  RETRY_ELIGIBLE: { label: "Retry eligible", tone: "escalated" },
  ESCALATED: { label: "Escalated", tone: "escalated" },
  RECOVERED: { label: "Recovered", tone: "recovered", terminal: true },
  EXHAUSTED: { label: "Exhausted", tone: "stopped", terminal: true },
  STOPPED: { label: "Stopped", tone: "stopped", terminal: true },
};

export function RecoveryStatusBadge({ state }: { state: CaseState }) {
  const config = STATE_STYLE[state] ?? { label: state, tone: "stopped" as Tone };
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1.5 rounded px-2 py-0.5 text-2xs font-medium whitespace-nowrap",
        TONE[config.tone],
        // Terminal states carry a heavier border so they stay distinguishable
        // in greyscale and for colour-blind users — never colour alone.
        config.terminal ? "border-2" : "border",
      )}
    >
      {config.label}
    </span>
  );
}

/* -------------------------------------------------------------------------- */

export function ConfidenceBadge({ value }: { value: number | null }) {
  if (value === null) {
    return (
      <span className="text-2xs text-[var(--color-ink-muted)]">Not scored</span>
    );
  }
  const pct = Math.round(value * 100);
  // Bands are presentation only. The automation threshold that actually gates
  // action lives in the backend policy engine.
  const band = pct >= 60 ? "High" : pct >= 35 ? "Medium" : "Low";
  const tone: Tone = pct >= 60 ? "recovered" : pct >= 35 ? "escalated" : "stopped";

  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="tabular text-sm font-medium">{pct}%</span>
      <span
        className={cx("rounded border px-1.5 py-0.5 text-2xs font-medium", TONE[tone])}
      >
        {band}
      </span>
    </span>
  );
}

/* -------------------------------------------------------------------------- */

const RECOVERABILITY_LABEL: Record<Recoverability, string> = {
  TRANSIENT: "Transient — likely to clear on retry",
  ACTIONABLE: "Actionable — needs customer action",
  STRUCTURAL: "Structural — needs re-authorisation",
  UNRECOVERABLE: "Unrecoverable",
};

export function RecoverabilityTag({
  value,
}: {
  value: Recoverability | null;
}) {
  if (!value) {
    return <span className="text-2xs text-[var(--color-ink-muted)]">—</span>;
  }
  return (
    <span
      className="text-2xs text-[var(--color-ink-secondary)]"
      title={RECOVERABILITY_LABEL[value]}
    >
      {value.charAt(0) + value.slice(1).toLowerCase()}
    </span>
  );
}

/* -------------------------------------------------------------------------- */

export function MetricCard({
  label,
  value,
  sublabel,
  emphasis = false,
}: {
  label: string;
  value: ReactNode;
  sublabel?: ReactNode;
  emphasis?: boolean;
}) {
  return (
    <div
      className={cx(
        "rounded-md border bg-[var(--color-surface)] px-4 py-3",
        emphasis
          ? "border-[var(--color-border-strong)]"
          : "border-[var(--color-border)]",
      )}
    >
      <div className="text-2xs font-medium uppercase tracking-wide text-[var(--color-ink-muted)]">
        {label}
      </div>
      <div className="mt-1.5">{value}</div>
      {sublabel ? (
        <div className="mt-1 text-2xs text-[var(--color-ink-secondary)]">
          {sublabel}
        </div>
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */

/**
 * Why the agent decided what it decided (spec FR-10).
 *
 * Renders the policy engine's own output — the rules that fired and the
 * strategies that were blocked. Nothing here is inferred client-side.
 */
export function DecisionReason({
  explanation,
  appliedRules,
  decisiveRule,
}: {
  explanation: string;
  appliedRules: string[];
  decisiveRule?: string | null;
}) {
  return (
    <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface-sunken)] p-3">
      <p className="text-sm leading-relaxed text-[var(--color-ink)]">
        {explanation}
      </p>
      {appliedRules.length > 0 ? (
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          {appliedRules.map((rule) => (
            <code
              key={rule}
              className={cx(
                "rounded px-1.5 py-0.5 font-mono text-2xs",
                rule === decisiveRule
                  ? "bg-[var(--color-risk-surface)] text-[var(--color-risk)] font-semibold"
                  : "bg-[var(--color-surface)] text-[var(--color-ink-secondary)] border border-[var(--color-border)]",
              )}
              title={rule === decisiveRule ? "Decisive rule" : "Applied rule"}
            >
              {rule}
            </code>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */

const ACTOR_STYLE: Record<string, { label: string; tone: Tone }> = {
  SYSTEM: { label: "System", tone: "stopped" },
  MODEL: { label: "Model", tone: "progress" },
  HUMAN: { label: "Human", tone: "escalated" },
  WEBHOOK: { label: "Webhook", tone: "recovered" },
};

export function ActorBadge({ actor }: { actor: string }) {
  const config = ACTOR_STYLE[actor] ?? { label: actor, tone: "stopped" as Tone };
  return (
    <span
      className={cx(
        "rounded border px-1.5 py-0.5 text-2xs font-medium",
        TONE[config.tone],
      )}
    >
      {config.label}
    </span>
  );
}

/* -------------------------------------------------------------------------- */

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="rounded-md border border-dashed border-[var(--color-border-strong)] bg-[var(--color-surface)] px-6 py-12 text-center">
      <p className="text-sm font-medium text-[var(--color-ink)]">{title}</p>
      <p className="mx-auto mt-1 max-w-md text-sm text-[var(--color-ink-secondary)]">
        {description}
      </p>
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  );
}

export function ErrorState({
  error,
  onRetry,
}: {
  error: Error;
  onRetry?: () => void;
}) {
  return (
    <div className="rounded-md border border-[var(--color-risk-border)] bg-[var(--color-risk-surface)] px-4 py-3">
      <p className="text-sm font-medium text-[var(--color-risk)]">
        Could not load this data
      </p>
      <p className="mt-1 text-sm text-[var(--color-ink-secondary)]">
        {error.message}
      </p>
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 rounded border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-3 py-1.5 text-xs font-medium hover:bg-[var(--color-surface-sunken)]"
        >
          Retry
        </button>
      ) : null}
    </div>
  );
}

export function LoadingRows({ rows = 5 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-busy="true" aria-live="polite">
      <span className="sr-only">Loading</span>
      {Array.from({ length: rows }).map((_, index) => (
        <div
          key={index}
          className="h-9 animate-pulse rounded bg-[var(--color-surface-sunken)]"
        />
      ))}
    </div>
  );
}

/**
 * Shown wherever generated data is displayed.
 *
 * The spec forbids presenting synthetic results as real, so this is not
 * optional decoration — it is a correctness requirement of the UI.
 */
export function SyntheticBanner() {
  return (
    <div className="flex items-start gap-2 rounded-md border border-[var(--color-escalated-border)] bg-[var(--color-escalated-surface)] px-3 py-2">
      <span className="mt-px text-2xs font-bold uppercase tracking-wide text-[var(--color-escalated)]">
        Synthetic
      </span>
      <p className="text-2xs leading-relaxed text-[var(--color-ink-secondary)]">
        This view contains deterministically generated demo data. Figures
        describe a controlled simulation, not real-world production performance.
      </p>
    </div>
  );
}
