import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  bodyOf,
  createTestQueryClient,
  errorResponse,
  headersOf,
  installFetch,
  jsonResponse,
  wrapperFor,
} from "../api/testSupport";
import { OperatorsPanel } from "./OperatorsPanel";
import {
  HANDOFF_ID,
  INVESTIGATION_ID,
  makeSession,
  makeTask,
  SESSION_ID,
  TASK_ID,
} from "./testFixtures";

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderPanel() {
  return render(<OperatorsPanel investigationId={INVESTIGATION_ID} />, {
    wrapper: wrapperFor(createTestQueryClient()),
  });
}

function taskPage(task = makeTask()) {
  return { items: [task], total: 1 };
}

describe("OperatorsPanel", () => {
  it("met en file une instruction quand l’agent est occupé", async () => {
    const user = userEvent.setup();
    const task = makeTask({
      status: "running",
      assigned_actor_kind: "operator",
      assigned_actor_id: "operator:codex_cli",
      lease_expires_at: "2026-11-22T10:20:00Z",
    });
    const recorder = installFetch(({ url, init }) => {
      const method = init?.method ?? "GET";
      if (url.endsWith("/agent-sessions") && method === "GET") {
        return jsonResponse([makeSession()]);
      }
      if (url.endsWith("/tasks") && method === "GET") {
        return jsonResponse(taskPage(task));
      }
      if (url.endsWith("/instructions") && method === "POST") {
        return jsonResponse({
          id: "55555555-5555-4555-8555-555555555555",
          investigation_id: INVESTIGATION_ID,
          task_id: TASK_ID,
          session_id: SESSION_ID,
          state: "queued",
          text_sha256: "c".repeat(64),
          queued_at: "2026-11-22T10:06:00Z",
          delivered_at: null,
          acknowledged_at: null,
          created_by: { kind: "human", actor_id: "analyst:test" },
          created_at: "2026-11-22T10:06:00Z",
          updated_at: "2026-11-22T10:06:00Z",
          queue_position: 2,
          delivered_live: false,
          note_diffs: [],
        });
      }
      return errorResponse(500, "unexpected_request");
    });

    renderPanel();

    await screen.findByText("Codex CLI · Vérifier le domaine C2");
    await user.type(
      screen.getByLabelText(/Instruction en cours de session/),
      "Priorise le PCAP.",
    );
    await user.click(
      screen.getByRole("button", { name: "Envoyer l’instruction" }),
    );

    expect(
      await screen.findByText(/mise en file persistante \(position 2\)/),
    ).toBeInTheDocument();
    const request = recorder.calls.find((call) =>
      call.url.endsWith("/instructions"),
    );
    expect(request).toBeDefined();
    if (request === undefined) throw new Error("instruction was not sent");
    expect(bodyOf(request.init)).toEqual({
      action: "enqueue",
      text: "Priorise le PCAP.",
      note_refs: [],
    });
    expect(headersOf(request.init)["Idempotency-Key"]).toBeTruthy();
  });

  it("signale une reprise indisponible et exige une nouvelle passation", async () => {
    const user = userEvent.setup();
    installFetch(({ url, init }) => {
      const method = init?.method ?? "GET";
      if (url.endsWith("/agent-sessions") && method === "GET") {
        return jsonResponse([
          makeSession({ state: "lost", close_reason: "session_lost" }),
        ]);
      }
      if (url.endsWith("/tasks") && method === "GET") {
        return jsonResponse(
          taskPage(
            makeTask({
              status: "blocked",
              blocked_reason: "session_lost",
              assigned_actor_kind: "operator",
              assigned_actor_id: "operator:codex_cli",
            }),
          ),
        );
      }
      if (url.endsWith("/resume") && method === "POST") {
        return errorResponse(409, "resume_unavailable");
      }
      return errorResponse(500, "unexpected_request");
    });

    renderPanel();

    await screen.findByText(/Session perdue : la réconciliation hôte/);
    await user.click(
      screen.getByRole("button", { name: "Reprendre la session" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Reprise indisponible : ouvrez une nouvelle session avec une nouvelle passation/,
    );
  });

  it("masque la reprise d’une session quand la tâche est terminale", async () => {
    installFetch(({ url, init }) => {
      const method = init?.method ?? "GET";
      if (url.endsWith("/agent-sessions") && method === "GET") {
        return jsonResponse([makeSession({ state: "lost" })]);
      }
      if (url.endsWith("/tasks") && method === "GET") {
        return jsonResponse(
          taskPage(
            makeTask({
              status: "completed",
              assigned_actor_kind: "operator",
              assigned_actor_id: "operator:codex_cli",
            }),
          ),
        );
      }
      return errorResponse(500, "unexpected_request");
    });

    renderPanel();

    expect(
      await screen.findByText(/Reprise masquée : la tâche est terminée/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Reprendre la session" }),
    ).not.toBeInTheDocument();
  });

  it("propose l’ouverture explicite d’une tâche attribuée sans session", async () => {
    const user = userEvent.setup();
    const recorder = installFetch(({ url, init }) => {
      const method = init?.method ?? "GET";
      if (url.includes("/agent-sessions/capabilities")) {
        return jsonResponse({
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
        });
      }
      if (url.endsWith("/agent-sessions") && method === "GET") {
        return jsonResponse([]);
      }
      if (url.endsWith("/agent-sessions") && method === "POST") {
        return jsonResponse({ session: makeSession() });
      }
      if (url.endsWith("/tasks") && method === "GET") {
        return jsonResponse(
          taskPage(
            makeTask({
              status: "assigned",
              assigned_actor_kind: "operator",
              assigned_actor_id: "operator:codex_cli",
              handoff_ref: HANDOFF_ID,
            }),
          ),
        );
      }
      return errorResponse(500, "unexpected_request");
    });

    renderPanel();

    expect(
      await screen.findByRole("heading", {
        name: /Tâches attribuées sans session \(1\)/,
      }),
    ).toBeInTheDocument();
    await user.click(
      await screen.findByRole("button", {
        name: "Ouvrir la session Codex CLI",
      }),
    );

    await waitFor(() =>
      expect(
        recorder.calls.some(
          (call) =>
            call.url.endsWith("/agent-sessions") &&
            call.init?.method === "POST",
        ),
      ).toBe(true),
    );
    const request = recorder.calls.find(
      (call) =>
        call.url.endsWith("/agent-sessions") && call.init?.method === "POST",
    );
    if (request === undefined) throw new Error("open request was not sent");
    expect(bodyOf(request.init)).toMatchObject({
      task_id: TASK_ID,
      adapter_kind: "codex_cli",
      handoff_id: HANDOFF_ID,
      executable: "codex",
      model: null,
    });
    expect(headersOf(request.init)["Idempotency-Key"]).toBeTruthy();
    expect(await screen.findByText(/session ouverte/)).toBeInTheDocument();
  });

  it("affiche l’état agent absent quand les sessions sont indisponibles", async () => {
    installFetch(({ url, init }) => {
      const method = init?.method ?? "GET";
      if (url.endsWith("/agent-sessions") && method === "GET") {
        return errorResponse(503, "agent_sessions_unavailable");
      }
      if (url.endsWith("/tasks") && method === "GET") {
        return jsonResponse(taskPage());
      }
      return errorResponse(500, "unexpected_request");
    });

    renderPanel();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Agent absent : les sessions d’opérateurs sont indisponibles \(503\)/,
    );
  });
});
