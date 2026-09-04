"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type PolicyResponse } from "@/lib/api";
import { ErrorState, LoadingRows, MoneyAmount } from "@/components/domain";

/**
 * Policy settings (spec section 16).
 *
 * These fields are the agent's safety bounds, so the screen is deliberately
 * plain and explicit about what each one does. Validation is the backend's —
 * the form submits and renders whatever the API says is wrong, rather than
 * duplicating the rules here where they could drift.
 */
export default function PoliciesPage() {
  const queryClient = useQueryClient();
  // Unsaved edits only. The form renders `draft ?? data`, so server state is
  // the source of truth until the user actually changes something — no effect
  // syncing props into state, and no stale draft lingering after a refetch.
  const [draft, setDraft] = useState<PolicyResponse | null>(null);
  const [saved, setSaved] = useState(false);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["policies"],
    queryFn: api.getPolicies,
  });

  const form = draft ?? data ?? null;

  const save = useMutation({
    mutationFn: () => {
      if (!form) throw new Error("nothing to save");
      return api.updatePolicies({
        max_automated_attempts: form.max_automated_attempts,
        cooldown_hours: form.cooldown_hours,
        min_auto_action_confidence: form.min_auto_action_confidence,
        high_value_threshold_paise: form.high_value_threshold_paise,
        min_expected_net_recovery_paise: form.min_expected_net_recovery_paise,
        backoff_base_hours: form.backoff_base_hours,
        backoff_cap_hours: form.backoff_cap_hours,
        enabled_strategies: form.enabled_strategies,
      });
    },
    onSuccess: () => {
      setSaved(true);
      // Drop the draft so the freshly-saved server state shows through.
      setDraft(null);
      queryClient.invalidateQueries();
      window.setTimeout(() => setSaved(false), 3000);
    },
  });

  if (isLoading) return <LoadingRows rows={8} />;
  if (error) return <ErrorState error={error as Error} onRetry={() => refetch()} />;
  if (!form) return null;

  const set = <K extends keyof PolicyResponse>(key: K, value: PolicyResponse[K]) =>
    setDraft({ ...form, [key]: value });

  const toggleStrategy = (strategy: string) =>
    set(
      "enabled_strategies",
      form.enabled_strategies.includes(strategy)
        ? form.enabled_strategies.filter((s) => s !== strategy)
        : [...form.enabled_strategies, strategy],
    );

  return (
    <div className="max-w-3xl space-y-4">
      <header>
        <h1 className="text-lg font-semibold tracking-tight">Policies</h1>
        <p className="text-xs text-[var(--color-ink-secondary)]">
          The bounds the agent operates within. Every one of these is enforced
          by the policy engine on the server, not by this form.
        </p>
      </header>

      {save.error ? <ValidationErrors error={save.error as Error} /> : null}
      {saved ? (
        <div className="rounded-md border border-[var(--color-recovered-border)] bg-[var(--color-recovered-surface)] px-3 py-2 text-xs text-[var(--color-recovered)]">
          Saved. New decisions use these bounds immediately.
        </div>
      ) : null}

      <Section title="Stopping rules">
        <Field
          label="Maximum automated attempts"
          help="After this many attempts the case goes to a human instead of being retried."
        >
          <NumberInput
            value={form.max_automated_attempts}
            onChange={(v) => set("max_automated_attempts", v)}
          />
        </Field>
        <Field
          label="Cooldown (hours)"
          help="Minimum gap between two actions on the same case. Also caps reminders per day."
        >
          <NumberInput
            value={form.cooldown_hours}
            onChange={(v) => set("cooldown_hours", v)}
          />
        </Field>
        <Field
          label="Minimum expected net recovery (paise)"
          help="Below this, the agent stops rather than spending more than the case is worth."
        >
          <NumberInput
            value={form.min_expected_net_recovery_paise}
            onChange={(v) => set("min_expected_net_recovery_paise", v)}
          />
        </Field>
      </Section>

      <Section title="Escalation">
        <Field
          label="Minimum confidence to act alone"
          help="Below this recovery probability the agent escalates instead of acting. 0–1."
        >
          <input
            type="number"
            step="0.01"
            min={0}
            max={1}
            value={form.min_auto_action_confidence}
            onChange={(e) =>
              set("min_auto_action_confidence", Number(e.target.value))
            }
            className="w-32 rounded border border-[var(--color-border-strong)] px-2 py-1 text-sm tabular"
          />
        </Field>
        <Field
          label="High-value threshold (paise)"
          help="At or above this amount, a low-confidence case always goes to a human."
        >
          <div className="flex items-center gap-2">
            <NumberInput
              value={form.high_value_threshold_paise}
              onChange={(v) => set("high_value_threshold_paise", v)}
            />
            <MoneyAmount
              value={{
                paise: form.high_value_threshold_paise,
                formatted: `INR ${(form.high_value_threshold_paise / 100).toLocaleString("en-IN")}`,
              }}
              size="sm"
              muted
            />
          </div>
        </Field>
      </Section>

      <Section title="Retry backoff">
        <Field label="Base (hours)" help="Attempt N waits base × 2^(N−1).">
          <NumberInput
            value={form.backoff_base_hours}
            onChange={(v) => set("backoff_base_hours", v)}
          />
        </Field>
        <Field label="Cap (hours)" help="Backoff never exceeds this.">
          <NumberInput
            value={form.backoff_cap_hours}
            onChange={(v) => set("backoff_cap_hours", v)}
          />
        </Field>
      </Section>

      <Section title="Enabled actions">
        <p className="text-2xs text-[var(--color-ink-secondary)]">
          A disabled action is blocked no matter how attractive its expected
          value — and a human reviewer cannot select it either.
        </p>
        <div className="mt-2 space-y-1.5">
          {form.available_strategies.map((strategy) => (
            <label key={strategy} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={form.enabled_strategies.includes(strategy)}
                onChange={() => toggleStrategy(strategy)}
              />
              <span>{strategy.replace(/_/g, " ").toLowerCase()}</span>
            </label>
          ))}
        </div>
      </Section>

      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => save.mutate()}
          disabled={save.isPending}
          className="rounded border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-4 py-1.5 text-sm font-medium hover:bg-[var(--color-surface-sunken)] disabled:opacity-50"
        >
          {save.isPending ? "Saving…" : "Save policy"}
        </button>
        <button
          type="button"
          onClick={() => setDraft(null)}
          disabled={draft === null}
          className="text-xs text-[var(--color-ink-secondary)] hover:underline disabled:opacity-40"
        >
          Discard changes
        </button>
      </div>
    </div>
  );
}

function ValidationErrors({ error }: { error: Error }) {
  return (
    <div className="rounded-md border border-[var(--color-risk-border)] bg-[var(--color-risk-surface)] px-3 py-2">
      <p className="text-sm font-medium text-[var(--color-risk)]">
        Policy rejected
      </p>
      <p className="mt-1 text-xs text-[var(--color-ink-secondary)]">
        {error.message}
      </p>
      <p className="mt-1 text-2xs text-[var(--color-ink-muted)]">
        Values are validated rather than clamped — a limit you set is the limit
        you get, or you are told why it is not allowed.
      </p>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <h2 className="text-sm font-semibold">{title}</h2>
      <div className="mt-3 space-y-3">{children}</div>
    </section>
  );
}

function Field({
  label,
  help,
  children,
}: {
  label: string;
  help: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="max-w-md">
        <div className="text-sm">{label}</div>
        <div className="text-2xs text-[var(--color-ink-secondary)]">{help}</div>
      </div>
      {children}
    </div>
  );
}

function NumberInput({
  value,
  onChange,
}: {
  value: number;
  onChange: (value: number) => void;
}) {
  return (
    <input
      type="number"
      value={value}
      onChange={(event) => onChange(Number(event.target.value))}
      className="tabular w-32 rounded border border-[var(--color-border-strong)] px-2 py-1 text-sm"
    />
  );
}
