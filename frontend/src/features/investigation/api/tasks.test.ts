import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createTask,
  getTask,
  getTimeline,
  listTaskEvents,
  listTasks,
  runTaskCommand,
  tasksKeys,
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
