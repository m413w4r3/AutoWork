import { afterEach, describe, expect, it, vi } from "vitest";

import {
  approveBootstrapEnvelope,
  bootstrapKeys,
  getBootstrapInventory,
  getBootstrapPlan,
  getBootstrapSnapshot,
  getBootstrapSnapshotExport,
  getBootstrapEnvelopeProposal,
  runBootstrap,
} from "./bootstrap";
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

describe("bootstrap reads", () => {
  it("reads each human-only bootstrap projection", async () => {
    const recorder = installFetch(() => jsonResponse({ ok: true }));
    const investigationId = "inv-9";

    await getBootstrapInventory(investigationId);
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-9/bootstrap/inventory",
    );
    expect(lastRequest(recorder).init?.method).toBe("GET");

    await getBootstrapPlan(investigationId);
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-9/bootstrap/plan",
    );

    await getBootstrapSnapshot(investigationId);
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-9/bootstrap/snapshot",
    );

    await getBootstrapSnapshotExport(investigationId);
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-9/bootstrap/snapshot/export",
    );

    await getBootstrapEnvelopeProposal(investigationId);
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-9/bootstrap/envelope-proposal",
    );
  });
});

describe("bootstrap mutations", () => {
  it("runs the batch with a mandatory Idempotency-Key and no body", async () => {
    const recorder = installFetch(() =>
      jsonResponse({ snapshot_id: "snap-1" }),
    );

    await runBootstrap("inv-9", "idem-bootstrap-1");

    const request = lastRequest(recorder);
    expect(request.url).toBe("/api/investigations/inv-9/bootstrap/run");
    expect(request.init?.method).toBe("POST");
    expect(request.init?.body).toBeUndefined();
    expect(headersOf(request.init)).toEqual({
      "Idempotency-Key": "idem-bootstrap-1",
    });
  });

  it("approves the exact proposal with CAS and Idempotency-Key", async () => {
    const recorder = installFetch(() => jsonResponse({ approved: true }));

    await approveBootstrapEnvelope(
      "inv-9",
      {
        reason: "Enveloppe bornée acceptée",
        correlation_id: "corr-1",
        proposal_sha256: "a".repeat(64),
        expected_version: 2,
      },
      "idem-envelope-1",
    );

    const request = lastRequest(recorder);
    expect(request.url).toBe(
      "/api/investigations/inv-9/bootstrap/envelope/approve",
    );
    expect(request.init?.method).toBe("POST");
    expect(headersOf(request.init)["Idempotency-Key"]).toBe("idem-envelope-1");
    expect(bodyOf(request.init)).toEqual({
      reason: "Enveloppe bornée acceptée",
      correlation_id: "corr-1",
      proposal_sha256: "a".repeat(64),
      expected_version: 2,
    });
  });

  it("parses the typed envelope conflict", async () => {
    installFetch(() => errorResponse(409, "envelope_conflict", "version"));

    await expect(
      approveBootstrapEnvelope(
        "inv-9",
        {
          reason: "r",
          correlation_id: "c",
          proposal_sha256: "b".repeat(64),
        },
        "idem-envelope-2",
      ),
    ).rejects.toMatchObject({ status: 409, code: "envelope_conflict" });
  });
});

describe("bootstrap query keys", () => {
  it("keeps stable, hierarchical keys", () => {
    expect(bootstrapKeys.all("inv-9")).toEqual([
      "investigation",
      "inv-9",
      "bootstrap",
    ]);
    expect(bootstrapKeys.inventory("inv-9")).toEqual([
      "investigation",
      "inv-9",
      "bootstrap",
      "inventory",
    ]);
    expect(bootstrapKeys.plan("inv-9")).toEqual([
      "investigation",
      "inv-9",
      "bootstrap",
      "plan",
    ]);
    expect(bootstrapKeys.snapshot("inv-9")).toEqual([
      "investigation",
      "inv-9",
      "bootstrap",
      "snapshot",
    ]);
    expect(bootstrapKeys.snapshotExport("inv-9")).toEqual([
      "investigation",
      "inv-9",
      "bootstrap",
      "snapshot-export",
    ]);
    expect(bootstrapKeys.envelopeProposal("inv-9")).toEqual([
      "investigation",
      "inv-9",
      "bootstrap",
      "envelope-proposal",
    ]);
  });
});
