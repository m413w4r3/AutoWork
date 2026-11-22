import { afterEach, describe, expect, it, vi } from "vitest";

import { transferTask, transfersKeys } from "./transfers";
import {
  bodyOf,
  headersOf,
  installFetch,
  jsonResponse,
  lastRequest,
} from "./testSupport";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("task transfer", () => {
  it("posts the destination and CAS version with Idempotency-Key", async () => {
    const recorder = installFetch(() => jsonResponse({ replayed: false }));

    await transferTask(
      "inv-1",
      "task-5",
      {
        to_actor_kind: "operator",
        to_adapter_kind: "codex_cli",
        instruction: "Reprendre à partir du handoff",
        expected_status_version: 6,
        open_session: true,
        executable: "/usr/local/bin/codex",
        model: "gpt-6-luna",
      },
      "idem-transfer-1",
    );

    const request = lastRequest(recorder);
    expect(request.url).toBe("/api/investigations/inv-1/tasks/task-5/transfer");
    expect(request.init?.method).toBe("POST");
    expect(headersOf(request.init)["Idempotency-Key"]).toBe("idem-transfer-1");
    expect(bodyOf(request.init)).toEqual({
      to_actor_kind: "operator",
      to_adapter_kind: "codex_cli",
      instruction: "Reprendre à partir du handoff",
      expected_status_version: 6,
      open_session: true,
      executable: "/usr/local/bin/codex",
      model: "gpt-6-luna",
    });
  });

  it("parses a typed transfer refusal", async () => {
    installFetch(() =>
      jsonResponse(
        {
          detail: {
            code: "transfer_status_not_allowed",
            message: "completed",
            details: { status: "completed" },
          },
        },
        409,
      ),
    );

    await expect(
      transferTask(
        "inv-1",
        "task-5",
        { to_actor_kind: "human", expected_status_version: 2 },
        "idem-transfer-2",
      ),
    ).rejects.toMatchObject({
      status: 409,
      code: "transfer_status_not_allowed",
      details: { status: "completed" },
    });
  });

  it("keeps a stable mutation key", () => {
    expect(transfersKeys.all("inv-1")).toEqual([
      "investigation",
      "inv-1",
      "transfers",
    ]);
    expect(transfersKeys.mutation("inv-1", "task-5")).toEqual([
      "investigation",
      "inv-1",
      "transfers",
      "task-5",
    ]);
  });
});
