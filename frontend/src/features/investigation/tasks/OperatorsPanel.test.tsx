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
import { OperatorsPanel } from "./OperatorsPanel";
import {
  HANDOFF_ID,
  INVESTIGATION_ID,
  makeSession,
  makeSummary,
  makeSummaryInstruction,
  makeSummaryOperation,
  makeTask,
  makeZeroBudget,
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
  it("scopes agent messages to the selected task and confirms session interruption separately", async () => {
    const user = userEvent.setup();
    const task = makeTask({
      status: "running",
      assigned_actor_kind: "operator",
      assigned_actor_id: "operator:codex_cli",
    });
    const otherTask = makeTask({
      id: "99999999-9999-4999-8999-999999999999",
      task_key: "lead-other",
      title: "Autre tâche",
    });
    const selectedSession = makeSession();
    const otherSession = makeSession({
      id: "88888888-8888-4888-8888-888888888888",
      task_id: otherTask.id,
      adapter_kind: "claude_code",
    });
    const recorder = installFetch(({ url, init }) => {
      if (url.endsWith("/summary")) return jsonResponse(makeSummary());
      if (url.endsWith("/interrupt") && init?.method === "POST") {
        return jsonResponse(makeSession({ state: "interrupt_requested" }));
      }
      return errorResponse(500, "unexpected_request");
    });

    render(
      <OperatorsPanel
        investigationId={INVESTIGATION_ID}
        taskId={task.id}
        task={task}
        sessionContext={[selectedSession, otherSession]}
      />,
      { wrapper: wrapperFor(createTestQueryClient()) },
    );

    const context = screen.getByRole("region", {
      name: "Contexte agent de la tâche",
    });
    expect(
      await within(context).findByText("Codex CLI · Vérifier le domaine C2"),
    ).toBeInTheDocument();
    expect(within(context).queryByText(/Autre tâche/)).toBeNull();
    expect(within(context).queryByText(selectedSession.task_id)).toBeNull();
    await user.click(
      within(context).getByText("Plus d’actions", { selector: "summary" }),
    );
    await user.click(
      within(context).getByRole("button", { name: "Interrompre la session" }),
    );
    expect(recorder.calls.some((call) => call.url.endsWith("/interrupt"))).toBe(
      false,
    );
    await user.type(
      within(context).getByLabelText(
        "Motif d’interruption de la session (obligatoire)",
      ),
      "Arrêt demandé par l’analyste",
    );
    await user.click(
      within(context).getByRole("button", {
        name: "Confirmer l’interruption de la session",
      }),
    );

    await waitFor(() =>
      expect(
        recorder.calls.some((call) => call.url.endsWith("/interrupt")),
      ).toBe(true),
    );
    const request = recorder.calls.find((call) =>
      call.url.endsWith("/interrupt"),
    );
    expect(request).toBeDefined();
    if (request === undefined)
      throw new Error("session interruption was not sent");
    expect(bodyOf(request.init)).toEqual({
      reason: "Arrêt demandé par l’analyste",
    });
  });

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
      if (url.endsWith("/summary") && method === "GET") {
        return jsonResponse(makeSummary());
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
    const sessionHeader = screen
      .getByText("Codex CLI · Vérifier le domaine C2")
      .closest("header");
    expect(
      within(sessionHeader as HTMLElement).getByText("En cours"),
    ).toHaveClass("m24-badge--running");
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
      if (url.endsWith("/summary") && method === "GET") {
        return jsonResponse(makeSummary());
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
      if (url.endsWith("/summary") && method === "GET") {
        return jsonResponse(makeSummary());
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

  it("affiche opérations, budget, broker, passation et instructions du résumé", async () => {
    const operation = makeSummaryOperation({
      tool_name: "advisor.consult",
      status: "needs_review",
      reserved_budget: { bridge_turns: 1 },
      created_at: "2026-11-22T10:04:00Z",
    });
    const summary = makeSummary({
      operations: {
        items: [
          operation,
          makeSummaryOperation({
            invocation_id: "88888888-8888-4888-8888-888888888888",
            consumed_budget: { vt_units: 6 },
            reserved_budget: { vt_units: 10 },
            created_at: "2026-11-22T10:01:00Z",
          }),
        ],
        total: 2,
        limit: 100,
        truncated: false,
      },
      budget: {
        session: {
          invocation_count: 2,
          consumed: { ...makeZeroBudget(), vt_units: 6 },
          reserved: { ...makeZeroBudget(), vt_units: 10, bridge_turns: 1 },
        },
        envelope: {
          present: true,
          version: 2,
          approved_at: "2026-11-22T09:30:00Z",
          limits: { ...makeZeroBudget(), bridge_turns: 4, vt_units: 100 },
          consumed: { ...makeZeroBudget(), vt_units: 6 },
          reserved: { ...makeZeroBudget(), vt_units: 5 },
          remaining: { ...makeZeroBudget(), bridge_turns: 3, vt_units: 89 },
        },
      },
      broker: {
        derived_from: "tool_invocations_journal",
        last_signed_call: operation,
        signed_call_count: 2,
        needs_review_count: 1,
        reconciliation_required: true,
      },
      handoff: {
        handoff_id: HANDOFF_ID,
        version: 2,
        sha256: "b".repeat(64),
        created_at: "2026-11-22T09:20:00Z",
        from_actor: { kind: "human", actor_id: "analyst:test" },
        to_actor: { kind: "operator", actor_id: "operator:codex_cli" },
        freshness: { checked: true, state: "fresh", diffs: [] },
      },
      instructions: {
        items: [
          makeSummaryInstruction({
            state: "queued",
            text: "Confirme le protocole C2.",
            queue_position: 1,
            delivered_at: null,
            acknowledged_at: null,
          }),
        ],
        total: 1,
        limit: 20,
        truncated: false,
      },
    });
    installFetch(({ url, init }) => {
      const method = init?.method ?? "GET";
      if (url.endsWith("/agent-sessions") && method === "GET") {
        return jsonResponse([makeSession()]);
      }
      if (url.endsWith("/tasks") && method === "GET") {
        return jsonResponse(taskPage());
      }
      if (url.endsWith("/summary") && method === "GET") {
        return jsonResponse(summary);
      }
      return errorResponse(500, "unexpected_request");
    });

    renderPanel();

    expect(
      await screen.findByText("Opérations journalisées (2)"),
    ).toBeInTheDocument();
    expect(screen.getByText("advisor.consult")).toBeInTheDocument();
    expect(screen.getByText("vt.lookup")).toBeInTheDocument();
    expect(
      screen.getByText(/Unités VirusTotal : 6 consommé, 10 réservé/),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Budget de la session et enveloppe"),
    ).toBeInTheDocument();
    expect(screen.getByText(/plafond 100 · restant 89/)).toBeInTheDocument();
    expect(screen.getByText(/plafond 4 · restant 3/)).toBeInTheDocument();
    expect(screen.getByText("Dérivé du journal")).toBeInTheDocument();
    expect(
      screen.getByText(/Aucun état broker en direct n’est lu/),
    ).toBeInTheDocument();
    expect(screen.getByText("Appels signés").parentElement).toHaveTextContent(
      "2",
    );
    expect(
      screen.getByText("Revue humaine requise", { selector: "dt" })
        .parentElement,
    ).toHaveTextContent("1 — réconciliation requise");

    const handoffSection = screen
      .getByText("Dernière passation")
      .closest("section");
    expect(handoffSection).toHaveTextContent(/version 2/);
    expect(handoffSection).toHaveTextContent(/bbbbbbbbbbbb…/);
    expect(handoffSection).toHaveTextContent(/À jour/);

    expect(screen.getByText("Instructions humaines (1)")).toBeInTheDocument();
    expect(screen.getByText("Instruction en attente :")).toBeInTheDocument();
    // Once as the pending instruction, once in the instruction history.
    expect(screen.getAllByText("Confirme le protocole C2.")).toHaveLength(2);
    expect(screen.getByText("eeeeeeeeeeee…")).toBeInTheDocument();
    expect(screen.queryByText(/Non exposé/)).not.toBeInTheDocument();
  });

  it("traduit les états transitoires de livraison d’instruction", async () => {
    const summary = makeSummary({
      instructions: {
        items: [
          makeSummaryInstruction({
            instruction_id: "99999999-9999-4999-8999-999999999999",
            state: "delivering",
            text: "Confirme le domaine C2.",
            queue_position: 1,
            delivered_at: null,
            acknowledged_at: null,
          }),
          makeSummaryInstruction({
            instruction_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            state: "delivery_unknown",
            text: "Relève les IOC réseau.",
            queue_position: 0,
            delivered_at: null,
            acknowledged_at: null,
          }),
        ],
        total: 2,
        limit: 20,
        truncated: false,
      },
    });
    installFetch(({ url, init }) => {
      const method = init?.method ?? "GET";
      if (url.endsWith("/agent-sessions") && method === "GET") {
        return jsonResponse([makeSession()]);
      }
      if (url.endsWith("/tasks") && method === "GET") {
        return jsonResponse(taskPage());
      }
      if (url.endsWith("/summary") && method === "GET") {
        return jsonResponse(summary);
      }
      return errorResponse(500, "unexpected_request");
    });

    renderPanel();

    expect(
      await screen.findByText("Instruction en attente :"),
    ).toBeInTheDocument();
    // Once as the pending instruction, once in the instruction history.
    expect(screen.getAllByText("Livraison en cours")).toHaveLength(2);
    expect(screen.getByText("Livraison inconnue")).toBeInTheDocument();
  });

  it("affiche les états vides du résumé sans inventer de données", async () => {
    installFetch(({ url, init }) => {
      const method = init?.method ?? "GET";
      if (url.endsWith("/agent-sessions") && method === "GET") {
        return jsonResponse([makeSession()]);
      }
      if (url.endsWith("/tasks") && method === "GET") {
        return jsonResponse(taskPage());
      }
      if (url.endsWith("/summary") && method === "GET") {
        return jsonResponse(makeSummary());
      }
      return errorResponse(500, "unexpected_request");
    });

    renderPanel();

    expect(
      await screen.findByText(
        "Aucune opération journalisée pour cette session.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Aucune consommation de budget enregistrée pour cette session.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Aucune enveloppe approuvée pour cette enquête."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Dernier appel signé").parentElement,
    ).toHaveTextContent("Aucun");
    expect(
      screen.getByText("Aucune passation liée à cette session."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Aucune instruction en attente."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Aucune instruction humaine pour cette session."),
    ).toBeInTheDocument();
  });

  it("signale un résumé indisponible sans masquer la session", async () => {
    installFetch(({ url, init }) => {
      const method = init?.method ?? "GET";
      if (url.endsWith("/agent-sessions") && method === "GET") {
        return jsonResponse([makeSession()]);
      }
      if (url.endsWith("/tasks") && method === "GET") {
        return jsonResponse(taskPage());
      }
      if (url.endsWith("/summary") && method === "GET") {
        return errorResponse(503, "agent_sessions_unavailable");
      }
      return errorResponse(500, "unexpected_request");
    });

    renderPanel();

    expect(
      await screen.findByText("Codex CLI · Vérifier le domaine C2"),
    ).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Résumé de session indisponible — Agent absent/,
    );
  });

  it("affiche l’attente du résumé de session", async () => {
    installFetch(({ url, init }) => {
      const method = init?.method ?? "GET";
      if (url.endsWith("/agent-sessions") && method === "GET") {
        return jsonResponse([makeSession()]);
      }
      if (url.endsWith("/tasks") && method === "GET") {
        return jsonResponse(taskPage());
      }
      if (url.endsWith("/summary") && method === "GET") {
        return new Promise<Response>(() => {});
      }
      return errorResponse(500, "unexpected_request");
    });

    renderPanel();

    expect(
      await screen.findByText("Chargement du résumé de session…"),
    ).toBeInTheDocument();
  });
});
