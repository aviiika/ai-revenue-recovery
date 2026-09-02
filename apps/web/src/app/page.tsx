"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, type OverviewMetrics } from "@/lib/api";
import {
  EmptyState,
  ErrorState,
  LoadingRows,
  MetricCard,
  MoneyAmount,
  SyntheticBanner,
} from "@/components/domain";

/** Paise → rupees, for chart axes only. Never for a displayed total. */
const toRupees = (paise: number) => Math.round(paise / 100);

const CHART_COLORS = {
  atRisk: "#9a3412",
  recovered: "#14653c",
  neutral: "#1e429f",
};

export default function OverviewPage() {
  const queryClient = useQueryClient();

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["overview"],
    queryFn: api.overview,
  });

  const seed = useMutation({
    mutationFn: () => api.seed(120, true),
    onSuccess: () => queryClient.invalidateQueries(),
  });

  const runAgent = useMutation({
    mutationFn: () => api.evaluateBatch(200),
    onSuccess: () => queryClient.invalidateQueries(),
  });

  if (isLoading) {
    return (
      <div className="space-y-4">
        <PageHeading />
        <LoadingRows rows={6} />
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-4">
        <PageHeading />
        <ErrorState error={error as Error} onRetry={() => refetch()} />
      </div>
    );
  }

  if (!data || data.kpis.total_cases === 0) {
    return (
      <div className="space-y-4">
        <PageHeading />
        <EmptyState
          title="No recovery cases yet"
          description="Seed a deterministic batch of synthetic cases to see the agent work. The same seed always produces the same cases."
          action={
            <button
              type="button"
              onClick={() => seed.mutate()}
              disabled={seed.isPending}
              className="rounded border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-3 py-1.5 text-sm font-medium hover:bg-[var(--color-surface-sunken)] disabled:opacity-50"
            >
              {seed.isPending ? "Seeding…" : "Seed 120 synthetic cases"}
            </button>
          }
        />
      </div>
    );
  }

  const { kpis } = data;

  return (
    <div className="space-y-5">
      <PageHeading>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => runAgent.mutate()}
            disabled={runAgent.isPending}
            className="rounded border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-3 py-1.5 text-xs font-medium hover:bg-[var(--color-surface-sunken)] disabled:opacity-50"
          >
            {runAgent.isPending ? "Running…" : "Run agent on pending cases"}
          </button>
          <button
            type="button"
            onClick={() => seed.mutate()}
            disabled={seed.isPending}
            className="rounded border border-[var(--color-border)] px-3 py-1.5 text-xs text-[var(--color-ink-secondary)] hover:bg-[var(--color-surface-sunken)] disabled:opacity-50"
          >
            {seed.isPending ? "Reseeding…" : "Reseed"}
          </button>
        </div>
      </PageHeading>

      {data.synthetic ? <SyntheticBanner /> : null}

      {runAgent.data ? (
        <div className="rounded-md border border-[var(--color-progress-border)] bg-[var(--color-progress-surface)] px-3 py-2 text-xs text-[var(--color-ink-secondary)]">
          Last run evaluated {runAgent.data.evaluated} cases —{" "}
          {runAgent.data.action_selected} actioned, {runAgent.data.escalated}{" "}
          escalated, {runAgent.data.stopped} stopped.
        </div>
      ) : null}

      {/* KPI row — spec section 16 */}
      <section
        aria-label="Key metrics"
        className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
      >
        <MetricCard
          label="Revenue at risk"
          value={<MoneyAmount value={kpis.revenue_at_risk} size="xl" />}
          sublabel={`${kpis.total_cases} cases`}
          emphasis
        />
        <MetricCard
          label="Revenue recovered"
          value={<MoneyAmount value={kpis.revenue_recovered} size="xl" />}
          sublabel={`${kpis.cases_recovered} cases recovered`}
          emphasis
        />
        <MetricCard
          label="Recovery rate"
          value={
            <span className="tabular text-3xl font-semibold tracking-tight">
              {(kpis.recovery_rate_by_value * 100).toFixed(1)}%
            </span>
          }
          sublabel={`by value · ${(kpis.recovery_rate_by_count * 100).toFixed(1)}% by count`}
        />
        <MetricCard
          label="Automated resolution"
          value={
            <span className="tabular text-3xl font-semibold tracking-tight">
              {(kpis.automated_resolution_rate * 100).toFixed(1)}%
            </span>
          }
          sublabel={`${kpis.cases_escalated} escalated to a human`}
        />
      </section>

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard
          label="In progress"
          value={<Stat>{kpis.cases_in_progress}</Stat>}
        />
        <MetricCard
          label="Escalated"
          value={<Stat>{kpis.cases_escalated}</Stat>}
          sublabel="awaiting human review"
        />
        <MetricCard
          label="Stopped"
          value={<Stat>{kpis.cases_stopped}</Stat>}
          sublabel="by policy guardrails"
        />
        <MetricCard
          label="Est. intervention cost"
          value={
            <MoneyAmount value={kpis.estimated_intervention_cost} size="lg" />
          }
          sublabel="estimated, not yet executed"
        />
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel
          title="Recovery funnel"
          caption="Cases reaching each stage of the agent loop"
        >
          <ChartFrame>
            <BarChart data={data.funnel} layout="vertical" margin={{ left: 8 }}>
              <CartesianGrid horizontal={false} stroke="#eef1f5" />
              <XAxis type="number" tick={{ fontSize: 11 }} />
              <YAxis
                type="category"
                dataKey="stage"
                width={100}
                tick={{ fontSize: 11 }}
              />
              <Tooltip
                formatter={(value) => [`${Number(value ?? 0)} cases`, "Count"]}
              />
              <Bar dataKey="count" fill={CHART_COLORS.neutral} radius={2} />
            </BarChart>
          </ChartFrame>
        </Panel>

        <Panel
          title="Revenue at risk by failure reason"
          caption="Where the money is being lost, INR"
        >
          <ChartFrame>
            <BarChart
              data={data.by_failure_reason.map((row) => ({
                ...row,
                label: row.failure_category.replace(/_/g, " ").toLowerCase(),
                at_risk: toRupees(row.at_risk_paise),
              }))}
              layout="vertical"
              margin={{ left: 8 }}
            >
              <CartesianGrid horizontal={false} stroke="#eef1f5" />
              <XAxis type="number" tick={{ fontSize: 11 }} />
              <YAxis
                type="category"
                dataKey="label"
                width={130}
                tick={{ fontSize: 10 }}
              />
              <Tooltip
                formatter={(value) => [
                  `INR ${Number(value ?? 0).toLocaleString("en-IN")}`,
                  "At risk",
                ]}
              />
              <Bar dataKey="at_risk" radius={2}>
                {data.by_failure_reason.map((_, index) => (
                  <Cell key={index} fill={CHART_COLORS.atRisk} />
                ))}
              </Bar>
            </BarChart>
          </ChartFrame>
        </Panel>

        <Panel
          title="Detected vs recovered over time"
          caption="Daily totals, INR"
        >
          <ChartFrame>
            <LineChart
              data={data.over_time.map((row) => ({
                date: row.date.slice(5),
                "At risk": toRupees(row.at_risk_paise),
                Recovered: toRupees(row.recovered_paise),
              }))}
            >
              <CartesianGrid stroke="#eef1f5" />
              <XAxis dataKey="date" tick={{ fontSize: 10 }} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip
                formatter={(value) =>
                  `INR ${Number(value ?? 0).toLocaleString("en-IN")}`
                }
              />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Line
                type="monotone"
                dataKey="At risk"
                stroke={CHART_COLORS.atRisk}
                dot={false}
                strokeWidth={2}
              />
              <Line
                type="monotone"
                dataKey="Recovered"
                stroke={CHART_COLORS.recovered}
                dot={false}
                strokeWidth={2}
              />
            </LineChart>
          </ChartFrame>
        </Panel>

        <Panel title="Cases by state" caption="Current position in the loop">
          <StateTable data={data} />
        </Panel>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function PageHeading({ children }: { children?: React.ReactNode }) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">Overview</h1>
        <p className="text-xs text-[var(--color-ink-secondary)]">
          Revenue at risk, what the agent did about it, and how much was
          recovered.
        </p>
      </div>
      {children}
    </header>
  );
}

function Stat({ children }: { children: React.ReactNode }) {
  return (
    <span className="tabular text-2xl font-semibold tracking-tight">
      {children}
    </span>
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

function ChartFrame({ children }: { children: React.ReactElement }) {
  return (
    <div className="h-64 w-full">
      <ResponsiveContainer width="100%" height="100%">
        {children}
      </ResponsiveContainer>
    </div>
  );
}

function StateTable({ data }: { data: OverviewMetrics }) {
  const rows = [...data.by_state].sort((a, b) => b.count - a.count);
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-[var(--color-border)] text-left text-2xs uppercase tracking-wide text-[var(--color-ink-muted)]">
            <th className="py-1.5 font-medium">State</th>
            <th className="py-1.5 text-right font-medium">Cases</th>
            <th className="py-1.5 text-right font-medium">At risk</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.state}
              className="border-b border-[var(--color-border)] last:border-0"
            >
              <td className="py-1.5">
                <Link
                  href={`/cases?state=${row.state}`}
                  className="text-[var(--color-accent)] hover:underline"
                >
                  {row.state.replace(/_/g, " ").toLowerCase()}
                </Link>
              </td>
              <td className="tabular py-1.5 text-right">{row.count}</td>
              <td className="tabular py-1.5 text-right">
                {`INR ${toRupees(row.at_risk_paise).toLocaleString("en-IN")}`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
