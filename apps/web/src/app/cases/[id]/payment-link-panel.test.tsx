/**
 * What the operator is allowed to be told about a payment link.
 *
 * These are rendering tests rather than integration tests on purpose: the
 * money, the idempotency and the recovery decision all live in the backend and
 * are tested there. What can only go wrong *here* is the display telling the
 * operator something the backend never said — a link that does not exist, a
 * simulated run dressed up as Razorpay, or a created link read as money
 * recovered.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PaymentLinkPanel } from "./case-detail-view";
import type { CaseDetail, InterventionOut } from "@/lib/api";

const AMOUNT = 10476477;

function money(paise: number) {
  return {
    paise,
    formatted: `INR ${(paise / 100).toLocaleString("en-IN")}`,
    currency: "INR",
  };
}

function makeCase(overrides: Partial<CaseDetail> = {}): CaseDetail {
  return {
    id: "905c7cc4-676c-41a7-b2d3-c14032306bc4",
    source_external_id: "syn_20260902_000166",
    source_type: "SUBSCRIPTION",
    current_state: "OBSERVING",
    failure_category: "INSUFFICIENT_FUNDS",
    failure_reason_code: "BAD_REQUEST_ERROR",
    recoverability: "ACTIONABLE",
    recoverability_score: 0.5853982300884956,
    priority_score: 6000000,
    model_version: "recovery-clf-v1",
    amount_at_risk: money(AMOUNT),
    recovered_amount: money(0),
    attempt_count: 1,
    do_not_contact: false,
    is_synthetic: true,
    detected_at: "2026-09-02T10:00:00Z",
    recovered_at: null,
    audit_trail: [],
    allowed_transitions: [],
    interventions: [],
    selected_strategy: "CREATE_PAYMENT_LINK",
    expected_net_paise: 6000000,
    ...overrides,
  } as CaseDetail;
}

function makeIntervention(
  result: Record<string, unknown>,
  overrides: Partial<InterventionOut> = {},
): InterventionOut {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    strategy: "CREATE_PAYMENT_LINK",
    status: "EXECUTED",
    channel: "RAZORPAY_TEST",
    attempt_number: 1,
    idempotency_key: "case:905c7cc4:attempt:1",
    estimated_cost_paise: 500,
    planned_at: "2026-09-05T05:20:00Z",
    executed_at: "2026-09-05T05:20:01Z",
    result,
    ...overrides,
  } as InterventionOut;
}

const LIVE_RESULT = {
  provider: "razorpay",
  mode: "test",
  simulated: false,
  payment_link_id: "plink_TEST123abc",
  short_url: "https://rzp.io/i/TESTabc123",
  status: "created",
};

const SIMULATED_RESULT = {
  provider: "simulated",
  simulated: true,
  strategy: "CREATE_PAYMENT_LINK",
};

describe("a real test-mode link", () => {
  it("shows the provider's URL and labels the run as Razorpay test mode", () => {
    render(
      <PaymentLinkPanel
        case={makeCase()}
        intervention={makeIntervention(LIVE_RESULT)}
      />,
    );

    const link = screen.getByRole("link", { name: /open payment link/i });
    expect(link).toHaveProperty("href", LIVE_RESULT.short_url);
    expect(screen.getByText("RAZORPAY TEST MODE")).toBeDefined();
    expect(screen.getByText(LIVE_RESULT.payment_link_id)).toBeDefined();
  });

  it("does not report the money as recovered just because a link exists", () => {
    render(
      <PaymentLinkPanel
        case={makeCase()}
        intervention={makeIntervention(LIVE_RESULT)}
      />,
    );

    expect(screen.getByText("Awaiting payment")).toBeDefined();
    expect(screen.queryByText(/revenue recovered/i)).toBeNull();
    expect(screen.getByText(/creating a link is not a recovery/i)).toBeDefined();
  });
});

describe("a simulated run", () => {
  it("never renders a link, and says why", () => {
    render(
      <PaymentLinkPanel
        case={makeCase()}
        intervention={makeIntervention(SIMULATED_RESULT)}
      />,
    );

    expect(screen.queryByRole("link", { name: /open payment link/i })).toBeNull();
    expect(screen.getByText("SIMULATED")).toBeDefined();
    expect(screen.getByText(/no live link/i)).toBeDefined();
  });

  it("treats a result with no simulated flag as simulated, not as live", () => {
    // Fail closed: an unrecognised provider result must not be presented as a
    // real Razorpay execution.
    render(
      <PaymentLinkPanel
        case={makeCase()}
        intervention={makeIntervention({ provider: "unknown" })}
      />,
    );

    expect(screen.getByText("SIMULATED")).toBeDefined();
    expect(screen.queryByRole("link", { name: /open payment link/i })).toBeNull();
  });
});

describe("after the payment webhook settles the case", () => {
  it("reports PAID and the recovered amount", () => {
    render(
      <PaymentLinkPanel
        case={makeCase({
          current_state: "RECOVERED",
          recovered_amount: money(AMOUNT),
          recovered_at: "2026-09-05T06:00:00Z",
        })}
        intervention={makeIntervention(LIVE_RESULT)}
      />,
    );

    expect(screen.getByText("PAID")).toBeDefined();
    expect(screen.getByText(/revenue recovered/i)).toBeDefined();
    expect(screen.queryByText(/creating a link is not a recovery/i)).toBeNull();
  });
});

describe("a provider failure", () => {
  it("says the creation failed and that the case can be retried", () => {
    render(
      <PaymentLinkPanel
        case={makeCase({ current_state: "ACTION_SELECTED" })}
        intervention={makeIntervention(
          { error: "Razorpay POST /payment_links returned 500" },
          { status: "FAILED", executed_at: null },
        )}
      />,
    );

    expect(screen.getByText(/payment link creation failed/i)).toBeDefined();
    expect(screen.getByText(/returned 500/)).toBeDefined();
    expect(screen.getByText(/can be retried/i)).toBeDefined();
    expect(screen.queryByRole("link", { name: /open payment link/i })).toBeNull();
  });
});

describe("a non-payment-link action", () => {
  it("is not dressed up as a Razorpay link", () => {
    render(
      <PaymentLinkPanel
        case={makeCase({ selected_strategy: "SEND_REMINDER_SIMULATED" })}
        intervention={makeIntervention(
          { simulated: true, would_send: "reminder email" },
          { strategy: "SEND_REMINDER_SIMULATED", channel: "SIMULATED" },
        )}
      />,
    );

    expect(screen.queryByText("Razorpay Payment Link")).toBeNull();
    expect(screen.getByText("Executed action")).toBeDefined();
  });
});
