/**
 * Typed API client.
 *
 * The backend is the single source of domain truth. These types mirror the
 * FastAPI response schemas; the frontend does not re-derive state names,
 * recompute money, or re-implement policy logic (spec QUALITY rule: "do not
 * duplicate domain logic between frontend and backend").
 *
 * Money arrives as `{ paise, formatted }`. The frontend renders `formatted`
 * and never does arithmetic on `paise` — every figure a user sees was computed
 * server-side in integer paise.
 */

const BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
      cache: "no-store",
    });
  } catch (cause) {
    // Network-level failure: the API is almost certainly not running. Say so
    // plainly rather than surfacing a generic "failed to fetch".
    throw new ApiError(
      `Cannot reach the API at ${BASE_URL}. Is the backend running?`,
      0,
      cause,
    );
  }

  if (!response.ok) {
    let detail: unknown;
    try {
      detail = await response.json();
    } catch {
      detail = await response.text().catch(() => undefined);
    }
    throw new ApiError(
      `${init?.method ?? "GET"} ${path} failed (${response.status})`,
      response.status,
      detail,
    );
  }
  return (await response.json()) as T;
}

/* -------------------------------------------------------------------------- */
/* Domain types — mirror of the backend schemas                                */
/* -------------------------------------------------------------------------- */

export interface MoneyOut {
  paise: number;
  formatted: string;
}

export type CaseState =
  | "NEW"
  | "DIAGNOSED"
  | "SCORED"
  | "ACTION_SELECTED"
  | "ACTION_PENDING"
  | "ACTION_EXECUTED"
  | "OBSERVING"
  | "RECOVERED"
  | "RETRY_ELIGIBLE"
  | "ESCALATED"
  | "EXHAUSTED"
  | "STOPPED";

export type Recoverability =
  | "TRANSIENT"
  | "ACTIONABLE"
  | "STRUCTURAL"
  | "UNRECOVERABLE";

export interface CaseSummary {
  id: string;
  source_type: string;
  source_external_id: string;
  amount_at_risk: MoneyOut;
  recovered_amount: MoneyOut;
  currency: string;
  failure_category: string;
  failure_reason_code: string | null;
  recoverability: Recoverability | null;
  current_state: CaseState;
  recoverability_score: number | null;
  priority_score: number | null;
  model_version: string | null;
  attempt_count: number;
  detected_at: string;
  recovered_at: string | null;
  last_action_at: string | null;
  do_not_contact: boolean;
  is_synthetic: boolean;
  updated_at: string;
}

export interface AuditEventOut {
  sequence: number;
  actor_type: "SYSTEM" | "MODEL" | "HUMAN" | "WEBHOOK";
  actor_id: string | null;
  event_type: string;
  before_state: CaseState | null;
  after_state: CaseState | null;
  summary: string;
  payload: Record<string, unknown>;
  correlation_id: string | null;
  created_at: string;
}

export interface CaseDetail extends CaseSummary {
  audit_trail: AuditEventOut[];
  allowed_transitions: CaseState[];
}

export interface CaseListResponse {
  items: CaseSummary[];
  total: number;
  limit: number;
  offset: number;
  contains_synthetic: boolean;
}

export interface ScoredStrategyOut {
  strategy: string;
  probability: number;
  expected_gross: MoneyOut;
  cost: MoneyOut;
  expected_net_paise: number;
  model_version: string;
}

export interface PolicyDecisionOut {
  recommended_strategy: string;
  next_state: CaseState | null;
  deferred: boolean;
  requires_human: boolean;
  explanation: string;
  expected_net_paise: number;
  applied_rules: string[];
  decisive_rule: string | null;
  retry_after: string | null;
  allowed: ScoredStrategyOut[];
  blocked: { strategy: string; rule_id: string; reason: string }[];
}

export interface OverviewMetrics {
  synthetic: boolean;
  kpis: {
    total_cases: number;
    revenue_at_risk: MoneyOut;
    revenue_recovered: MoneyOut;
    estimated_intervention_cost: MoneyOut;
    net_recovery_paise: number;
    recovery_rate_by_count: number;
    recovery_rate_by_value: number;
    automated_resolution_rate: number;
    cases_recovered: number;
    cases_escalated: number;
    cases_stopped: number;
    cases_in_progress: number;
  };
  funnel: { stage: string; count: number; at_risk_paise: number }[];
  by_state: { state: CaseState; count: number; at_risk_paise: number }[];
  by_failure_reason: {
    failure_category: string;
    count: number;
    at_risk_paise: number;
    recovered_paise: number;
  }[];
  over_time: {
    date: string;
    count: number;
    at_risk_paise: number;
    recovered_paise: number;
  }[];
}

export interface ModelReport {
  trained: boolean;
  synthetic?: boolean;
  message?: string;
  selected_model?: string;
  threshold?: number;
  expected_calibration_error?: number;
  selection_margin_pr_auc?: number;
  threshold_note?: string;
  dataset?: Record<string, string | number>;
  reports?: Record<
    string,
    {
      classification: Record<string, number>;
      business: Record<string, number>;
      calibration: {
        mean_predicted: number;
        observed_rate: number;
        count: number;
      }[];
      feature_influence: Record<string, number>;
    }
  >;
}

export type ReviewStatus = "PENDING" | "APPROVED" | "OVERRIDDEN" | "REJECTED";

export interface ReviewOut {
  id: string;
  recovery_case_id: string;
  source_external_id: string;
  reason: string;
  status: ReviewStatus;
  proposed_strategy: string | null;
  chosen_strategy: string | null;
  explanation: string | null;
  amount_at_risk: MoneyOut;
  failure_category: string | null;
  recoverability_score: number | null;
  current_state: CaseState | null;
  attempt_count: number;
  reviewer: string | null;
  decision_notes: string | null;
  applied_rules: string[];
  created_at: string;
  resolved_at: string | null;
}

export interface ReviewListResponse {
  items: ReviewOut[];
  total: number;
  limit: number;
  offset: number;
  pending: number;
  value_awaiting_review: MoneyOut;
  by_reason: Record<string, number>;
}

export interface PolicyResponse {
  merchant_id: string;
  max_automated_attempts: number;
  cooldown_hours: number;
  min_auto_action_confidence: number;
  high_value_threshold_paise: number;
  min_expected_net_recovery_paise: number;
  backoff_base_hours: number;
  backoff_cap_hours: number;
  enabled_strategies: string[];
  available_strategies: string[];
}

export type PolicyUpdate = Partial<
  Omit<PolicyResponse, "merchant_id" | "available_strategies">
>;

export interface CaseFilters {
  state?: CaseState[];
  source_type?: string[];
  failure_category?: string[];
  min_amount_paise?: number;
  limit?: number;
  offset?: number;
}

/* -------------------------------------------------------------------------- */
/* Endpoints                                                                    */
/* -------------------------------------------------------------------------- */

function toQuery(filters: CaseFilters): string {
  const params = new URLSearchParams();
  filters.state?.forEach((s) => params.append("state", s));
  filters.source_type?.forEach((s) => params.append("source_type", s));
  filters.failure_category?.forEach((s) =>
    params.append("failure_category", s),
  );
  if (filters.min_amount_paise !== undefined) {
    params.set("min_amount_paise", String(filters.min_amount_paise));
  }
  params.set("limit", String(filters.limit ?? 50));
  params.set("offset", String(filters.offset ?? 0));
  return params.toString();
}

export const api = {
  health: () =>
    request<{ status: string; database: string; razorpay_mode: string }>(
      "/health",
    ),

  listCases: (filters: CaseFilters = {}) =>
    request<CaseListResponse>(`/api/v1/cases?${toQuery(filters)}`),

  getCase: (id: string) => request<CaseDetail>(`/api/v1/cases/${id}`),

  evaluateCase: (id: string) =>
    request<{
      case: CaseSummary;
      decision: PolicyDecisionOut;
      model_version: string;
    }>(`/api/v1/cases/${id}/evaluate`, { method: "POST" }),

  stopCase: (id: string, reason: string, reviewer?: string) =>
    request<CaseSummary>(`/api/v1/cases/${id}/stop`, {
      method: "POST",
      body: JSON.stringify({ reason, reviewer }),
    }),

  evaluateBatch: (limit = 200) =>
    request<{
      evaluated: number;
      action_selected: number;
      escalated: number;
      stopped: number;
      waiting: number;
      total_at_risk: MoneyOut;
      total_expected_net_paise: number;
      by_rule: Record<string, number>;
    }>("/api/v1/cases/evaluate-batch", {
      method: "POST",
      body: JSON.stringify({ limit }),
    }),

  overview: () => request<OverviewMetrics>("/api/v1/metrics/overview"),

  interventions: () =>
    request<{
      interventions: {
        strategy: string;
        selected: number;
        recovered: number;
        success_rate: number;
        at_risk_paise: number;
        recovered_paise: number;
        estimated_cost_paise: number;
      }[];
    }>("/api/v1/metrics/interventions"),

  modelMetrics: () => request<ModelReport>("/api/v1/metrics/models"),

  listReviews: (status: ReviewStatus = "PENDING") =>
    request<ReviewListResponse>(`/api/v1/reviews?status=${status}`),

  approveReview: (id: string, reviewer: string, notes: string) =>
    request<ReviewOut>(`/api/v1/reviews/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({ reviewer, notes }),
    }),

  overrideReview: (id: string, reviewer: string, notes: string, strategy: string) =>
    request<ReviewOut>(`/api/v1/reviews/${id}/override`, {
      method: "POST",
      body: JSON.stringify({ reviewer, notes, strategy }),
    }),

  rejectReview: (id: string, reviewer: string, notes: string) =>
    request<ReviewOut>(`/api/v1/reviews/${id}/reject`, {
      method: "POST",
      body: JSON.stringify({ reviewer, notes }),
    }),

  getPolicies: () => request<PolicyResponse>("/api/v1/policies"),

  updatePolicies: (update: PolicyUpdate) =>
    request<PolicyResponse>("/api/v1/policies", {
      method: "PUT",
      body: JSON.stringify(update),
    }),

  seed: (count = 120, reset = true) =>
    request<{
      cases_created: number;
      total_at_risk: MoneyOut;
      seed_used: number;
    }>("/api/v1/demo/seed", {
      method: "POST",
      body: JSON.stringify({ count, reset }),
    }),
};
