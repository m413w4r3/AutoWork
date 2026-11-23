import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createTestQueryClient,
  errorResponse,
  installFetch,
  jsonResponse,
  wrapperFor,
} from "../api/testSupport";
import { INVESTIGATION_ID } from "../tasks/testFixtures";
import { PlanTasksView } from "./index";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("PlanTasksView", () => {
  it("assemble le plan, le poste de travail et l’activité globale repliable", async () => {
    installFetch(({ url }) => {
      if (url.endsWith(`/api/investigations/${INVESTIGATION_ID}`)) {
        return jsonResponse({ version: 1 });
      }
      if (url.endsWith("/plans")) {
        return jsonResponse({ current_version: null, plans: [] });
      }
      if (url.endsWith("/consultations")) {
        return jsonResponse({
          investigation_id: INVESTIGATION_ID,
          consultations: [],
        });
      }
      if (url.endsWith("/tasks")) return jsonResponse({ items: [], total: 0 });
      if (url.endsWith("/workspace")) {
        return jsonResponse({
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
        });
      }
      if (url.endsWith("/agent-sessions")) return jsonResponse([]);
      if (url.endsWith("/timeline")) {
        return jsonResponse({
          items: [],
          next_cursor: null,
          has_more: false,
        });
      }
      return errorResponse(500, "unexpected_request");
    });

    render(<PlanTasksView investigationId={INVESTIGATION_ID} />, {
      wrapper: wrapperFor(createTestQueryClient()),
    });

    expect(
      await screen.findByRole("heading", { name: "Plan et tâches", level: 2 }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Plan Bridge", level: 3 }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Tâches", level: 3 }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Activité globale", { selector: "summary" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Opérateurs actifs" }),
    ).toBeNull();
    expect(
      screen.getByRole("region", { name: "Détail de la tâche sélectionnée" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByText(/Aucun plan pour cette enquête/),
    ).toBeInTheDocument();
    expect(await screen.findByText(/Aucune tâche\./)).toBeInTheDocument();
  });
});
