import { afterEach, describe, expect, it, vi } from "vitest";

import {
  annotateWorkspaceNote,
  askAgentToContinueFromNote,
  askBridgeToReviewNote,
  encodeNotePath,
  getWorkspaceNoteView,
  proposeNoteObservationConversion,
  proposeNoteVerificationTask,
  workspaceNotesKeys,
} from "./workspaceNotes";
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

const REVISION = "rev-0123456789abcdef0123456789abcdef";

describe("workspace note reads", () => {
  it("reads a note view keeping path slashes but escaping segments", async () => {
    const recorder = installFetch(() => jsonResponse({}));

    await getWorkspaceNoteView("inv-1", "notes/humaines/analyse 1.md");

    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/workspace/notes/" +
        "notes/humaines/analyse%201.md/view",
    );
    expect(lastRequest(recorder).init?.method).toBe("GET");
    expect(encodeNotePath("notes/humaines/a.md")).toBe("notes/humaines/a.md");
  });
});

describe("workspace note actions", () => {
  it("annotates under Idempotency-Key", async () => {
    const recorder = installFetch(() => jsonResponse({ kind: "link_ioc" }));

    await annotateWorkspaceNote(
      "inv-1",
      REVISION,
      { kind: "link_ioc", ioc_type: "ipv4", ioc_value: "203.0.113.7" },
      "idem-annotation-1",
    );

    const request = lastRequest(recorder);
    expect(request.url).toBe(
      `/api/investigations/inv-1/workspace/notes/${REVISION}/annotations`,
    );
    expect(request.init?.method).toBe("POST");
    expect(headersOf(request.init)["Idempotency-Key"]).toBe(
      "idem-annotation-1",
    );
    expect(bodyOf(request.init)).toEqual({
      kind: "link_ioc",
      ioc_type: "ipv4",
      ioc_value: "203.0.113.7",
    });
  });

  it("asks Bridge with the working-state CAS", async () => {
    const recorder = installFetch(() => jsonResponse({ pending: true }));

    await askBridgeToReviewNote(
      "inv-1",
      REVISION,
      {
        consultation_type: "critique",
        question: "Cette note est-elle cohérente ?",
        expected_working_state_version: 8,
        evidence_refs: ["obs-1"],
      },
      "idem-bridge-review-1",
    );

    const request = lastRequest(recorder);
    expect(request.url).toBe(
      `/api/investigations/inv-1/workspace/notes/${REVISION}/ask-bridge`,
    );
    expect(headersOf(request.init)["Idempotency-Key"]).toBe(
      "idem-bridge-review-1",
    );
    expect(bodyOf(request.init)).toMatchObject({
      consultation_type: "critique",
      expected_working_state_version: 8,
    });
  });

  it("asks the agent with task/session refs and a key", async () => {
    const recorder = installFetch(() =>
      jsonResponse({ instruction_id: "i-1" }),
    );

    await askAgentToContinueFromNote(
      "inv-1",
      REVISION,
      { task_id: "task-1", session_id: "session-2", question: "Continue" },
      "idem-agent-continue-1",
    );

    const request = lastRequest(recorder);
    expect(request.url).toBe(
      `/api/investigations/inv-1/workspace/notes/${REVISION}/ask-agent`,
    );
    expect(headersOf(request.init)["Idempotency-Key"]).toBe(
      "idem-agent-continue-1",
    );
    expect(bodyOf(request.init)).toEqual({
      task_id: "task-1",
      session_id: "session-2",
      question: "Continue",
    });
  });

  it("proposes a verification task with a key", async () => {
    const recorder = installFetch(() => jsonResponse({ status: "proposed" }));

    await proposeNoteVerificationTask(
      "inv-1",
      REVISION,
      {
        passage_start: 0,
        passage_end: 120,
        question: "Vérifier cette affirmation",
        claim_refs: ["claim-1"],
      },
      "idem-verification-1",
    );

    const request = lastRequest(recorder);
    expect(request.url).toBe(
      `/api/investigations/inv-1/workspace/notes/${REVISION}/verification-task`,
    );
    expect(headersOf(request.init)["Idempotency-Key"]).toBe(
      "idem-verification-1",
    );
    expect(bodyOf(request.init)).toMatchObject({
      passage_start: 0,
      passage_end: 120,
      question: "Vérifier cette affirmation",
    });
  });

  it("declares an observation proposal without writing or a key", async () => {
    const recorder = installFetch(() => jsonResponse({ is_evidence: false }));

    await proposeNoteObservationConversion("inv-1", REVISION, {
      object_type: "ip",
      method: "dns_query",
      tool: "dig",
    });

    const request = lastRequest(recorder);
    expect(request.url).toBe(
      `/api/investigations/inv-1/workspace/notes/${REVISION}/observation-proposal`,
    );
    expect(headersOf(request.init)).toEqual({
      "Content-Type": "application/json",
    });
    expect(bodyOf(request.init)).toEqual({
      object_type: "ip",
      method: "dns_query",
      tool: "dig",
    });
  });

  it("parses a typed missing-note refusal", async () => {
    installFetch(() => errorResponse(404, "note_not_found"));

    await expect(
      getWorkspaceNoteView("inv-1", "notes/humaines/missing.md"),
    ).rejects.toMatchObject({ status: 404, code: "note_not_found" });
  });
});

describe("workspace note query keys", () => {
  it("keeps stable keys", () => {
    expect(workspaceNotesKeys.all("inv-1")).toEqual([
      "investigation",
      "inv-1",
      "workspace-notes",
    ]);
    expect(workspaceNotesKeys.view("inv-1", "notes/a.md")).toEqual([
      "investigation",
      "inv-1",
      "workspace-notes",
      "view",
      "notes/a.md",
    ]);
  });
});
