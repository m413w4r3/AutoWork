import { render, screen, waitFor, within } from "@testing-library/react";
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
  TASK_ID,
  makeSession,
  makeSummary,
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

function renderWorkspace(
  tasks: ReturnType<typeof makeTask>[],
  timelineItems: unknown[] = [],
  sessionItems: AgentSessionView[] = [],
) {
  installFetch(({ url }) => {
    if (url.endsWith("/tasks"))
      return jsonResponse({ items: tasks, total: tasks.length });
    if (url.endsWith("/agent-sessions")) return jsonResponse(sessionItems);
    if (url.endsWith("/workspace")) return jsonResponse(workspaceStatus());
    if (url.includes("/timeline"))
      return jsonResponse({
        items: timelineItems,
        next_cursor: null,
        has_more: false,
      });
    if (url.includes("/events"))
      return jsonResponse({ items: [], next_cursor: null, has_more: false });
    return errorResponse(500, "unexpected_request");
  });
  return render(<TaskWorkspace investigationId={INVESTIGATION_ID} />, {
    wrapper: wrapperFor(createTestQueryClient()),
  });
}

describe("TaskCard actions", () => {
  it("n’affiche aucune action non exécutable sur une tâche terminale", async () => {
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
    expect(
      screen.queryByRole("button", { name: "Interrompre la tâche" }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Ajouter une note" }),
    ).toBeNull();
    expect(screen.getAllByText("Terminée").length).toBeGreaterThan(0);
    const technicalSummary = screen.getByText("Détails techniques", {
      selector: "summary",
    });
    expect(technicalSummary.closest("details")).not.toHaveAttribute("open");
    await userEvent.setup().click(technicalSummary);
    expect(screen.getByText("2", { selector: "code" })).toBeVisible();
  });

  it("affiche l’adaptateur d’une assignation opérateur en libellé humain", () => {
    renderCard(assignedOperatorTask());

    expect(screen.getByText("Opérateur · Codex CLI")).toBeInTheDocument();
    expect(screen.queryByText(/operator:codex_cli/)).toBeNull();
  });

  it("masque les attributions sans workspace et demande une confirmation d’annulation dans Plus d’actions", async () => {
    const user = userEvent.setup();
    renderCard(makeTask(), null);

    expect(
      screen.queryByRole("button", { name: "Attribuer à Codex" }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Je prends cette tâche" }),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: "Annuler la tâche" }),
    ).not.toBeVisible();
    await user.click(
      screen.getByText("Plus d’actions", { selector: "summary" }),
    );
    await user.click(screen.getByRole("button", { name: "Annuler la tâche" }));
    expect(
      screen.getByLabelText("Motif d’annulation de la tâche (obligatoire)"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirmer" })).toBeDisabled();
  });

  it("confirme l’arrêt technique de la session séparément de la tâche", async () => {
    const user = userEvent.setup();
    renderCard(
      makeTask({
        status: "running",
        assigned_actor_kind: "operator",
        assigned_actor_id: "operator:codex_cli",
      }),
      true,
      [makeSession()],
    );

    const interruptTask = screen.queryByRole("button", {
      name: "Interrompre la tâche",
    });
    expect(interruptTask).not.toBeVisible();
    await user.click(screen.getByRole("tab", { name: "Agent et messages" }));
    const agentContext = screen.getByRole("region", {
      name: "Contexte agent de la tâche",
    });
    const sessionInterrupt = within(agentContext).queryByRole("button", {
      name: "Confirmer l’arrêt technique",
    });
    expect(sessionInterrupt).not.toBeVisible();
    await user.click(
      within(agentContext).getByText("Interrompre la session", {
        selector: "summary",
      }),
    );
    expect(
      within(agentContext).getByText(/Arrêt technique de la session/),
    ).toBeInTheDocument();
    await user.click(
      within(agentContext).getByRole("button", {
        name: "Confirmer l’arrêt technique",
      }),
    );
    expect(
      within(agentContext).getByLabelText(
        "Motif d’arrêt de la session (obligatoire)",
      ),
    ).toBeInTheDocument();
    expect(
      within(agentContext).getByRole("button", {
        name: "Confirmer l’arrêt de la session",
      }),
    ).toBeDisabled();
  });

  it("expose les sorties et le transfert depuis Agent et messages", async () => {
    const user = userEvent.setup();
    renderCard(
      makeTask({
        status: "running",
        assigned_actor_kind: "operator",
        assigned_actor_id: "operator:codex_cli",
      }),
      true,
      [makeSession()],
    );

    await user.click(screen.getByRole("tab", { name: "Agent et messages" }));
    const agentContext = screen.getByRole("region", {
      name: "Contexte agent de la tâche",
    });
    await user.click(
      within(agentContext).getByRole("button", { name: "Transférer" }),
    );
    expect(screen.getByText("Destination du transfert")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Annuler" }));
    await user.click(
      within(agentContext).getByRole("button", { name: "Voir les sorties" }),
    );

    expect(
      screen.getByRole("tab", { name: "Résultats et preuves" }),
    ).toHaveAttribute("aria-selected", "true");
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

    const technicalSummary = await screen.findByText("Détails techniques", {
      selector: "summary",
    });
    await user.click(technicalSummary);
    expect(screen.getByText("2", { selector: "code" })).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Je prends cette tâche" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /version obsolète/,
    );
    expect(await screen.findByText("3", { selector: "code" })).toBeVisible();
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
    await user.click(screen.getByRole("tab", { name: "Agent et messages" }));

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

  it("garde le message d’ouverture quand la tâche passe en cours", async () => {
    const user = userEvent.setup();
    installFetch(({ url, init }) => {
      const method = init?.method ?? "GET";
      if (url.includes("/agent-sessions/capabilities")) {
        return jsonResponse(sessionCapabilities());
      }
      if (url.endsWith("/agent-sessions") && method === "POST") {
        return jsonResponse({ session: makeSession() });
      }
      return errorResponse(500, "unexpected_request");
    });

    const assigned = assignedOperatorTask();
    const { rerender } = renderCard(assigned);
    await user.click(screen.getByRole("tab", { name: "Agent et messages" }));
    await user.click(
      await screen.findByRole("button", {
        name: "Ouvrir la session Codex CLI",
      }),
    );
    expect(
      await screen.findByText(/session ouverte ; la tâche passe en cours/),
    ).toBeInTheDocument();

    // The refetch that follows the opening turns the task `running` and
    // exposes its session. The opening action unmounts; its feedback must stay
    // on the card, which survives the refetch.
    const running = makeTask({
      status: "running",
      status_version: assigned.status_version + 1,
      assigned_actor_kind: "operator",
      assigned_actor_id: "operator:codex_cli",
      handoff_ref: HANDOFF_ID,
    });
    rerender(
      <TaskCard
        investigationId={INVESTIGATION_ID}
        task={running}
        allTasks={[running]}
        sessions={[makeSession()]}
        workspaceAvailable={true}
      />,
    );

    expect(
      screen.getByText(/session ouverte ; la tâche passe en cours/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Ouvrir la session Codex CLI" }),
    ).toBeNull();
  });

  it("hides the action with the typed reason when the host probe fails", async () => {
    const user = userEvent.setup();
    installFetch(({ url }) =>
      url.includes("/agent-sessions/capabilities")
        ? errorResponse(503, "workspace_local_unavailable")
        : errorResponse(500, "unexpected_request"),
    );

    renderCard(assignedOperatorTask());
    await user.click(screen.getByRole("tab", { name: "Agent et messages" }));

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
    await user.click(screen.getByRole("tab", { name: "Agent et messages" }));
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

describe("TaskCard Bridge entry point", () => {
  it("ouvre le panneau Demander à Bridge sans envoyer de mutation", async () => {
    const user = userEvent.setup();
    const recorder = installFetch(({ url, init }) => {
      const method = init?.method ?? "GET";
      if (
        url === `/api/investigations/${INVESTIGATION_ID}` &&
        method === "GET"
      ) {
        return jsonResponse({ version: 5 });
      }
      if (
        url.endsWith("/consultations?task_id=" + makeTask().id) &&
        method === "GET"
      ) {
        return jsonResponse({
          investigation_id: INVESTIGATION_ID,
          consultations: [],
        });
      }
      return errorResponse(500, "unexpected_request");
    });

    renderCard();
    await user.click(screen.getByRole("tab", { name: "Agent et messages" }));

    const toggle = screen.getByRole("button", { name: "Demander à Bridge" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByLabelText("Question pour Bridge")).toBeNull();

    await user.click(toggle);

    expect(await screen.findByLabelText("Question pour Bridge")).toBeVisible();
    expect(
      screen.getByText(/ni résultat de tâche, ni preuve, ni décision/),
    ).toBeVisible();
    expect(
      recorder.calls.filter((call) => call.init?.method === "POST"),
    ).toEqual([]);
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

describe("TaskWorkspace list and selection", () => {
  it("ne répète pas l’adaptateur dans le libellé d’acteur d’une tâche active", async () => {
    const task = assignedOperatorTask();
    renderWorkspace([task], [], [makeSession({ task_id: task.id })]);

    const list = await screen.findByRole("list", { name: "Liste des tâches" });
    const row = within(list).getByRole("button", {
      name: /Vérifier le domaine C2/,
    });
    expect(row).toHaveTextContent("Acteur : Opérateur · Codex CLI");
    expect(row).not.toHaveTextContent("Codex CLI · Codex CLI");
  });

  it("keeps status, priority, actor and text filters", async () => {
    const user = userEvent.setup();
    const matching = makeTask({
      title: "Distinguer le C2 principal",
      status: "running",
      priority: "high",
      assigned_actor_kind: "operator",
      assigned_actor_id: "operator:codex_cli",
    });
    const other = makeTask({
      id: "55555555-5555-4555-8555-555555555555",
      task_key: "lead-2",
      title: "Vérifier un domaine secondaire",
      status: "ready",
      priority: "low",
      assigned_actor_kind: "human",
      assigned_actor_id: "human:analyst",
    });
    renderWorkspace([other, matching]);

    await user.selectOptions(screen.getByLabelText("Statut"), "running");
    await user.selectOptions(screen.getByLabelText("Priorité"), "high");
    await user.selectOptions(screen.getByLabelText("Assignation"), "operator");
    await user.type(screen.getByRole("searchbox", { name: "Recherche" }), "C2");

    const list = await screen.findByRole("list", { name: "Liste des tâches" });
    expect(
      within(list).getByText("Distinguer le C2 principal"),
    ).toBeInTheDocument();
    expect(
      within(list).queryByText("Vérifier un domaine secondaire"),
    ).toBeNull();
  });

  it("restores URL selection, preserves other params, supports Back and keyboard arrows", async () => {
    const user = userEvent.setup();
    const first = makeTask({
      title: "Première tâche",
      id: TASK_ID,
      status: "ready",
    });
    const second = makeTask({
      title: "Deuxième tâche",
      id: "55555555-5555-4555-8555-555555555555",
      task_key: "lead-2",
      status: "completed",
      updated_at: "2026-11-22T11:00:00Z",
    });
    window.history.replaceState(
      {},
      "",
      `/?edition=E2&vue=plan&task=${second.id}`,
    );
    renderWorkspace([first, second]);

    const list = await screen.findByRole("list", { name: "Liste des tâches" });
    const rows = () =>
      Array.from(
        list.querySelectorAll<HTMLButtonElement>("button[aria-pressed]"),
      );
    await waitFor(() =>
      expect(
        rows().find((row) => row.textContent?.includes("Deuxième tâche")),
      ).toHaveAttribute("aria-pressed", "true"),
    );
    expect(window.location.search).toContain("edition=E2");
    expect(window.location.search).toContain("vue=plan");

    const firstRow = rows().find((row) =>
      row.textContent?.includes("Première tâche"),
    );
    if (firstRow === undefined) throw new Error("first row was not rendered");
    await user.click(firstRow);
    expect(firstRow).toHaveAttribute("aria-pressed", "true");
    expect(window.location.search).toContain(`task=${first.id}`);
    await user.keyboard("{ArrowDown}");
    expect(
      rows().find((row) => row.textContent?.includes("Deuxième tâche")),
    ).toHaveAttribute("aria-pressed", "true");
    expect(window.location.search).toContain(`task=${second.id}`);

    await new Promise((resolve) => window.setTimeout(resolve, 0));
    window.history.back();
    await waitFor(() =>
      expect(
        rows().find((row) => row.textContent?.includes("Première tâche")),
      ).toHaveAttribute("aria-pressed", "true"),
    );
  });

  it("discards an unknown URL task and selects the default task", async () => {
    const missingId = "99999999-9999-4999-8999-999999999999";
    const task = makeTask();
    window.history.replaceState({}, "", `/?vue=plan&task=${missingId}`);
    renderWorkspace([task]);

    expect(
      await screen.findByText(/tâche indiquée dans l’URL est introuvable/i),
    ).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Liste des tâches" });
    expect(
      within(list).getByRole("button", { name: /Vérifier le domaine C2/ }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(new URL(window.location.href).searchParams.has("task")).toBe(false);
    expect(screen.queryByText(missingId)).toBeNull();
  });

  it("sorts 30 synthetic tasks and mounts only one detailed card without showing UUIDs in rows", async () => {
    const statuses = [
      ...Array.from({ length: 6 }, () => "completed" as const),
      ...Array.from({ length: 6 }, () => "blocked" as const),
      ...Array.from({ length: 6 }, () => "ready" as const),
      ...Array.from({ length: 6 }, () => "running" as const),
      ...Array.from({ length: 6 }, () => "awaiting_review" as const),
    ];
    const tasks = statuses.map((status, index) =>
      makeTask({
        id:
          index === 0
            ? TASK_ID
            : `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
        task_key: `lead-${index}`,
        title: `Tâche synthétique ${index}`,
        status,
        blocked_reason: status === "blocked" ? "dependency" : null,
        result_refs:
          status === "awaiting_review" ? [{ result: "pending" }] : [],
        priority: index % 2 === 0 ? "low" : "critical",
      }),
    );
    const { container } = renderWorkspace(tasks);

    const list = await screen.findByRole("list", { name: "Liste des tâches" });
    const rowButtons = Array.from(
      list.querySelectorAll<HTMLButtonElement>("button[aria-pressed]"),
    );
    expect(rowButtons).toHaveLength(30);
    expect(rowButtons[0]?.textContent).toContain("En revue");
    expect(rowButtons[6]?.textContent).toContain("En cours");
    expect(rowButtons[12]?.textContent).toContain("Prête");
    expect(rowButtons[18]?.textContent).toContain("Bloquée");
    expect(rowButtons[24]?.textContent).toContain("Terminée");
    expect(container.querySelectorAll("article.m24-task-card")).toHaveLength(1);
    expect(list.textContent).not.toContain(TASK_ID);
  });

  it("opens result details directly from the row", async () => {
    const user = userEvent.setup();
    const task = makeTask({
      status: "awaiting_review",
      result_refs: [{ source_url: "https://example.test/report" }],
    });
    renderWorkspace(
      [task],
      [
        {
          item_type: "contribution",
          object_id: "contribution-1",
          occurred_at: "2026-11-22T11:00:00Z",
          task_id: task.id,
          actor_kind: "operator",
          actor_id: "codex",
          summary: "future_event:whatever",
          evidence: false,
          refs: {
            finding_id: "finding-1",
            source_url: "https://example.test/source",
          },
          linked_event_ids: [],
          linked_observation_ids: [],
        },
      ],
    );

    await user.click(
      await screen.findByRole("button", {
        name: `Voir les résultats de ${task.title}`,
      }),
    );
    expect(
      screen.getByRole("tab", { name: "Résultats et preuves" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(
      screen
        .getAllByRole("link", { name: "Ouvrir la source" })
        .some(
          (link) => link.getAttribute("href") === "https://example.test/report",
        ),
    ).toBe(true);
    expect(
      await screen.findByText("Contribution agentique non vérifiée"),
    ).toBeInTheDocument();
    expect(screen.getByText("Activité enregistrée")).toBeInTheDocument();
    const resultItem = screen
      .getByText("Activité enregistrée")
      .closest("li.m24-result-evidence");
    expect(resultItem).not.toBeNull();
    const technicalDetails = within(resultItem as HTMLElement).getByText(
      "Détails techniques",
      { selector: "summary" },
    );
    expect(
      within(resultItem as HTMLElement).getByText("future_event:whatever"),
    ).not.toBeVisible();
    expect(
      within(resultItem as HTMLElement).getByText("finding-1"),
    ).not.toBeVisible();
    await user.click(technicalDetails);
    expect(
      within(resultItem as HTMLElement).getByText("future_event:whatever"),
    ).toBeVisible();
    expect(
      within(resultItem as HTMLElement).getByText("finding-1"),
    ).toBeVisible();
    expect(screen.queryByText("Contribution confirmée")).toBeNull();
  });

  it("provides shortcuts to pending decisions and active agent sessions", async () => {
    const user = userEvent.setup();
    const decision = makeTask({
      title: "Décision à prendre",
      status: "awaiting_review",
      result_refs: [{ result: "pending" }],
    });
    const active = makeTask({
      id: "55555555-5555-4555-8555-555555555555",
      task_key: "lead-2",
      title: "Session en cours",
      status: "running",
      assigned_actor_kind: "operator",
      assigned_actor_id: "operator:codex_cli",
    });
    renderWorkspace(
      [active, decision],
      [],
      [makeSession({ task_id: active.id })],
    );

    await user.click(
      await screen.findByRole("button", { name: "Nouvelles décisions (1)" }),
    );
    expect(
      screen.getByRole("tab", { name: "Résultats et preuves" }),
    ).toHaveAttribute("aria-selected", "true");
    await user.click(
      screen.getByRole("button", { name: "Sessions actives (1)" }),
    );
    expect(
      screen.getByRole("tab", { name: "Agent et messages" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(
      screen.getByRole("heading", { name: "Session en cours" }),
    ).toBeInTheDocument();
  });
});

describe("TaskCard detail subviews and agent messages", () => {
  it("keeps human task context visible and hides exact references in technical details", async () => {
    const task = makeTask({
      task_key: "lead-76739db1-exact",
      question: "Quelle infrastructure est utilisée ?",
      objective: "Identifier les serveurs de commande.",
      evidence_refs: ["8a8fa705-9e71-55cb-exact"],
      sample_refs: ["2ad3cc5e-exact-sha"],
      lease_expires_at: "2026-11-24T12:30:00Z",
    });
    renderCard(task);

    expect(screen.getByText(task.question)).toBeVisible();
    expect(screen.getByText(task.objective)).toBeVisible();
    expect(
      screen.getByText("1 preuve de départ · voir Résultats et preuves"),
    ).toBeVisible();
    expect(
      screen.getByRole("link", {
        name: "1 échantillon · voir Échantillons et résultats",
      }),
    ).toHaveAttribute("href", expect.stringContaining("vue=samples"));
    expect(screen.getByText(task.task_key)).not.toBeVisible();
    expect(screen.getByText(task.evidence_refs[0] ?? "")).not.toBeVisible();
    expect(screen.getByText(task.sample_refs[0] ?? "")).not.toBeVisible();

    const details = screen.getByText("Détails techniques", {
      selector: "summary",
    });
    const disclosure = details.closest("details");
    expect(disclosure).not.toHaveAttribute("open");
    await userEvent.setup().click(details);
    expect(screen.getByText(task.task_key)).toBeVisible();
    expect(screen.getByText(task.evidence_refs[0] ?? "")).toBeVisible();
    expect(screen.getByText(task.sample_refs[0] ?? "")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Copier la clé de tâche" }),
    ).toBeVisible();
  });

  it("provides Résumé, Résultats, Agent et messages, and Historique tabs", async () => {
    const user = userEvent.setup();
    installFetch(({ url }) => {
      if (url.includes("/timeline"))
        return jsonResponse({ items: [], next_cursor: null, has_more: false });
      if (url.includes("/events"))
        return jsonResponse({ items: [], next_cursor: null, has_more: false });
      if (url.endsWith("/summary")) return jsonResponse(makeSummary());
      return errorResponse(500, "unexpected_request");
    });
    renderCard();

    expect(screen.getByRole("tablist")).toBeInTheDocument();
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual([
      "Résumé",
      "Résultats et preuves",
      "Agent et messages",
      "Historique",
    ]);
    for (const tab of tabs) {
      expect(
        document.getElementById(tab.getAttribute("aria-controls") ?? ""),
      ).toHaveAttribute("role", "tabpanel");
    }
    await user.click(screen.getByRole("tab", { name: "Résultats et preuves" }));
    expect(screen.getByText("Résultat déclaré")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Agent et messages" }));
    expect(
      screen.getByRole("heading", { name: "Contexte agent de la tâche" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Historique" }));
    expect(
      await screen.findByRole("heading", { name: "Historique de la tâche" }),
    ).toBeInTheDocument();
  });

  it("sends an instruction from the selected task’s Agent et messages detail", async () => {
    const user = userEvent.setup();
    const task = makeTask({
      status: "running",
      assigned_actor_kind: "operator",
      assigned_actor_id: "operator:codex_cli",
    });
    const session = makeSession();
    const recorder = installFetch(({ url, init }) => {
      if (url.endsWith("/summary")) return jsonResponse(makeSummary());
      if (url.endsWith("/instructions") && init?.method === "POST") {
        return jsonResponse({
          state: "queued",
          queue_position: 1,
          delivered_live: false,
        });
      }
      return errorResponse(500, "unexpected_request");
    });
    renderCard(task, true, [session]);
    await user.click(screen.getByRole("tab", { name: "Agent et messages" }));
    await user.type(
      screen.getByLabelText(/Instruction à envoyer/),
      "Vérifie la source liée au finding.",
    );
    await user.click(
      screen.getByRole("button", { name: "Envoyer l’instruction" }),
    );

    expect(
      await screen.findByText(/en file persistante \(position 1\)/),
    ).toBeInTheDocument();
    const request = recorder.calls.find((call) =>
      call.url.endsWith("/instructions"),
    );
    expect(request).toBeDefined();
    if (request === undefined)
      throw new Error("agent instruction was not sent");
    expect(bodyOf(request.init)).toMatchObject({
      text: "Vérifie la source liée au finding.",
    });
  });
});
