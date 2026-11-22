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
import type { AgentSessionView } from "../api/agentSessions";
import { TaskCard } from "./TaskCard";
import { TaskWorkspace } from "./TaskWorkspace";
import {
  HANDOFF_ID,
  INVESTIGATION_ID,
  makeSession,
  makeTask,
} from "./testFixtures";

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderCard(
  task = makeTask(),
  workspaceAvailable: boolean | null = true,
  sessions: AgentSessionView[] = [],
) {
  return render(
    <TaskCard
      investigationId={INVESTIGATION_ID}
      task={task}
      allTasks={[task]}
      sessions={sessions}
      workspaceAvailable={workspaceAvailable}
    />,
    { wrapper: wrapperFor(createTestQueryClient()) },
  );
}

function sessionCapabilities() {
  return {
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
  };
}

function assignedOperatorTask() {
  return makeTask({
    status: "assigned",
    assigned_actor_kind: "operator",
    assigned_actor_id: "operator:codex_cli",
    handoff_ref: HANDOFF_ID,
  });
}

function workspaceStatus() {
  return {
    investigation_id: INVESTIGATION_ID,
    workspace_state: "bound",
    workspace_available: true,
    availability_code: "available",
    availability_reason: null,
    binding: null,
    manifest: null,
    generation: 1,
    cursor: 1,
    counts: {
      files: 0,
      human_notes: 0,
      unresolved_conflicts: 0,
      policy_violations: 0,
      plans: 0,
      tasks: 0,
      findings: 0,
    },
    lastly_observed_at: null,
  };
}

describe("TaskCard actions", () => {
  it("n’affiche aucune action non exécutable sur une tâche terminale", () => {
    const completed = makeTask({
      status: "completed",
      result_verification_status: "accepted",
    });
    renderCard(completed);

    expect(
      screen.queryByRole("button", { name: "Attribuer à Codex" }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Je prends cette tâche" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "Interrompre" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Ajouter une note" }),
    ).toBeNull();
    expect(screen.getByText(/Terminée · version 2/)).toBeInTheDocument();
  });

  it("n’affiche pas les attributions quand le workspace n’est pas prouvé", () => {
    renderCard(makeTask(), null);

    expect(
      screen.queryByRole("button", { name: "Attribuer à Codex" }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Je prends cette tâche" }),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: "Annuler la tâche" }),
    ).toBeEnabled();
  });

  it("n’offre les actions de session que si une session réelle les permet", () => {
    renderCard(
      makeTask({
        status: "running",
        assigned_actor_kind: "operator",
        assigned_actor_id: "operator:codex_cli",
      }),
    );

    expect(screen.getByRole("button", { name: "Interrompre" })).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: "Interrompre la session" }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Reprendre la session" }),
    ).toBeNull();
  });
});

describe("TaskCard CAS", () => {
  it("traite stale_task_version par un rechargement et un message", async () => {
    const user = userEvent.setup();
    let stale = false;
    installFetch(({ url, init }) => {
      const method = init?.method ?? "GET";
      if (url.endsWith("/tasks") && method === "GET") {
        return jsonResponse({
          items: [makeTask({ status_version: stale ? 3 : 2 })],
          total: 1,
        });
      }
      if (url.endsWith("/transfer") && method === "POST") {
        stale = true;
        return errorResponse(409, "stale_task_version");
      }
      if (url.endsWith("/workspace") && method === "GET") {
        return jsonResponse(workspaceStatus());
      }
      if (url.endsWith("/agent-sessions") && method === "GET") {
        return jsonResponse([]);
      }
      return errorResponse(500, "unexpected_request");
    });

    render(<TaskWorkspace investigationId={INVESTIGATION_ID} />, {
      wrapper: wrapperFor(createTestQueryClient()),
    });

    expect(await screen.findByText(/version 2/)).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Je prends cette tâche" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /version obsolète/,
    );
    expect(await screen.findByText(/version 3/)).toBeInTheDocument();
  });
});

describe("TaskCard session launch", () => {
  it("opens the probed session with the capability executable and no free model", async () => {
    const user = userEvent.setup();
    const recorder = installFetch(({ url, init }) => {
      const method = init?.method ?? "GET";
      if (url.includes("/agent-sessions/capabilities")) {
        return jsonResponse(sessionCapabilities());
      }
      if (url.endsWith("/agent-sessions") && method === "POST") {
        return jsonResponse({ session: makeSession() });
      }
      return errorResponse(500, "unexpected_request");
    });

    renderCard(assignedOperatorTask());

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
    expect(bodyOf(request.init)).toEqual({
      task_id: assignedOperatorTask().id,
      adapter_kind: "codex_cli",
      handoff_id: HANDOFF_ID,
      executable: "codex",
      model: null,
    });
    expect(headersOf(request.init)["Idempotency-Key"]).toBeTruthy();
    expect(await screen.findByText(/session ouverte/)).toBeInTheDocument();
  });

  it("hides the action with the typed reason when the host probe fails", async () => {
    installFetch(({ url }) =>
      url.includes("/agent-sessions/capabilities")
        ? errorResponse(503, "workspace_local_unavailable")
        : errorResponse(500, "unexpected_request"),
    );

    renderCard(assignedOperatorTask());

    expect(
      await screen.findByText(
        /Ouverture de session masquée : Agent hôte absent/,
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Ouvrir la session Codex CLI" }),
    ).toBeNull();
  });

  it("renders the typed external-disclosure refusal", async () => {
    const user = userEvent.setup();
    installFetch(({ url, init }) => {
      if (url.includes("/agent-sessions/capabilities")) {
        return jsonResponse(sessionCapabilities());
      }
      if (url.endsWith("/agent-sessions") && init?.method === "POST") {
        return errorResponse(403, "agent_session_external_llm_not_allowed");
      }
      return errorResponse(500, "unexpected_request");
    });

    renderCard(assignedOperatorTask());
    await user.click(
      await screen.findByRole("button", {
        name: "Ouvrir la session Codex CLI",
      }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Divulgation externe refusée/,
    );
  });

  it("does not offer an opening when a session already exists", () => {
    installFetch(() => errorResponse(500, "unexpected_request"));

    renderCard(assignedOperatorTask(), true, [makeSession()]);

    expect(
      screen.queryByRole("button", { name: "Ouvrir la session Codex CLI" }),
    ).toBeNull();
  });
});

describe("TaskCard keyboard", () => {
  it("déclenche une attribution au clavier avec CAS et idempotence", async () => {
    const user = userEvent.setup();
    const recorder = installFetch(({ url, init }) => {
      if (url.endsWith("/transfer") && (init?.method ?? "GET") === "POST") {
        return jsonResponse({ handoff: { version: 1 } });
      }
      return errorResponse(500, "unexpected_request");
    });

    renderCard();

    const target = screen.getByRole("button", { name: "Attribuer à Codex" });
    for (let index = 0; index < 8; index += 1) {
      if (document.activeElement === target) break;
      await user.tab();
    }
    expect(target).toHaveFocus();

    await user.keyboard("{Enter}");

    await waitFor(() =>
      expect(
        recorder.calls.some((call) => call.url.endsWith("/transfer")),
      ).toBe(true),
    );
    const request = recorder.calls.find((call) =>
      call.url.endsWith("/transfer"),
    );
    expect(request).toBeDefined();
    if (request === undefined) throw new Error("transfer was not sent");
    expect(bodyOf(request.init)).toMatchObject({
      to_actor_kind: "operator",
      to_adapter_kind: "codex_cli",
      open_session: false,
      expected_status_version: 2,
    });
    expect(headersOf(request.init)["Idempotency-Key"]).toBeTruthy();
  });
});
