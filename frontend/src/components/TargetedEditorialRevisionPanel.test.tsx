import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { ArtifactResponse } from "../api/production";
import { TargetedEditorialRevisionPanel } from "./TargetedEditorialRevisionPanel";

const api = vi.hoisted(() => ({
  acquireSourceAddition: vi.fn(),
  applyTargetedEditorialRevision: vi.fn(),
  getTargetedEditorialRevisionLineage: vi.fn(),
  previewTargetedEditorialRevision: vi.fn(),
  recordSourceAdditionCandidate: vi.fn(),
  selectSourceAddition: vi.fn(),
}));

vi.mock("../api/production", () => api);

afterEach(() => vi.unstubAllGlobals());
beforeEach(() => {
  vi.clearAllMocks();
  api.acquireSourceAddition.mockResolvedValue({ addition: { state: "ready" } });
  api.applyTargetedEditorialRevision.mockResolvedValue({});
  api.getTargetedEditorialRevisionLineage.mockResolvedValue({
    artifact_id: "synthesis-1",
    production_run_id: "run-1",
    revisions: [],
    paragraph_provenance: {},
    element_provenance: {},
  });
  api.previewTargetedEditorialRevision.mockResolvedValue({});
  api.recordSourceAdditionCandidate.mockResolvedValue({
    addition: { state: "candidate" },
  });
  api.selectSourceAddition.mockResolvedValue({
    addition: { state: "selected" },
  });
});

const artifact: ArtifactResponse = {
  artifact_id: "synthesis-1",
  production_run_id: "run-1",
  stage: "synthesis",
  version: 1,
  input_hash: "a".repeat(64),
  canonical_sha256: "b".repeat(64),
  status: "verified",
  metadata: {},
  revision_targets: [
    {
      id: "sections-target",
      label: "Sections",
      summary: "Sections actuelles",
      target: {
        kind: "sections",
        stable_ids: [],
        parent_id: null,
        anchor_id: null,
        insert_after: false,
      },
      operations: ["insert_section"],
    },
  ],
  canonical_content: null,
};

it("transmet l’intention descriptive choisie pour une source opérateur", async () => {
  vi.stubGlobal("crypto", { randomUUID: () => "request-1" });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <TargetedEditorialRevisionPanel
        subjectId="subject-1"
        stage="synthesis"
        artifact={artifact}
      />
    </QueryClientProvider>,
  );

  fireEvent.click(
    screen.getByRole("radio", { name: "Autoriser l’extension de sources" }),
  );
  expect(
    screen.getByLabelText("Intention d’extraction de la source"),
  ).toHaveValue("indicators_only");
  fireEvent.change(screen.getByLabelText("URL de la source opérateur"), {
    target: { value: "https://github.example/NanoDump" },
  });
  fireEvent.change(
    screen.getByLabelText("Intention d’extraction de la source"),
    {
      target: { value: "descriptive_facts" },
    },
  );
  fireEvent.change(screen.getByLabelText("Motif de sélection de la source"), {
    target: { value: "Documenter le fonctionnement de l’outil" },
  });
  fireEvent.click(
    screen.getByRole("button", { name: "Archiver et préparer la source" }),
  );

  await waitFor(() =>
    expect(api.recordSourceAdditionCandidate).toHaveBeenCalledWith(
      "subject-1",
      expect.objectContaining({
        production_run_id: "run-1",
        request_id: "request-1",
        url: "https://github.example/NanoDump",
        extraction_intent: "descriptive_facts",
      }),
    ),
  );
  expect(api.selectSourceAddition).toHaveBeenCalled();
  expect(api.acquireSourceAddition).toHaveBeenCalled();
});
