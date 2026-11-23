import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createTestQueryClient,
  errorResponse,
  installFetch,
  jsonResponse,
  wrapperFor,
} from "../api/testSupport";
import { TimelinePanel } from "./TimelinePanel";
import { INVESTIGATION_ID, TASK_ID } from "./testFixtures";

afterEach(() => {
  vi.unstubAllGlobals();
});

function timelinePage() {
  return {
    items: [
      {
        item_type: "observation",
        object_id: "obs-1",
        occurred_at: "2026-11-22T10:00:00Z",
        task_id: null,
        actor_kind: "source",
        actor_id: "virustotal",
        summary: "vt_file_report:sample",
        evidence: true,
        refs: { observation_id: "obs-1", sha256: "d".repeat(64) },
        linked_event_ids: [],
        linked_observation_ids: [],
      },
      {
        item_type: "bridge_consultation",
        object_id: "inv-1",
        occurred_at: "2026-11-22T10:05:00Z",
        task_id: TASK_ID,
        actor_kind: "human",
        actor_id: "analyst:test",
        summary: "advisor.consult:succeeded",
        evidence: false,
        refs: { invocation_id: "inv-1", operation_key: "op-1" },
        linked_event_ids: [],
        linked_observation_ids: ["obs-2"],
      },
    ],
    next_cursor: null,
    has_more: false,
  };
}

const HANDOFF_SHA = "9".repeat(64);
const FILE_SHA = "e".repeat(64);
const TEXT_SHA = "f".repeat(64);
const DELIVERY_SHA = "0".repeat(64);

/** The four B7 working objects, shaped like `investigation_timeline.py`. */
function workingItems() {
  return [
    {
      item_type: "handoff",
      object_id: "hd-1",
      occurred_at: "2026-11-22T11:00:00Z",
      task_id: TASK_ID,
      actor_kind: "human",
      actor_id: "analyst:test",
      summary: "handoff:v2:human->operator",
      evidence: false,
      refs: {
        handoff_id: "hd-1",
        task_id: TASK_ID,
        version: "2",
        sha256: HANDOFF_SHA,
        predecessor_id: "hd-0",
      },
      linked_event_ids: [],
      linked_observation_ids: [],
    },
    {
      item_type: "contribution",
      object_id: "ct-1",
      occurred_at: "2026-11-22T11:05:00Z",
      task_id: TASK_ID,
      actor_kind: "operator",
      actor_id: "codex",
      summary: "contribution:finding:imported",
      evidence: false,
      refs: {
        contribution_id: "ct-1",
        workspace_id: "ws-1",
        workspace_revision_id: "rev-2",
        file_relpath: "notes/agents/task-1/finding.md",
        file_sha256: FILE_SHA,
        finding_id: "finding-1",
      },
      linked_event_ids: [],
      linked_observation_ids: [],
    },
    {
      item_type: "agent_session",
      object_id: "se-1",
      occurred_at: "2026-11-22T11:10:00Z",
      task_id: TASK_ID,
      actor_kind: "system",
      actor_id: "codex_cli",
      summary: "agent_session:codex_cli:awaiting_instruction",
      evidence: false,
      refs: {
        session_id: "se-1",
        task_id: TASK_ID,
        adapter_kind: "codex_cli",
        state: "awaiting_instruction",
        state_version: "3",
        handoff_id: "hd-1",
      },
      linked_event_ids: [],
      linked_observation_ids: [],
    },
    {
      item_type: "instruction",
      object_id: "in-1",
      occurred_at: "2026-11-22T11:15:00Z",
      task_id: TASK_ID,
      actor_kind: "human",
      actor_id: "analyst:test",
      summary: "instruction:queued",
      evidence: false,
      refs: {
        instruction_id: "in-1",
        session_id: "se-1",
        text_sha256: TEXT_SHA,
        delivery_attempts: "2",
        delivery_mode: "next_run",
        delivery_sha256: DELIVERY_SHA,
      },
      linked_event_ids: [],
      linked_observation_ids: [],
    },
  ];
}

function workingPage() {
  return {
    items: [...timelinePage().items, ...workingItems()],
    next_cursor: null,
    has_more: false,
  };
}

function renderTimeline(page: unknown = timelinePage()) {
  installFetch(({ url }) =>
    url.includes("/timeline")
      ? jsonResponse(page)
      : errorResponse(500, "unexpected_request"),
  );
  return render(<TimelinePanel investigationId={INVESTIGATION_ID} />, {
    wrapper: wrapperFor(createTestQueryClient()),
  });
}

describe("TimelinePanel", () => {
  it("étiquette les avis comme non-preuves et pointe l’objet exact", async () => {
    renderTimeline();

    expect(
      await screen.findByText("vt_file_report:sample"),
    ).toBeInTheDocument();
    expect(screen.getByText("Preuve canonique")).toBeInTheDocument();
    expect(screen.getByText("Avis Bridge — non preuve")).toBeInTheDocument();
    expect(screen.getAllByText("inv-1").length).toBeGreaterThan(0);
    expect(screen.getAllByText("obs-1").length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "Voir la tâche" })).toHaveAttribute(
      "href",
      `#task-${TASK_ID}`,
    );
  });

  it("filtre par type puis par statut de preuve", async () => {
    const user = userEvent.setup();
    renderTimeline();

    await screen.findByText("vt_file_report:sample");
    await user.selectOptions(
      screen.getByLabelText(/Type d’événement/),
      "bridge_consultation",
    );
    expect(screen.queryByText("vt_file_report:sample")).toBeNull();
    expect(screen.getByText("advisor.consult:succeeded")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText(/Type d’événement/), "all");
    await user.selectOptions(screen.getByLabelText(/Preuve/), "evidence");
    expect(screen.queryByText("advisor.consult:succeeded")).toBeNull();
    expect(screen.getByText("vt_file_report:sample")).toBeInTheDocument();
  });

  it("affiche les quatre objets de travail comme non probants", async () => {
    renderTimeline(workingPage());

    const handoff = await screen.findByText("handoff:v2:human->operator");
    const contribution = screen.getByText("contribution:finding:imported");
    const session = screen.getByText(
      "agent_session:codex_cli:awaiting_instruction",
    );
    const instruction = screen.getByText("instruction:queued");
    expect(
      within(handoff.closest("li") as HTMLElement).getByText("Passation"),
    ).toBeInTheDocument();
    expect(
      within(contribution.closest("li") as HTMLElement).getByText(
        "Contribution",
      ),
    ).toBeInTheDocument();
    expect(
      within(session.closest("li") as HTMLElement).getByText("Session d’agent"),
    ).toBeInTheDocument();
    expect(
      within(instruction.closest("li") as HTMLElement).getByText(
        "Instruction humaine",
      ),
    ).toBeInTheDocument();
    for (const label of [
      "Passation",
      "Contribution",
      "Session d’agent",
      "Instruction humaine",
    ]) {
      expect(screen.getByRole("option", { name: label })).toBeInTheDocument();
    }
    expect(
      screen.getAllByText("Non probant — élément de travail"),
    ).toHaveLength(4);
    expect(screen.getAllByText("Preuve canonique")).toHaveLength(1);
    expect(screen.getByText("Avis Bridge — non preuve")).toBeInTheDocument();

    // Persisted ids stay whole; digests are abbreviated.
    expect(screen.getAllByText("hd-1")).toHaveLength(2);
    expect(screen.getByText("ct-1")).toBeInTheDocument();
    expect(
      screen.getByText("notes/agents/task-1/finding.md"),
    ).toBeInTheDocument();
    expect(screen.getByText(`${"9".repeat(12)}…`)).toBeInTheDocument();
    expect(screen.getByText(`${"e".repeat(12)}…`)).toBeInTheDocument();
    expect(screen.getByText(`${"f".repeat(12)}…`)).toBeInTheDocument();
    expect(screen.getByText(`${"0".repeat(12)}…`)).toBeInTheDocument();

    // Closed-union ref values are translated, not shown as snake_case.
    expect(screen.getByText("Codex CLI")).toBeInTheDocument();
    expect(screen.getByText("Attente d’instruction")).toBeInTheDocument();
    expect(screen.getByText("Au prochain démarrage")).toBeInTheDocument();
    expect(screen.getByText("Tentatives de livraison")).toBeInTheDocument();

    // Handoffs and sessions expose `task_id` as a ref link; the other two
    // working objects only carry the envelope task and its footer link.
    const refTaskLinks = screen.getAllByRole("link", { name: TASK_ID });
    expect(refTaskLinks).toHaveLength(2);
    const taskLinks = screen.getAllByRole("link", { name: "Voir la tâche" });
    expect(taskLinks).toHaveLength(5);
    for (const link of [...refTaskLinks, ...taskLinks]) {
      expect(link).toHaveAttribute("href", `#task-${TASK_ID}`);
    }
  });

  it("filtre les objets de travail par type", async () => {
    const user = userEvent.setup();
    renderTimeline(workingPage());

    await screen.findByText("handoff:v2:human->operator");
    await user.selectOptions(
      screen.getByLabelText(/Type d’événement/),
      "instruction",
    );

    expect(screen.getByText("instruction:queued")).toBeInTheDocument();
    expect(screen.queryByText("handoff:v2:human->operator")).toBeNull();
    expect(screen.queryByText("contribution:finding:imported")).toBeNull();
    expect(
      screen.queryByText("agent_session:codex_cli:awaiting_instruction"),
    ).toBeNull();
    expect(
      screen.getByText(/1 entrée\(s\) affichée\(s\) sur 6\./),
    ).toBeInTheDocument();
  });

  it("affiche l’état vide", async () => {
    renderTimeline({ items: [], next_cursor: null, has_more: false });

    expect(
      await screen.findByText("Aucun événement pour cette enquête."),
    ).toBeInTheDocument();
  });

  it("affiche l’état d’erreur", async () => {
    installFetch(() => errorResponse(500, "timeline_unavailable"));
    render(<TimelinePanel investigationId={INVESTIGATION_ID} />, {
      wrapper: wrapperFor(createTestQueryClient()),
    });

    expect(
      await screen.findByText(
        /Chronologie indisponible : timeline_unavailable\./,
      ),
    ).toBeInTheDocument();
  });
});
