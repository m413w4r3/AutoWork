import { afterEach, describe, expect, it, vi } from "vitest";

import {
  acceptPlan,
  getPlan,
  listConsultations,
  listPlans,
  materializePlan,
  plansKeys,
  requestPlan,
} from "./plans";
import {
  bodyOf,
  errorResponse,
  headersOf,
  installFetch,
  jsonResponse,
  lastRequest,
} from "./testSupport";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("plan reads", () => {
  it("reads the plan list, one version and the consultation ledger", async () => {
    const recorder = installFetch(() => jsonResponse([]));

    await listPlans("inv-1");
    expect(lastRequest(recorder).url).toBe("/api/investigations/inv-1/plans");
    expect(lastRequest(recorder).init?.method).toBe("GET");

    await getPlan("inv-1", "plan-version-2");
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/plans/plan-version-2",
    );

    await listConsultations("inv-1");
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/consultations",
    );
  });
});

describe("plan mutations", () => {
  it("requests a plan with the working-state CAS and Idempotency-Key", async () => {
    const recorder = installFetch(() => jsonResponse({ advice_only: false }));

    await requestPlan(
      "inv-1",
      {
        expected_version: 3,
        expected_sha256: "c".repeat(64),
        question: "Plan ?",
      },
      "idem-plan-request-1",
    );

    const request = lastRequest(recorder);
    expect(request.url).toBe("/api/investigations/inv-1/plans/request");
    expect(request.init?.method).toBe("POST");
    expect(headersOf(request.init)["Idempotency-Key"]).toBe(
      "idem-plan-request-1",
    );
    expect(bodyOf(request.init)).toEqual({
      expected_version: 3,
      expected_sha256: "c".repeat(64),
      question: "Plan ?",
    });
  });

  it("materialises a persisted invocation without an idempotency header", async () => {
    const recorder = installFetch(() => jsonResponse({ created: true }));

    await materializePlan("inv-1", { source_invocation_id: "invoc-1" });

    const request = lastRequest(recorder);
    expect(request.url).toBe("/api/investigations/inv-1/plans");
    expect(headersOf(request.init)).toEqual({
      "Content-Type": "application/json",
    });
    expect(bodyOf(request.init)).toEqual({ source_invocation_id: "invoc-1" });
  });

  it("accepts a plan version under CAS and Idempotency-Key", async () => {
    const recorder = installFetch(() => jsonResponse({ tasks: [] }));

    await acceptPlan(
      "inv-1",
      "plan-version-2",
      { expected_sha256: "d".repeat(64), reason: "Plan accepté" },
      "idem-plan-accept-1",
    );

    const request = lastRequest(recorder);
    expect(request.url).toBe(
      "/api/investigations/inv-1/plans/plan-version-2/accept",
    );
    expect(headersOf(request.init)["Idempotency-Key"]).toBe(
      "idem-plan-accept-1",
    );
    expect(bodyOf(request.init)).toEqual({
      expected_sha256: "d".repeat(64),
      reason: "Plan accepté",
    });
  });

  it("parses a typed stale-plan refusal", async () => {
    installFetch(() => errorResponse(409, "stale_plan_version"));

    await expect(
      acceptPlan(
        "inv-1",
        "plan-version-1",
        { expected_sha256: "e".repeat(64) },
        "idem-plan-accept-2",
      ),
    ).rejects.toMatchObject({ status: 409, code: "stale_plan_version" });
  });
});

describe("plan query keys", () => {
  it("keeps stable keys", () => {
    expect(plansKeys.all("inv-1")).toEqual(["investigation", "inv-1", "plans"]);
    expect(plansKeys.list("inv-1")).toEqual([
      "investigation",
      "inv-1",
      "plans",
      "list",
    ]);
    expect(plansKeys.detail("inv-1", "plan-version-2")).toEqual([
      "investigation",
      "inv-1",
      "plans",
      "plan-version-2",
    ]);
    expect(plansKeys.consultations("inv-1")).toEqual([
      "investigation",
      "inv-1",
      "plans",
      "consultations",
    ]);
  });
});
