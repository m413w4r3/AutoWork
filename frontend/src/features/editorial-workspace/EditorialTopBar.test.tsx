import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
    can_regenerate_pdf: false,
  },
  preview_status: "READY",
  impact_status: "none",
  pdf_status: "CURRENT",
  pdf_artifact_id: null,
  pdf_artifact_version: null,
  pdf_artifact_input_hash: null,
  pdf_artifact_canonical_sha256: null,
  pdf_render_id: null,
  pdf_render_identity: null,
  pdf_error_code: null,
  pdf_error_message: null,
  pdf_url: null,
  snapshot_id: "snapshot-1",
};

describe("EditorialTopBar render state", () => {
  it.each([
    ["READY", "À jour"],
    ["GENERATING", "Génération du PDF en cours"],
    ["STALE", "PDF obsolète"],
    ["FAILED", "Erreur de rendu du PDF"],
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

  it("offers explicit PDF regeneration while the current preview is stale", () => {
    render(
      <EditorialTopBar
        subjectId="subject-1"
        snapshot={{
          ...baseSnapshot,
          preview_status: "STALE",
          capabilities: {
            ...baseSnapshot.capabilities,
            can_regenerate_pdf: true,
          },
        }}
        pdfStatus={{
          subject_id: "subject-1",
          status: "STALE",
          artifact_id: "old-pdf",
          artifact_version: 1,
          artifact_input_hash: "old-input",
          artifact_canonical_sha256: "old-sha",
          render_id: "old-render",
          render_identity: "old-identity",
          error_code: null,
          error_message: null,
          pdf_url: "/old.pdf",
        }}
        stale={false}
        menuOpen={false}
        technicalOpen={false}
        onToggleMenu={vi.fn()}
        onOpenTechnicalDetails={vi.fn()}
        onTechnicalOpenChange={vi.fn()}
        onRefresh={vi.fn()}
        onRegeneratePdf={vi.fn()}
      />,
    );

    expect(screen.getByText("PDF obsolète")).toHaveAttribute(
      "aria-label",
      expect.stringContaining("Régénérez l’aperçu"),
    );
    expect(
      screen.getByRole("button", { name: "Régénérer le PDF" }),
    ).toBeEnabled();
    expect(screen.queryByRole("link", { name: "Voir le PDF" })).toBeNull();
    expect(screen.getByText("old-identity")).not.toBeVisible();
  });

  it("shows the rendering error and keeps regeneration disabled while generating", () => {
    const { rerender } = render(
      <EditorialTopBar
        subjectId="subject-1"
        snapshot={{
          ...baseSnapshot,
          capabilities: {
            ...baseSnapshot.capabilities,
            can_regenerate_pdf: true,
          },
        }}
        pdfStatus={{
          subject_id: "subject-1",
          status: "GENERATING",
          artifact_id: null,
          artifact_version: null,
          artifact_input_hash: null,
          artifact_canonical_sha256: null,
          render_id: null,
          render_identity: null,
          error_code: null,
          error_message: null,
          pdf_url: null,
        }}
        stale={false}
        menuOpen={false}
        technicalOpen={false}
        onToggleMenu={vi.fn()}
        onOpenTechnicalDetails={vi.fn()}
        onTechnicalOpenChange={vi.fn()}
        onRefresh={vi.fn()}
        onRegeneratePdf={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("button", { name: "Régénérer le PDF" }),
    ).toBeDisabled();
    expect(screen.getByText("Génération du PDF en cours")).toBeVisible();

    rerender(
      <EditorialTopBar
        subjectId="subject-1"
        snapshot={{
          ...baseSnapshot,
          capabilities: {
            ...baseSnapshot.capabilities,
            can_regenerate_pdf: true,
          },
        }}
        pdfStatus={{
          subject_id: "subject-1",
          status: "FAILED",
          artifact_id: null,
          artifact_version: null,
          artifact_input_hash: null,
          artifact_canonical_sha256: null,
          render_id: null,
          render_identity: null,
          error_code: "typst_unavailable",
          error_message:
            "Vérifiez la configuration Typst puis relancez le rendu.",
          pdf_url: null,
        }}
        stale={false}
        menuOpen={false}
        technicalOpen={false}
        onToggleMenu={vi.fn()}
        onOpenTechnicalDetails={vi.fn()}
        onTechnicalOpenChange={vi.fn()}
        onRefresh={vi.fn()}
        onRegeneratePdf={vi.fn()}
      />,
    );

    expect(screen.getByText(/Vérifiez la configuration Typst/)).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Régénérer le PDF" }),
    ).toBeEnabled();
  });

  it("moves from stale through generating to a ready PDF without exposing its identity", () => {
    const { rerender } = render(
      <EditorialTopBar
        subjectId="subject-1"
        snapshot={{
          ...baseSnapshot,
          capabilities: {
            ...baseSnapshot.capabilities,
            can_regenerate_pdf: true,
          },
        }}
        pdfStatus={{
          subject_id: "subject-1",
          status: "STALE",
          artifact_id: "old-pdf",
          artifact_version: 1,
          artifact_input_hash: "old-input",
          artifact_canonical_sha256: "old-sha",
          render_id: "old-render",
          render_identity: "old-render-identity",
          error_code: null,
          error_message: null,
          pdf_url: null,
        }}
        stale={false}
        menuOpen={false}
        technicalOpen={false}
        onToggleMenu={vi.fn()}
        onOpenTechnicalDetails={vi.fn()}
        onTechnicalOpenChange={vi.fn()}
        onRefresh={vi.fn()}
        onRegeneratePdf={vi.fn()}
      />,
    );
    expect(screen.getByText("PDF obsolète")).toBeVisible();

    rerender(
      <EditorialTopBar
        subjectId="subject-1"
        snapshot={{
          ...baseSnapshot,
          capabilities: {
            ...baseSnapshot.capabilities,
            can_regenerate_pdf: true,
          },
        }}
        pdfStatus={{
          subject_id: "subject-1",
          status: "GENERATING",
          artifact_id: null,
          artifact_version: null,
          artifact_input_hash: null,
          artifact_canonical_sha256: null,
          render_id: null,
          render_identity: null,
          error_code: null,
          error_message: null,
          pdf_url: null,
        }}
        stale={false}
        menuOpen={false}
        technicalOpen={false}
        onToggleMenu={vi.fn()}
        onOpenTechnicalDetails={vi.fn()}
        onTechnicalOpenChange={vi.fn()}
        onRefresh={vi.fn()}
        onRegeneratePdf={vi.fn()}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Régénérer le PDF" }),
    ).toBeDisabled();

    rerender(
      <EditorialTopBar
        subjectId="subject-1"
        snapshot={{
          ...baseSnapshot,
          capabilities: { ...baseSnapshot.capabilities, can_preview_pdf: true },
        }}
        pdfStatus={{
          subject_id: "subject-1",
          status: "READY",
          artifact_id: "new-pdf",
          artifact_version: 2,
          artifact_input_hash: "new-input",
          artifact_canonical_sha256: "new-sha",
          render_id: "new-render",
          render_identity: "new-render-identity",
          error_code: null,
          error_message: null,
          pdf_url: "/new-version.pdf",
        }}
        stale={false}
        menuOpen={false}
        technicalOpen={false}
        onToggleMenu={vi.fn()}
        onOpenTechnicalDetails={vi.fn()}
        onTechnicalOpenChange={vi.fn()}
        onRefresh={vi.fn()}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("À jour");
    expect(screen.getByRole("link", { name: "Voir le PDF" })).toHaveAttribute(
      "href",
      "/new-version.pdf",
    );
    expect(screen.getByText("new-render-identity")).not.toBeVisible();
  });

  it("requires an explicit PDF render after a stale or failed impact", () => {
    render(
      <EditorialTopBar
        subjectId="subject-1"
        snapshot={{
          ...baseSnapshot,
          preview_status: "STALE",
          impact_status: "failed",
          pdf_status: "STALE",
          pdf_artifact_id: null,
          pdf_artifact_version: null,
          pdf_artifact_input_hash: null,
          pdf_artifact_canonical_sha256: null,
          pdf_render_id: null,
          pdf_render_identity: null,
          pdf_error_code: null,
          pdf_error_message: null,
          pdf_url: null,
          capabilities: {
            ...baseSnapshot.capabilities,
            can_regenerate_pdf: true,
          },
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
      screen.getByRole("button", { name: "Régénérer le PDF" }),
    ).toBeEnabled();
    expect(
      screen.queryByText(/retenté automatiquement/i),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/Régénérez l’aperçu/)).toBeVisible();
  });

  it("shows local drafts without claiming the canonical document is current", () => {
    render(
      <EditorialTopBar
        subjectId="subject-1"
        snapshot={baseSnapshot}
        stale={false}
        hasLocalDrafts
        menuOpen={false}
        technicalOpen={false}
        onToggleMenu={vi.fn()}
        onOpenTechnicalDetails={vi.fn()}
        onTechnicalOpenChange={vi.fn()}
        onRefresh={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("status", {
        name: "Brouillon local. Un brouillon de ce bloc n’est pas encore enregistré comme nouvelle version canonique.",
      }),
    ).toHaveTextContent("Brouillon local");
    expect(
      screen.queryByRole("status", {
        name: /dernière version canonique enregistrée/i,
      }),
    ).not.toBeInTheDocument();
  });

  it("keeps stale document and PDF states ahead of local drafts", () => {
    const { rerender } = render(
      <EditorialTopBar
        subjectId="subject-1"
        snapshot={{ ...baseSnapshot, preview_status: "READY" }}
        stale
        hasLocalDrafts
        menuOpen={false}
        technicalOpen={false}
        onToggleMenu={vi.fn()}
        onOpenTechnicalDetails={vi.fn()}
        onTechnicalOpenChange={vi.fn()}
        onRefresh={vi.fn()}
      />,
    );
    expect(screen.getByText("Version périmée")).toBeVisible();

    rerender(
      <EditorialTopBar
        subjectId="subject-1"
        snapshot={{ ...baseSnapshot, preview_status: "STALE" }}
        stale
        hasLocalDrafts
        menuOpen={false}
        technicalOpen={false}
        onToggleMenu={vi.fn()}
        onOpenTechnicalDetails={vi.fn()}
        onTechnicalOpenChange={vi.fn()}
        onRefresh={vi.fn()}
      />,
    );
    expect(screen.getByText("PDF obsolète")).toBeVisible();
  });

  it("links a stale composition to its edition without offering a repin action", () => {
    render(
      <EditorialTopBar
        subjectId="subject-1"
        snapshot={baseSnapshot}
        compositionStatus={{
          edition_id: "edition-1",
          subject_id: "subject-1",
          status: "stale",
          message: "La composition utilise une ancienne version.",
          composition_id: "composition-1",
          composition_version: 3,
          pinned_artifact_id: "pinned-artifact",
          pinned_artifact_version: 2,
          current_artifact_id: "current-artifact",
          current_artifact_version: 3,
          current_version_available: true,
          composition_url: "/api/editions/edition-1/composition",
        }}
        stale={false}
        menuOpen={false}
        technicalOpen={false}
        onToggleMenu={vi.fn()}
        onOpenTechnicalDetails={vi.fn()}
        onTechnicalOpenChange={vi.fn()}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByText("Composition à mettre à jour")).toBeVisible();
    expect(
      screen.getByRole("link", { name: "Ouvrir la composition" }),
    ).toHaveAttribute("href", "/editions/edition-1/composition");
    expect(
      screen.queryByRole("button", { name: /repin|repointer/i }),
    ).toBeNull();
  });

  it("opens history from the top bar menu", async () => {
    const user = userEvent.setup();
    const onOpenHistory = vi.fn();
    render(
      <EditorialTopBar
        subjectId="subject-1"
        snapshot={baseSnapshot}
        stale={false}
        menuOpen
        technicalOpen={false}
        onToggleMenu={vi.fn()}
        onOpenTechnicalDetails={vi.fn()}
        onTechnicalOpenChange={vi.fn()}
        onRefresh={vi.fn()}
        onOpenHistory={onOpenHistory}
      />,
    );

    await user.click(screen.getByRole("menuitem", { name: "Historique" }));
    expect(onOpenHistory).toHaveBeenCalledOnce();
  });
});
