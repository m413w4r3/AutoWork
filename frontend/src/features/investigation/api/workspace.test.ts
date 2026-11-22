import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ensureWorkspace,
  exportWorkspace,
  getWorkspaceStatus,
  listWorkspaceConflicts,
  listWorkspaceFiles,
  readWorkspaceFile,
  resolveWorkspaceConflict,
  syncWorkspace,
  workspaceKeys,
  writeWorkspaceNote,
} from "./workspace";
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

describe("workspace reads", () => {
  it("reads status, file page, content and conflicts", async () => {
    const recorder = installFetch(() => jsonResponse({}));

    await getWorkspaceStatus("inv-1");
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/workspace",
    );
    expect(lastRequest(recorder).init?.method).toBe("GET");

    await listWorkspaceFiles("inv-1", {
      prefix: "notes/humaines",
      page_token: "page-2",
      limit: 50,
    });
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/workspace/files" +
        "?prefix=notes%2Fhumaines&page_token=page-2&limit=50",
    );

    await readWorkspaceFile("inv-1", "notes/humaines/a.md", "a".repeat(64));
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/workspace/files/content" +
        `?path=notes%2Fhumaines%2Fa.md&expected_sha256=${"a".repeat(64)}`,
    );

    await listWorkspaceConflicts("inv-1");
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/workspace/conflicts",
    );
  });
});

describe("workspace actions", () => {
  it("runs ensure/sync/export as bodyless POSTs without idempotency", async () => {
    const recorder = installFetch(() => jsonResponse({}));

    await ensureWorkspace("inv-1");
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/workspace/ensure",
    );
    expect(lastRequest(recorder).init?.method).toBe("POST");
    expect(lastRequest(recorder).init?.body).toBeUndefined();
    expect(headersOf(lastRequest(recorder).init)).toEqual({});

    await syncWorkspace("inv-1");
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/workspace/sync",
    );

    await exportWorkspace("inv-1");
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/workspace/exports",
    );
  });

  it("writes a note with the exact CAS SHA and Idempotency-Key", async () => {
    const recorder = installFetch(() =>
      jsonResponse({ path: "notes/humaines/a.md" }),
    );

    await writeWorkspaceNote(
      "inv-1",
      {
        path: "notes/humaines/a.md",
        expected_sha256: "new",
        text: "Observation locale",
      },
      "idem-note-write-1",
    );

    const request = lastRequest(recorder);
    expect(request.url).toBe("/api/investigations/inv-1/workspace/files");
    expect(request.init?.method).toBe("PUT");
    expect(headersOf(request.init)["Idempotency-Key"]).toBe(
      "idem-note-write-1",
    );
    expect(bodyOf(request.init)).toEqual({
      path: "notes/humaines/a.md",
      expected_sha256: "new",
      text: "Observation locale",
    });
  });

  it("resolves a conflict with both recorded SHAs and Idempotency-Key", async () => {
    const recorder = installFetch(() =>
      jsonResponse({ choice: "keep_current" }),
    );

    await resolveWorkspaceConflict(
      "inv-1",
      "cfl-0123456789abcdef0123456789abcdef",
      {
        choice: "keep_current",
        expected_current_sha256: "b".repeat(64),
        expected_proposed_sha256: "c".repeat(64),
        reason: "Version humaine conservée",
      },
      "idem-conflict-1",
    );

    const request = lastRequest(recorder);
    expect(request.url).toBe(
      "/api/investigations/inv-1/workspace/conflicts/" +
        "cfl-0123456789abcdef0123456789abcdef/resolve",
    );
    expect(headersOf(request.init)["Idempotency-Key"]).toBe("idem-conflict-1");
    expect(bodyOf(request.init)).toEqual({
      choice: "keep_current",
      expected_current_sha256: "b".repeat(64),
      expected_proposed_sha256: "c".repeat(64),
      reason: "Version humaine conservée",
    });
  });

  it("parses a typed content mismatch", async () => {
    installFetch(() => errorResponse(409, "workspace_content_mismatch"));

    await expect(
      readWorkspaceFile("inv-1", "notes/humaines/a.md", "d".repeat(64)),
    ).rejects.toMatchObject({
      status: 409,
      code: "workspace_content_mismatch",
    });
  });
});

describe("workspace query keys", () => {
  it("keeps stable keys including filters and CAS", () => {
    expect(workspaceKeys.all("inv-1")).toEqual([
      "investigation",
      "inv-1",
      "workspace",
    ]);
    expect(workspaceKeys.status("inv-1")).toEqual([
      "investigation",
      "inv-1",
      "workspace",
      "status",
    ]);
    expect(
      workspaceKeys.files("inv-1", { prefix: "notes", limit: 10 }),
    ).toEqual([
      "investigation",
      "inv-1",
      "workspace",
      "files",
      "notes",
      "",
      10,
    ]);
    expect(
      workspaceKeys.content("inv-1", "notes/a.md", "a".repeat(64)),
    ).toEqual([
      "investigation",
      "inv-1",
      "workspace",
      "content",
      "notes/a.md",
      "a".repeat(64),
    ]);
    expect(workspaceKeys.conflicts("inv-1")).toEqual([
      "investigation",
      "inv-1",
      "workspace",
      "conflicts",
    ]);
  });
});
