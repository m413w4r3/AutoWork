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
import { TaskCard } from "./TaskCard";
import { TaskWorkspace } from "./TaskWorkspace";
import { INVESTIGATION_ID, makeTask } from "./testFixtures";

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderCard(
  task = makeTask(),
  workspaceAvailable: boolean | null = true,
) {
  return render(
    <TaskCard
      investigationId={INVESTIGATION_ID}
      task={task}
      allTasks={[task]}
      sessions={[]}
      workspaceAvailable={workspaceAvailable}
    />,
    { wrapper: wrapperFor(createTestQueryClient()) },
  );
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
