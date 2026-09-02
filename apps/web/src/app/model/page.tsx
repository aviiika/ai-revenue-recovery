"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import {
  EmptyState,
  ErrorState,
  LoadingRows,
  MetricCard,
  SyntheticBanner,
} from "@/components/domain";

export default function ModelPage() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["model-metrics"],
    queryFn: api.modelMetrics,
  });

  if (isLoading) return <LoadingRows rows={8} />;
  if (error) {
    return <ErrorState error={error as Error} onRetry={() => refetch()} />;
  }
  if (!data) return null;

  if (!data.trained) {
    return (
      <div className="space-y-4">
        <Heading />
        <EmptyState
          title="No trained model"
          description={
            data.message ??
            "The system is running on the deterministic baseline scorer."
          }
        />
      </div>
    );
  }

  const selected = data.selected_model ?? "";
  // The per-model block for the selected candidate; absent only if the
  // artifact predates this report shape.
  const detail = data.reports?.[selected];

  return (
    <div className="space-y-5">
      <Heading />
      <SyntheticBanner />

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard
          label="Selected model"
          value={
            <span className="text-lg font-semibold">
              {selected.replace(/_/g, " ")}
            </span>
          }
          sublabel={`PR-AUC margin vs challenger: ${data.selection_margin_pr_auc}`}
          emphasis
        />
        <MetricCard
          label="PR-AUC"
          value={<Stat>{detail?.classification.pr_auc.toFixed(4)}</Stat>}
          sublabel="held-out test set"
          emphasis
        />
        <MetricCard
          label="Brier score"
          value={<Stat>{detail?.classification.brier.toFixed(4)}</Stat>}
          sublabel="lower is better"
        />
        <MetricCard
          label="Calibration error"
          value={<Stat>{data.expected_calibration_error}</Stat>}
          sublabel="expected calibration error"
        />
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Classification metrics" caption="Held-out test set">
          <MetricTable
            rows={Object.entries(detail?.classification ?? {}).map(
              ([key, value]) => [
                key.replace(/_/g, " "),
                typeof value === "number" ? value.toFixed(4) : String(value),
              ],
            )}
          />
        </Panel>

        <Panel
          title="Calibration"
          caption="A predicted 0.8 should mean roughly 80% observed recovery"
        >
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-[var(--color-border)] text-left text-2xs uppercase tracking-wide text-[var(--color-ink-muted)]">
                <th className="py-1.5 font-medium">Predicted</th>
                <th className="py-1.5 text-right font-medium">Observed</th>
                <th className="py-1.5 text-right font-medium">n</th>
              </tr>
            </thead>
            <tbody>
              {(detail?.calibration ?? []).map((bin, index) => (
                <tr
                  key={index}
                  className="border-b border-[var(--color-border)] last:border-0"
                >
                  <td className="tabular py-1.5">
                    {bin.mean_predicted.toFixed(3)}
                  </td>
                  <td className="tabular py-1.5 text-right">
                    {bin.observed_rate.toFixed(3)}
                  </td>
                  <td className="tabular py-1.5 text-right">{bin.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        <Panel
          title="Feature influence"
          caption="Logistic coefficients — correlational, not causal"
        >
          <MetricTable
            rows={Object.entries(detail?.feature_influence ?? {}).map(
              ([key, value]) => [key.replace(/^(numeric|categorical)__/, ""), String(value)],
            )}
          />
        </Panel>

        <Panel title="Decision threshold" caption={data.threshold_note}>
          <p className="tabular text-2xl font-semibold">{data.threshold}</p>
          <p className="mt-2 text-xs text-[var(--color-ink-secondary)]">
            Chosen to maximise expected net recovery on the validation split,
            not left at 0.5. The policy engine applies an independent confidence
            floor on top of this.
          </p>
          {data.dataset ? (
            <div className="mt-3">
              <MetricTable
                rows={Object.entries(data.dataset).map(([key, value]) => [
                  key.replace(/_/g, " "),
                  String(value),
                ])}
              />
            </div>
          ) : null}
        </Panel>
      </div>
    </div>
  );
}

function Heading() {
  return (
    <header>
      <h1 className="text-lg font-semibold tracking-tight">Model metrics</h1>
      <p className="text-xs text-[var(--color-ink-secondary)]">
        How the recovery classifier performs, and how much to trust it.
      </p>
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

function MetricTable({ rows }: { rows: [string, string][] }) {
  return (
    <table className="w-full text-sm">
      <tbody>
        {rows.map(([label, value]) => (
          <tr
            key={label}
            className="border-b border-[var(--color-border)] last:border-0"
          >
            <td className="py-1.5 text-[var(--color-ink-secondary)]">
              {label}
            </td>
            <td className="tabular py-1.5 text-right font-medium">{value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
