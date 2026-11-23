import { afterEach, describe, expect, it, vi } from "vitest";

import {
  agentSessionsKeys,
  enqueueAgentInstruction,
  getAgentSession,
  getAgentSessionCapabilities,
  getAgentSessionSummary,
  interruptAgentSession,
  listAgentSessions,
  openAgentSession,
  resumeAgentSession,
} from "./agentSessions";
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

describe("agent session reads", () => {
  it("lists sessions with the optional task filter and reads one", async () => {
    const recorder = installFetch(() => jsonResponse({}));

    await listAgentSessions("inv-1");
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/agent-sessions",
    );
    expect(lastRequest(recorder).init?.method).toBe("GET");

    await listAgentSessions("inv-1", "task-1");
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/agent-sessions?task_id=task-1",
    );

    await getAgentSession("inv-1", "session-2");
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/agent-sessions/session-2",
    );
  });

  it("reads the human-only journal summary of one session", async () => {
    const recorder = installFetch(() =>
      jsonResponse({
        session_id: "session-2",
        operations: { items: [], total: 0, limit: 100, truncated: false },
        instructions: { items: [], total: 0, limit: 20, truncated: false },
      }),
    );

    const summary = await getAgentSessionSummary("inv-1", "session-2");

    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/agent-sessions/session-2/summary",
    );
    expect(lastRequest(recorder).init?.method).toBe("GET");
    expect(summary.operations.truncated).toBe(false);
    expect(summary.instructions.limit).toBe(20);
  });

  it("probes capabilities with the adapter and the executable only", async () => {
    const recorder = installFetch(() =>
      jsonResponse({
        adapter_kind: "codex_cli",
        executable: "codex",
        available: true,
        version: "codex-cli 0.163.0",
        capability_sha256: "a".repeat(64),
        probed_at: "2026-11-22T10:05:00Z",
        supports_resume: true,
        supports_structured_output: true,
        supports_non_interactive: true,
        modes: ["non_interactive"],
        permission_modes: [],
        confinement: {
          profile: "bwrap-loopback",
          bwrap_ok: true,
          netns: true,
          network_mode: "isolated_egress",
          loopback_isolated: true,
        },
      }),
    );

    const capabilities = await getAgentSessionCapabilities(
      "codex_cli",
      "codex",
    );

    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/agent-sessions/capabilities?adapter_kind=codex_cli&executable=codex",
    );
    expect(lastRequest(recorder).init?.method).toBe("GET");
    expect(capabilities.confinement?.network_mode).toBe("isolated_egress");
    expect(capabilities.available).toBe(true);
  });

  it("keeps the typed refusal of an unattested confinement", async () => {
    installFetch(() =>
      errorResponse(
        503,
        "agent_session_network_isolation_unavailable",
        "the host CLI sandbox is not attested loopback-isolated",
      ),
    );

    await expect(
      getAgentSessionCapabilities("claude_code", "claude"),
    ).rejects.toMatchObject({
      status: 503,
      code: "agent_session_network_isolation_unavailable",
    });
  });
});

describe("agent session human actions", () => {
  it("opens a session with Idempotency-Key", async () => {
    const recorder = installFetch(() => jsonResponse({ session: {} }));

    await openAgentSession(
      "inv-1",
      {
        task_id: "task-1",
        adapter_kind: "claude_code",
        handoff_id: "handoff-3",
        executable: "/usr/local/bin/claude",
        model: "claude-sonnet-5-5",
      },
      "idem-session-open-1",
    );

    const request = lastRequest(recorder);
    expect(request.url).toBe("/api/investigations/inv-1/agent-sessions");
    expect(request.init?.method).toBe("POST");
    expect(headersOf(request.init)["Idempotency-Key"]).toBe(
      "idem-session-open-1",
    );
    expect(bodyOf(request.init)).toEqual({
      task_id: "task-1",
      adapter_kind: "claude_code",
      handoff_id: "handoff-3",
      executable: "/usr/local/bin/claude",
      model: "claude-sonnet-5-5",
    });
  });

  it("enqueues a human instruction with note references", async () => {
    const recorder = installFetch(() => jsonResponse({ queue_position: 1 }));

    await enqueueAgentInstruction(
      "inv-1",
      "session-2",
      { text: "Vérifie les IOC", note_refs: ["notes/humaines/x.md"] },
      "idem-instruction-1",
    );

    const request = lastRequest(recorder);
    expect(request.url).toBe(
      "/api/investigations/inv-1/agent-sessions/session-2/instructions",
    );
    expect(headersOf(request.init)["Idempotency-Key"]).toBe(
      "idem-instruction-1",
    );
    expect(bodyOf(request.init)).toEqual({
      action: "enqueue",
      text: "Vérifie les IOC",
      note_refs: ["notes/humaines/x.md"],
    });
  });

  it("interrupts with a reason and resumes without a body", async () => {
    const recorder = installFetch(() => jsonResponse({}));

    await interruptAgentSession("inv-1", "session-2", "Changement de plan");
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/agent-sessions/session-2/interrupt",
    );
    expect(bodyOf(lastRequest(recorder).init)).toEqual({
      reason: "Changement de plan",
    });
    expect(headersOf(lastRequest(recorder).init)).toEqual({
      "Content-Type": "application/json",
    });

    await resumeAgentSession("inv-1", "session-2");
    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/agent-sessions/session-2/resume",
    );
    expect(lastRequest(recorder).init?.body).toBeUndefined();
    expect(headersOf(lastRequest(recorder).init)).toEqual({});
  });

  it("parses the typed active-session conflict", async () => {
    installFetch(() => errorResponse(409, "active_session_exists"));

    await expect(
      openAgentSession(
        "inv-1",
        {
          task_id: "task-1",
          adapter_kind: "codex_cli",
          handoff_id: "handoff-3",
          executable: "/usr/local/bin/codex",
        },
        "idem-session-open-2",
      ),
    ).rejects.toMatchObject({ status: 409, code: "active_session_exists" });
  });
});

describe("agent session query keys", () => {
  it("keeps stable keys with and without a task filter", () => {
    expect(agentSessionsKeys.all("inv-1")).toEqual([
      "investigation",
      "inv-1",
      "agent-sessions",
    ]);
    expect(agentSessionsKeys.list("inv-1")).toEqual([
      "investigation",
      "inv-1",
      "agent-sessions",
      "list",
      "",
    ]);
    expect(agentSessionsKeys.list("inv-1", "task-1")).toEqual([
      "investigation",
      "inv-1",
      "agent-sessions",
      "list",
      "task-1",
    ]);
    expect(agentSessionsKeys.detail("inv-1", "session-2")).toEqual([
      "investigation",
      "inv-1",
      "agent-sessions",
      "session-2",
    ]);
    expect(agentSessionsKeys.summary("inv-1", "session-2")).toEqual([
      "investigation",
      "inv-1",
      "agent-sessions",
      "session-2",
      "summary",
    ]);
    expect(agentSessionsKeys.capabilities("codex_cli", "codex")).toEqual([
      "agent-sessions",
      "capabilities",
      "codex_cli",
      "codex",
    ]);
  });
});
