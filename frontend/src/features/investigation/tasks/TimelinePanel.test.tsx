import { render, screen } from "@testing-library/react";
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

function renderTimeline() {
  installFetch(({ url }) =>
    url.includes("/timeline")
      ? jsonResponse(timelinePage())
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
});
