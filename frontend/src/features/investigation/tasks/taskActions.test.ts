import { describe, expect, it } from "vitest";

import { makeSession, makeTask } from "./testFixtures";
import { sessionForTask, taskActionAvailability } from "./taskActions";

const available = { workspaceAvailable: true, sessions: [] } as const;

describe("taskActionAvailability", () => {
  it("n’expose les attributions que sur une tâche prête avec un workspace prouvé", () => {
    const ready = makeTask();

    expect(taskActionAvailability(ready, available)).toEqual([
      "take",
      "assign_codex",
      "assign_claude",
      "transfer",
      "interrupt_task",
      "add_note",
    ]);
    expect(
      taskActionAvailability(ready, {
        workspaceAvailable: false,
        sessions: [],
      }),
    ).toEqual(["interrupt_task"]);
    expect(
      taskActionAvailability(ready, { workspaceAvailable: null, sessions: [] }),
    ).toEqual(["interrupt_task"]);
  });

  it("cache toute mutation sur une tâche terminale", () => {
    const completed = makeTask({
      status: "completed",
      result_verification_status: "accepted",
      result_refs: [{ kind: "observation", id: "obs-1" }],
    });

    expect(taskActionAvailability(completed, available)).toEqual([
      "examine_results",
    ]);
    expect(
      taskActionAvailability(makeTask({ status: "cancelled" }), available),
    ).toEqual([]);
  });

  it("n’expose jamais les transitions réservées à l’opérateur", () => {
    const running = makeTask({
      status: "running",
      assigned_actor_kind: "operator",
      assigned_actor_id: "operator:codex_cli",
    });

    const actions = taskActionAvailability(running, available);

    expect(actions).not.toContain("start");
    expect(actions).not.toContain("submit");
    expect(actions).not.toContain("resume_task");
    expect(actions).toContain("interrupt_task");
    expect(actions).toContain("transfer");
  });

  it("ajoute les actions de session selon l’état réel de la session", () => {
    const running = makeTask({
      status: "running",
      assigned_actor_kind: "operator",
      assigned_actor_id: "operator:codex_cli",
    });

    expect(
      taskActionAvailability(running, {
        workspaceAvailable: true,
        sessions: [makeSession()],
      }),
    ).toContain("interrupt_session");

    expect(
      taskActionAvailability(running, {
        workspaceAvailable: true,
        sessions: [makeSession({ state: "lost" })],
      }),
    ).toContain("resume_session");

    const closed = taskActionAvailability(running, {
      workspaceAvailable: true,
      sessions: [
        makeSession({ state: "closed", closed_at: "2026-11-22T10:30:00Z" }),
      ],
    });
    expect(closed).not.toContain("interrupt_session");
    expect(closed).not.toContain("resume_session");
  });

  it("propose Reprendre seulement sur une tâche bloquée, jamais une attribution", () => {
    const blocked = makeTask({
      status: "blocked",
      blocked_reason: "dependency",
    });

    const actions = taskActionAvailability(blocked, available);

    expect(actions).toContain("resume_task");
    expect(actions).not.toContain("take");
    expect(actions).not.toContain("assign_codex");
  });
});

describe("sessionForTask", () => {
  it("choisit la session active la plus récente puis une session repréparable", () => {
    const older = makeSession({
      id: "22222222-2222-4222-8222-222222222222",
      state: "ready",
      updated_at: "2026-11-22T10:00:00Z",
    });
    const newer = makeSession({
      id: "99999999-9999-4999-8999-999999999999",
      state: "running",
      updated_at: "2026-11-22T10:10:00Z",
    });
    expect(sessionForTask([older, newer], older.task_id)?.id).toBe(newer.id);

    const lost = makeSession({
      id: "77777777-7777-4777-8777-777777777777",
      state: "lost",
      updated_at: "2026-11-22T10:20:00Z",
    });
    const closed = makeSession({
      id: "88888888-8888-4888-8888-888888888888",
      state: "closed",
      closed_at: "2026-11-22T10:30:00Z",
      updated_at: "2026-11-22T10:30:00Z",
    });
    expect(sessionForTask([closed, lost], lost.task_id)?.state).toBe("lost");
  });
});
