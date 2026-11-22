import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createHandoff,
  getHandoff,
  getHandoffFreshness,
  handoffsKeys,
  listHandoffs,
} from "./handoffs";
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

describe("handoff reads", () => {
  it("lists handoffs with the optional task filter", async () => {
    const recorder = installFetch(() => jsonResponse([]));

    await listHandoffs("inv-1");
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/handoffs",
    );
    expect(lastRequest(recorder).init?.method).toBe("GET");

    await listHandoffs("inv-1", "task-3");
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/handoffs?task_id=task-3",
    );
  });

  it("reads one handoff and its freshness report", async () => {
    const recorder = installFetch(() => jsonResponse({}));

    await getHandoff("inv-1", "handoff-4");
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/handoffs/handoff-4",
    );

    await getHandoffFreshness("inv-1", "handoff-4");
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/handoffs/handoff-4/freshness",
    );
  });
});

describe("handoff creation", () => {
  it("posts cited canonical ids with Idempotency-Key", async () => {
    const recorder = installFetch(() => jsonResponse({ version: 1 }));

    await createHandoff(
      "inv-1",
      {
        task_id: "task-3",
        to_actor: { kind: "operator", actor_id: "codex" },
        observation_ids: ["obs-1"],
        file_revision_ids: ["rev-0123456789abcdef0123456789abcdef"],
        next_action: "Continuer l’analyse",
        work: {
          hypotheses: [
            {
              text: "Le C2 utilise HTTPS",
              refs: [{ kind: "observation", id: "obs-1" }],
            },
          ],
        },
      },
      "idem-handoff-1",
    );

    const request = lastRequest(recorder);
    expect(request.url).toBe("/api/investigations/inv-1/handoffs");
    expect(request.init?.method).toBe("POST");
    expect(headersOf(request.init)["Idempotency-Key"]).toBe("idem-handoff-1");
    expect(bodyOf(request.init)).toMatchObject({
      task_id: "task-3",
      to_actor: { kind: "operator", actor_id: "codex" },
      observation_ids: ["obs-1"],
      next_action: "Continuer l’analyse",
    });
  });

  it("parses a stale-task handoff refusal", async () => {
    installFetch(() => errorResponse(409, "handoff_task_stale"));

    await expect(
      createHandoff(
        "inv-1",
        {
          task_id: "task-3",
          to_actor: { kind: "human", actor_id: "human:analyst" },
          next_action: "Reprendre",
        },
        "idem-handoff-2",
      ),
    ).rejects.toMatchObject({ status: 409, code: "handoff_task_stale" });
  });
});

describe("handoff query keys", () => {
  it("keeps stable keys with and without a task filter", () => {
    expect(handoffsKeys.all("inv-1")).toEqual([
      "investigation",
      "inv-1",
      "handoffs",
    ]);
    expect(handoffsKeys.list("inv-1")).toEqual([
      "investigation",
      "inv-1",
      "handoffs",
      "list",
      "",
    ]);
    expect(handoffsKeys.list("inv-1", "task-3")).toEqual([
      "investigation",
      "inv-1",
      "handoffs",
      "list",
      "task-3",
    ]);
    expect(handoffsKeys.detail("inv-1", "handoff-4")).toEqual([
      "investigation",
      "inv-1",
      "handoffs",
      "handoff-4",
    ]);
    expect(handoffsKeys.freshness("inv-1", "handoff-4")).toEqual([
      "investigation",
      "inv-1",
      "handoffs",
      "handoff-4",
      "freshness",
    ]);
  });
});
