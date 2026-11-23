import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { investigationOperatorQueryKeys } from "../../../api/investigationOperator";
import {
  acceptNoteProposal,
  cancelNoteProposal,
  getNoteProposal,
  ingestNoteProposalResponse,
  listNoteProposals,
  opinionProposalsKeys,
  rejectNoteProposal,
  requestNoteProposal,
  useRequestNoteProposal,
} from "./opinionProposals";
import {
  bodyOf,
  createTestQueryClient,
  errorResponse,
  headersOf,
  installFetch,
  jsonResponse,
  lastRequest,
  wrapperFor,
} from "./testSupport";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("note proposal reads", () => {
  it("lists proposals with the optional status filter", async () => {
    const recorder = installFetch(() => jsonResponse([]));

    await listNoteProposals("inv-1");
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/note-proposals",
    );
    expect(lastRequest(recorder).init?.method).toBe("GET");

    await listNoteProposals("inv-1", "proposed");
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/note-proposals?status=proposed",
    );
  });

  it("reads one proposal diff", async () => {
    const recorder = installFetch(() => jsonResponse({}));

    await getNoteProposal("inv-1", "proposal-2");

    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/note-proposals/proposal-2",
    );
  });
});

describe("note proposal mutations", () => {
  it("requests a draft with the working-state CAS and Idempotency-Key", async () => {
    const recorder = installFetch(() => jsonResponse({ materialised: false }));

    await requestNoteProposal(
      "inv-1",
      {
        anchor: "lead:0012",
        target_block_type: "lead",
        bridge_block_type: "key_findings",
        finding_refs: ["finding-1"],
        observation_refs: ["obs-1"],
        expected_working_state_version: 5,
        expected_working_state_sha256: "f".repeat(64),
      },
      "idem-note-request-1",
    );

    const request = lastRequest(recorder);
    expect(request.url).toBe("/api/investigations/inv-1/note-proposals");
    expect(request.init?.method).toBe("POST");
    expect(headersOf(request.init)["Idempotency-Key"]).toBe(
      "idem-note-request-1",
    );
    expect(bodyOf(request.init)).toMatchObject({
      anchor: "lead:0012",
      target_block_type: "lead",
      bridge_block_type: "key_findings",
      expected_working_state_version: 5,
    });
  });

  it("ingests a response under CAS without an idempotency header", async () => {
    const recorder = installFetch(() => jsonResponse({ status: "proposed" }));

    await ingestNoteProposalResponse("inv-1", "proposal-2", {
      expected_status_version: 3,
    });

    const request = lastRequest(recorder);
    expect(request.url).toBe(
      "/api/investigations/inv-1/note-proposals/proposal-2/ingest",
    );
    expect(headersOf(request.init)).toEqual({
      "Content-Type": "application/json",
    });
    expect(bodyOf(request.init)).toEqual({ expected_status_version: 3 });
  });

  it("accepts with version/hash pins and Idempotency-Key", async () => {
    const recorder = installFetch(() => jsonResponse({ opinion_version: 4 }));

    await acceptNoteProposal(
      "inv-1",
      "proposal-2",
      {
        expected_status_version: 4,
        expected_base_version: 3,
        expected_base_sha256: "a".repeat(64),
        reason: "Note acceptée",
      },
      "idem-note-accept-1",
    );

    const request = lastRequest(recorder);
    expect(request.url).toBe(
      "/api/investigations/inv-1/note-proposals/proposal-2/accept",
    );
    expect(headersOf(request.init)["Idempotency-Key"]).toBe(
      "idem-note-accept-1",
    );
    expect(bodyOf(request.init)).toEqual({
      expected_status_version: 4,
      expected_base_version: 3,
      expected_base_sha256: "a".repeat(64),
      reason: "Note acceptée",
    });
  });

  it("rejects and cancels with the status CAS only", async () => {
    const recorder = installFetch(() => jsonResponse({ status: "rejected" }));

    await rejectNoteProposal("inv-1", "proposal-2", {
      expected_status_version: 2,
      reason: "Hors sujet",
    });
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/note-proposals/proposal-2/reject",
    );
    expect(headersOf(lastRequest(recorder).init)).toEqual({
      "Content-Type": "application/json",
    });
    expect(bodyOf(lastRequest(recorder).init)).toEqual({
      expected_status_version: 2,
      reason: "Hors sujet",
    });

    await cancelNoteProposal("inv-1", "proposal-2", {
      expected_status_version: 3,
    });
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/note-proposals/proposal-2/cancel",
    );
  });

  it("parses a stale-proposal refusal", async () => {
    installFetch(() => errorResponse(409, "stale_note_proposal"));

    await expect(
      acceptNoteProposal(
        "inv-1",
        "proposal-2",
        { expected_status_version: 1 },
        "idem-note-accept-2",
      ),
    ).rejects.toMatchObject({ status: 409, code: "stale_note_proposal" });
  });
});

describe("note proposal query keys", () => {
  it("keeps stable keys with and without a status filter", () => {
    expect(opinionProposalsKeys.all("inv-1")).toEqual([
      "investigation",
      "inv-1",
      "note-proposals",
    ]);
    expect(opinionProposalsKeys.list("inv-1")).toEqual([
      "investigation",
      "inv-1",
      "note-proposals",
      "list",
      "",
    ]);
    expect(opinionProposalsKeys.list("inv-1", "accepted")).toEqual([
      "investigation",
      "inv-1",
      "note-proposals",
      "list",
      "accepted",
    ]);
    expect(opinionProposalsKeys.detail("inv-1", "proposal-2")).toEqual([
      "investigation",
      "inv-1",
      "note-proposals",
      "proposal-2",
    ]);
  });
});

describe("note proposal working-state refresh", () => {
  it("reloads the activity version after a drafting request settles", async () => {
    installFetch(() =>
      jsonResponse({
        proposal: { id: "proposal-1", status: "proposed" },
        invocation_id: "invocation-1",
        invocation_status: "succeeded",
        idempotent_replay: false,
        materialised: true,
      }),
    );
    const client = createTestQueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");

    const { result } = renderHook(() => useRequestNoteProposal("inv-1"), {
      wrapper: wrapperFor(client),
    });
    result.current.mutate({
      anchor: "lead:0001",
      target_block_type: "lead",
      bridge_block_type: "key_findings",
      expected_working_state_version: 7,
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(invalidate).toHaveBeenCalledWith(
      expect.objectContaining({
        queryKey: investigationOperatorQueryKeys.activity("inv-1"),
      }),
    );
  });
});
