"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api, type CaseState } from "@/lib/api";
import {
  ConfidenceBadge,
  EmptyState,
  ErrorState,
  LoadingRows,
  MoneyAmount,
  RecoverabilityTag,
  RecoveryStatusBadge,
  SyntheticBanner,
} from "@/components/domain";

const STATES: CaseState[] = [
  "NEW",
  "DIAGNOSED",
  "SCORED",
  "ACTION_SELECTED",
  "OBSERVING",
  "RETRY_ELIGIBLE",
  "ESCALATED",
  "RECOVERED",
  "EXHAUSTED",
  "STOPPED",
];

const PAGE_SIZE = 50;

export default function CasesPage() {
  const [state, setState] = useState<CaseState | "">("");
  const [minAmount, setMinAmount] = useState("");
  const [offset, setOffset] = useState(0);

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ["cases", state, minAmount, offset],
    queryFn: () =>
      api.listCases({
        state: state ? [state] : undefined,
        min_amount_paise: minAmount ? Number(minAmount) * 100 : undefined,
        limit: PAGE_SIZE,
        offset,
      }),
  });

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-lg font-semibold tracking-tight">Recovery cases</h1>
        <p className="text-xs text-[var(--color-ink-secondary)]">
          Every unit of revenue at risk, its diagnosis, and where the agent has
          taken it.
        </p>
      </header>

      {data?.contains_synthetic ? <SyntheticBanner /> : null}

      <div className="flex flex-wrap items-end gap-3 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
        <label className="flex flex-col gap-1">
          <span className="text-2xs font-medium uppercase tracking-wide text-[var(--color-ink-muted)]">
            State
          </span>
          <select
            value={state}
            onChange={(event) => {
              setState(event.target.value as CaseState | "");
              setOffset(0);
            }}
            className="rounded border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-sm"
          >
            <option value="">All states</option>
            {STATES.map((value) => (
              <option key={value} value={value}>
                {value.replace(/_/g, " ").toLowerCase()}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-2xs font-medium uppercase tracking-wide text-[var(--color-ink-muted)]">
            Min amount (INR)
          </span>
          <input
            type="number"
            min={0}
            value={minAmount}
            onChange={(event) => {
              setMinAmount(event.target.value);
              setOffset(0);
            }}
            placeholder="0"
            className="w-32 rounded border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-sm"
          />
        </label>

        {(state || minAmount) && (
          <button
            type="button"
            onClick={() => {
              setState("");
              setMinAmount("");
              setOffset(0);
            }}
            className="rounded border border-[var(--color-border)] px-2.5 py-1 text-xs text-[var(--color-ink-secondary)] hover:bg-[var(--color-surface-sunken)]"
          >
            Clear filters
          </button>
        )}

        <div className="ml-auto text-2xs text-[var(--color-ink-muted)]">
          {data ? `${data.total} cases` : null}
          {isFetching ? " · refreshing" : null}
        </div>
      </div>

      {isLoading ? (
        <LoadingRows rows={8} />
      ) : error ? (
        <ErrorState error={error as Error} onRetry={() => refetch()} />
      ) : !data || data.items.length === 0 ? (
        <EmptyState
          title="No cases match these filters"
          description="Try clearing the filters, or seed a batch of synthetic cases from the Overview page."
        />
      ) : (
        <>
          <div className="overflow-x-auto rounded-md border border-[var(--color-border)] bg-[var(--color-surface)]">
            <table className="w-full min-w-[64rem] text-sm">
              <caption className="sr-only">
                Recovery cases, sorted by detection time, most recent first
              </caption>
              <thead className="bg-[var(--color-surface-sunken)]">
                <tr className="text-left text-2xs uppercase tracking-wide text-[var(--color-ink-muted)]">
                  <th scope="col" className="px-3 py-2 font-medium">
                    Source
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    At risk
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Failure reason
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Recovery probability
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    State
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    Attempts
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    Recovered
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((item) => (
                  <tr
                    key={item.id}
                    className="border-t border-[var(--color-border)] hover:bg-[var(--color-surface-sunken)]"
                  >
                    <td className="px-3 py-2">
                      <Link
                        href={`/cases/${item.id}`}
                        className="font-medium text-[var(--color-accent)] hover:underline"
                      >
                        {item.source_external_id}
                      </Link>
                      <div className="text-2xs text-[var(--color-ink-muted)]">
                        {item.source_type.toLowerCase()}
                        {item.do_not_contact ? " · do not contact" : ""}
                      </div>
                    </td>
                    <td className="px-3 py-2 text-right">
                      <MoneyAmount value={item.amount_at_risk} />
                    </td>
                    <td className="px-3 py-2">
                      <div className="text-xs">
                        {item.failure_category.replace(/_/g, " ").toLowerCase()}
                      </div>
                      <RecoverabilityTag value={item.recoverability} />
                    </td>
                    <td className="px-3 py-2">
                      <ConfidenceBadge value={item.recoverability_score} />
                    </td>
                    <td className="px-3 py-2">
                      <RecoveryStatusBadge state={item.current_state} />
                    </td>
                    <td className="tabular px-3 py-2 text-right">
                      {item.attempt_count}
                    </td>
                    <td className="px-3 py-2 text-right">
                      {item.recovered_amount.paise > 0 ? (
                        <MoneyAmount value={item.recovered_amount} />
                      ) : (
                        <span className="text-[var(--color-ink-muted)]">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="flex items-center justify-between text-xs">
            <span className="text-[var(--color-ink-secondary)]">
              Showing {offset + 1}–{Math.min(offset + PAGE_SIZE, data.total)} of{" "}
              {data.total}
            </span>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                disabled={offset === 0}
                className="rounded border border-[var(--color-border-strong)] px-2.5 py-1 disabled:opacity-40"
              >
                Previous
              </button>
              <button
                type="button"
                onClick={() => setOffset(offset + PAGE_SIZE)}
                disabled={offset + PAGE_SIZE >= data.total}
                className="rounded border border-[var(--color-border-strong)] px-2.5 py-1 disabled:opacity-40"
              >
                Next
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
