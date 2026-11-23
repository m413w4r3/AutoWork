import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createTask,
  getTask,
  getTimeline,
  listTaskEvents,
  listTasks,
  runTaskCommand,
  tasksKeys,
  type TimelinePageV1,
} from "./tasks";
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

describe("task reads", () => {
  it("reads the task page, one task and the event page", async () => {
    const recorder = installFetch(() => jsonResponse({ items: [], total: 0 }));

    await listTasks("inv-1");
    expect(lastRequest(recorder).url).toBe("/api/investigations/inv-1/tasks");
    expect(lastRequest(recorder).init?.method).toBe("GET");

    await getTask("inv-1", "task-2");
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/tasks/task-2",
    );

    await listTaskEvents("inv-1", "task-2", { cursor: "abc", limit: 25 });
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/tasks/task-2/events?cursor=abc&limit=25",
    );
  });

  it("reads a paged timeline", async () => {
    const recorder = installFetch(() =>
      jsonResponse({ items: [], next_cursor: null, has_more: false }),
    );

    await getTimeline("inv-1", { cursor: "cursor-1", limit: 50 });

    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/timeline?cursor=cursor-1&limit=50",
    );
  });

  it("keeps the B7 working item types inside the same envelope", async () => {
    const page: TimelinePageV1 = {
      items: [
        {
          item_type: "handoff",
          object_id: "hd-1",
          occurred_at: "2026-11-22T10:00:00Z",
          task_id: "task-2",
          actor_kind: "human",
          actor_id: "analyst",
          summary: "handoff:v1:human->operator",
          evidence: false,
          refs: { handoff_id: "hd-1", task_id: "task-2", version: "1" },
          linked_event_ids: [],
          linked_observation_ids: [],
        },
        {
          item_type: "contribution",
          object_id: "ct-1",
          occurred_at: "2026-11-22T10:01:00Z",
          task_id: "task-2",
          actor_kind: "operator",
          actor_id: "codex",
          summary: "contribution:finding:imported",
          evidence: false,
          refs: { contribution_id: "ct-1", file_sha256: "a".repeat(64) },
          linked_event_ids: [],
          linked_observation_ids: [],
        },
        {
          item_type: "agent_session",
          object_id: "se-1",
          occurred_at: "2026-11-22T10:02:00Z",
          task_id: "task-2",
          actor_kind: "system",
          actor_id: "codex_cli",
          summary: "agent_session:codex_cli:running",
          evidence: false,
          refs: { session_id: "se-1", adapter_kind: "codex_cli" },
          linked_event_ids: [],
          linked_observation_ids: [],
        },
        {
          item_type: "instruction",
          object_id: "in-1",
          occurred_at: "2026-11-22T10:03:00Z",
          task_id: "task-2",
          actor_kind: "human",
          actor_id: "analyst",
          summary: "instruction:queued",
          evidence: false,
          refs: { instruction_id: "in-1", delivery_attempts: "0" },
          linked_event_ids: [],
          linked_observation_ids: [],
        },
      ],
      next_cursor: null,
      has_more: false,
    };
    installFetch(() => jsonResponse(page));

    const result = await getTimeline("inv-1");

    expect(result.items.map((item) => item.item_type)).toEqual([
      "handoff",
      "contribution",
      "agent_session",
      "instruction",
    ]);
    expect(result.items.every((item) => item.evidence === false)).toBe(true);
    expect(result.items[0]?.refs.handoff_id).toBe("hd-1");
  });
});

describe("task mutations", () => {
  it("creates a task with Idempotency-Key", async () => {
    const recorder = installFetch(() =>
      jsonResponse({ task: {}, event: null }),
    );

    await createTask(
      "inv-1",
      { task_key: "lead-1", title: "Vérifier la C2" },
      "idem-task-create-1",
    );

    const request = lastRequest(recorder);
    expect(request.url).toBe("/api/investigations/inv-1/tasks");
    expect(request.init?.method).toBe("POST");
    expect(headersOf(request.init)["Idempotency-Key"]).toBe(
      "idem-task-create-1",
    );
    expect(bodyOf(request.init)).toEqual({
      task_key: "lead-1",
      title: "Vérifier la C2",
    });
  });

  it("runs a lease-bound command with CAS, fence and Idempotency-Key", async () => {
    const recorder = installFetch(() =>
      jsonResponse({ task: {}, event: null }),
    );

    await runTaskCommand(
      "inv-1",
      "task-2",
      { command: "heartbeat", expected_status_version: 4, lease_fence: 7 },
      "idem-task-command-1",
    );

    const request = lastRequest(recorder);
    expect(request.url).toBe("/api/investigations/inv-1/tasks/task-2/commands");
    expect(request.init?.method).toBe("POST");
    expect(headersOf(request.init)["Idempotency-Key"]).toBe(
      "idem-task-command-1",
    );
    expect(bodyOf(request.init)).toEqual({
      command: "heartbeat",
      expected_status_version: 4,
      lease_fence: 7,
    });
  });

  it("parses a stale task version refusal", async () => {
    installFetch(() => errorResponse(409, "stale_task_version", "stale"));

    await expect(
      runTaskCommand(
        "inv-1",
        "task-2",
        { command: "start", expected_status_version: 1 },
        "idem-task-command-2",
      ),
    ).rejects.toMatchObject({ status: 409, code: "stale_task_version" });
  });
});

describe("task query keys", () => {
  it("keeps stable keys and includes paging parameters", () => {
    expect(tasksKeys.all("inv-1")).toEqual(["investigation", "inv-1", "tasks"]);
    expect(tasksKeys.list("inv-1")).toEqual([
      "investigation",
      "inv-1",
      "tasks",
      "list",
    ]);
    expect(tasksKeys.detail("inv-1", "task-2")).toEqual([
      "investigation",
      "inv-1",
      "tasks",
      "task-2",
    ]);
    expect(
      tasksKeys.events("inv-1", "task-2", { cursor: "abc", limit: 25 }),
    ).toEqual([
      "investigation",
      "inv-1",
      "tasks",
      "task-2",
      "events",
      "abc",
      25,
    ]);
    expect(tasksKeys.timeline("inv-1", { cursor: "c", limit: 10 })).toEqual([
      "investigation",
      "inv-1",
      "timeline",
      "c",
      10,
    ]);
  });
});
