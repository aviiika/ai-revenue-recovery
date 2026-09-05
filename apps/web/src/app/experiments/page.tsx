"use client";

import { useQuery } from "@tanstack/react-query";
import { api, type ArmResult, type PolicyComparison } from "@/lib/api";
import {
  EmptyState,
  ErrorState,
  LoadingRows,
  MetricCard,
  SyntheticBanner,
} from "@/components/domain";

/**
 * The causal evidence page.
 *
 * Every other page reports what the agent *did*. This one answers the only
 * question a sceptic actually asks: would the money have come back anyway?
 * The randomised holdout is what makes that answerable, so the arms, their
 * sizes and the caveat are shown together — a lift figure without the sample
 * behind it is not evidence.
 *
 * All arithmetic is server-side. This page renders figures; it never computes
 * a rate or a difference itself.
 */
export default function ExperimentsPage() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["experiments"],
    queryFn: api.experiments,
  });

  if (isLoading) return <LoadingRows rows={8} />;
  if (error) {
    return <ErrorState error={error as Error} onRetry={() => refetch()} />;
  }
  if (!data) return null;

  if (!data.available || !data.incremental || !data.agent_vs_baseline) {
    return (
      <div className="space-y-4">
        <Heading />
        <EmptyState
          title="No experiment data yet"
          description={
            data.message ??
            "Seed the demo population and run a batch; the holdout is assigned before the first action."
          }
        />
      </div>
    );
  }

  const { incremental, agent_vs_baseline: versus, policies } = data;
  const { treatment, holdout } = incremental;

  // A holdout this small cannot carry a conclusion. The backend says so in its
  // caveat; the page repeats it as a visible state rather than burying it.
  const underpowered = holdout.cases < 20;
  // Heavy-tailed amounts can push the value-weighted estimator the other way.
  // Saying so beats letting a reader spot the contradiction unaided.
  const signsDisagree =
    Math.sign(incremental.incremental_rate_points) !==
      Math.sign(incremental.incremental_rate_points_by_value) &&
    incremental.incremental_rate_points_by_value !== 0;

  return (
    <div className="space-y-5">
      <Heading />
      <SyntheticBanner />

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <MetricCard
          label="Incremental recovery rate"
          value={`${incremental.incremental_rate_points > 0 ? "+" : ""}${incremental.incremental_rate_points} pp`}
          sublabel="share of cases recovered, treated minus holdout"
          emphasis
        />
        <MetricCard
          label="Incremental value"
          value={formatPaise(incremental.incremental_value_paise)}
          sublabel="that rate applied to treated value at risk"
        />
        <MetricCard
          label="Net of intervention spend"
          value={formatPaise(incremental.net_incremental_value_paise)}
          sublabel={`less ${formatPaise(treatment.spend_paise)} spent acting`}
        />
        <MetricCard
          label="Same difference, by value"
          value={`${incremental.incremental_rate_points_by_value > 0 ? "+" : ""}${incremental.incremental_rate_points_by_value} pp`}
          sublabel="heavy-tailed — read with the caveat below"
        />
        <MetricCard
          label="Holdout share"
          value={`${(incremental.holdout_share * 100).toFixed(1)}%`}
          sublabel={`${holdout.cases} of ${treatment.cases + holdout.cases} actionable cases`}
        />
      </section>

      {signsDisagree ? (
        <p className="rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface-sunken)] px-3 py-2 text-xs text-[var(--color-ink-secondary)]">
          The two estimators disagree in sign. The count-based figure leads
          because it measures the effect on whether a case recovers at all; the
          value-based one is dominated by however a few large cases happened to
          settle. Treat the value figure as unreliable at this sample size
          rather than as a contradiction.
        </p>
      ) : null}

      {underpowered ? (
        <p className="rounded-md border border-[var(--color-escalated-border)] bg-[var(--color-escalated-surface)] px-3 py-2 text-xs text-[var(--color-escalated)]">
          Holdout arm has only {holdout.cases} cases — too few to read this
          difference as meaningful. Seed a larger batch before quoting it.
        </p>
      ) : null}

      <section className="space-y-2">
        <h2 className="text-sm font-semibold">Randomised arms</h2>
        <p className="text-xs text-[var(--color-ink-secondary)]">
          Both arms are restricted to cases the agent decided to act on, so the
          comparison is like-for-like. In the treated arm it acted; in the
          holdout it was deliberately withheld.
        </p>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[40rem] border-collapse text-sm">
            <thead>
              <tr className="border-b border-[var(--color-border-strong)] text-left">
                <Th>Arm</Th>
                <Th align="right">Cases</Th>
                <Th align="right">Recovered</Th>
                <Th align="right">Rate (count)</Th>
                <Th align="right">Rate (value)</Th>
                <Th align="right">Actions executed</Th>
                <Th align="right">Spend</Th>
              </tr>
            </thead>
            <tbody>
              <ArmRow arm={treatment} label="Treatment" />
              <ArmRow arm={holdout} label="Holdout (withheld)" />
            </tbody>
          </table>
        </div>
        <p className="text-2xs text-[var(--color-ink-muted)]">
          The holdout must show zero executed actions. A contacted holdout would
          invalidate the entire measurement.
        </p>
      </section>

      <section className="space-y-2">
        <h2 className="text-sm font-semibold">Agent versus a contact-everyone baseline</h2>
        <p className="text-xs text-[var(--color-ink-secondary)]">
          Scored over the same population. A policy that recovers slightly more
          by contacting everyone twice is not obviously better, so actions are
          counted alongside money.
        </p>
        <div className="grid gap-3 sm:grid-cols-2">
          <MetricCard
            label="Expected net advantage"
            value={formatPaise(versus.expected_net_delta_paise)}
            sublabel={`${versus.net_uplift_pct > 0 ? "+" : ""}${versus.net_uplift_pct}% versus baseline`}
            emphasis
          />
          {/* `fewer_actions_pct` is already positive when the agent acts less,
              so it is rendered as-is; negating it here would invert the claim. */}
          <MetricCard
            label="Restraint"
            value={`${Math.abs(versus.fewer_actions_pct)}% ${versus.fewer_actions_pct >= 0 ? "fewer" : "more"} actions`}
            sublabel={`${Math.abs(versus.action_delta)} ${versus.action_delta < 0 ? "fewer" : "more"} interventions than baseline`}
          />
        </div>
        {policies ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[40rem] border-collapse text-sm">
              <thead>
                <tr className="border-b border-[var(--color-border-strong)] text-left">
                  <Th>Policy</Th>
                  <Th align="right">Actions</Th>
                  <Th align="right">Actions / case</Th>
                  <Th align="right">Expected recovered</Th>
                  <Th align="right">Spend</Th>
                  <Th align="right">Expected net</Th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(policies).map(([name, policy]) => (
                  <PolicyRow key={name} name={name} policy={policy} />
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      <section className="space-y-2 rounded-md border border-[var(--color-border)] bg-[var(--color-surface-sunken)] p-4">
        <h2 className="text-sm font-semibold">How to read these numbers</h2>
        <p className="text-xs text-[var(--color-ink-secondary)]">
          {incremental.caveat}
        </p>
        {data.spontaneous_recovery_note ? (
          <p className="text-xs text-[var(--color-ink-secondary)]">
            {data.spontaneous_recovery_note}
          </p>
        ) : null}
        {data.assumed_spontaneous_rate ? (
          <dl className="mt-1 flex flex-wrap gap-x-6 gap-y-1">
            {Object.entries(data.assumed_spontaneous_rate).map(([band, rate]) => (
              <div key={band} className="flex gap-1.5 text-2xs">
                <dt className="text-[var(--color-ink-muted)]">
                  {band.replace(/_/g, " ").toLowerCase()}
                </dt>
                <dd className="tabular font-medium">{rate}</dd>
              </div>
            ))}
          </dl>
        ) : null}
      </section>
    </div>
  );
}

function Heading() {
  return (
    <header>
      <h1 className="text-lg font-semibold tracking-tight">
        Incremental impact
      </h1>
      <p className="mt-1 text-xs text-[var(--color-ink-secondary)]">
        What the agent caused, measured against a randomised holdout — not what
        it merely coincided with.
      </p>
    </header>
  );
}

function ArmRow({ arm, label }: { arm: ArmResult; label: string }) {
  return (
    <tr className="border-b border-[var(--color-border)]">
      <td className="py-2 pr-3 font-medium">{label}</td>
      <Td>{arm.cases}</Td>
      <Td>{arm.recovered}</Td>
      <Td>{(arm.recovery_rate_by_count * 100).toFixed(1)}%</Td>
      <Td>{(arm.recovery_rate_by_value * 100).toFixed(1)}%</Td>
      <Td>{arm.executed_actions}</Td>
      <Td>{formatPaise(arm.spend_paise)}</Td>
    </tr>
  );
}

function PolicyRow({
  name,
  policy,
}: {
  name: string;
  policy: PolicyComparison;
}) {
  return (
    <tr className="border-b border-[var(--color-border)]">
      <td className="py-2 pr-3 font-medium capitalize">{name}</td>
      <Td>{policy.actions}</Td>
      <Td>{policy.actions_per_case.toFixed(2)}</Td>
      <Td>{formatPaise(policy.expected_recovered_paise)}</Td>
      <Td>{formatPaise(policy.spend_paise)}</Td>
      <Td>{formatPaise(policy.expected_net_paise)}</Td>
    </tr>
  );
}

function Th({
  children,
  align = "left",
}: {
  children: React.ReactNode;
  align?: "left" | "right";
}) {
  return (
    <th
      className={`py-1.5 pr-3 text-2xs font-medium uppercase tracking-wide text-[var(--color-ink-muted)] ${
        align === "right" ? "text-right" : ""
      }`}
    >
      {children}
    </th>
  );
}

function Td({ children }: { children: React.ReactNode }) {
  return <td className="tabular py-2 pr-3 text-right">{children}</td>;
}

/**
 * Paise to rupees for display only.
 *
 * The backend sends pre-formatted strings wherever a figure is a `Money`; the
 * experiment report sends raw paise because several of its values are signed
 * differences. Integer division keeps this exact — no float ever touches a
 * rupee figure.
 */
function formatPaise(paise: number): string {
  const negative = paise < 0;
  const abs = Math.abs(paise);
  const rupees = Math.trunc(abs / 100);
  const remainder = String(abs % 100).padStart(2, "0");
  return `${negative ? "−" : ""}INR ${rupees.toLocaleString("en-IN")}.${remainder}`;
}
