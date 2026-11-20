import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { EditorialWorkspaceSnapshot } from "../../api/editorialWorkspace";
import { EditorialTopBar } from "./EditorialTopBar";

const baseSnapshot: EditorialWorkspaceSnapshot = {
  subject_id: "subject-1",
  production_run_id: "run-1",
  publication_kind: "article",
  synthesis_artifact_id: "synthesis-1",
  synthesis_version: 1,
  synthesis_canonical_sha256: "a".repeat(64),
  enrichment_artifact_id: null,
  enrichment_version: null,
  publication_artifact_id: null,
  publication_version: null,
  pipeline_generation: 1,
  blocks: [],
  enrichments: [],
  document_outline: [],
  capabilities: {
    can_discuss: false,
    can_preview_revision: false,
    manual_edit: false,
    can_add_enrichment: false,
    can_preview_pdf: false,
  },
  preview_status: "READY",
  impact_status: "none",
  pdf_status: "CURRENT",
  snapshot_id: "snapshot-1",
};

describe("EditorialTopBar render state", () => {
  it.each([
    ["READY", "À jour"],
    ["IN_PROGRESS", "Aperçu en cours"],
    ["STALE", "PDF obsolète"],
    ["FAILED", "PDF indisponible"],
  ] as const)(
    "shows the French label for %s without an impact",
    (status, label) => {
      render(
        <EditorialTopBar
          subjectId="subject-1"
          snapshot={{ ...baseSnapshot, preview_status: status }}
          stale={false}
          menuOpen={false}
          technicalOpen={false}
          onToggleMenu={vi.fn()}
          onOpenTechnicalDetails={vi.fn()}
          onTechnicalOpenChange={vi.fn()}
          onRefresh={vi.fn()}
        />,
      );

      expect(screen.getByRole("status")).toHaveTextContent(label);
      expect(screen.queryByText("Impact en attente")).not.toBeInTheDocument();
    },
  );

  it("labels refresh as a status refresh and describes the automatic retry", () => {
    render(
      <EditorialTopBar
        subjectId="subject-1"
        snapshot={{
          ...baseSnapshot,
          preview_status: "STALE",
          impact_status: "failed",
          pdf_status: "STALE",
        }}
        stale
        menuOpen={false}
        technicalOpen={false}
        onToggleMenu={vi.fn()}
        onOpenTechnicalDetails={vi.fn()}
        onTechnicalOpenChange={vi.fn()}
        onRefresh={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("button", { name: "Actualiser le statut" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/retenté automatiquement/i)).toBeInTheDocument();
    expect(screen.queryByText(/régénérez l’aperçu/i)).not.toBeInTheDocument();
  });
});
