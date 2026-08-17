import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SubjectWorkbench } from "./SubjectWorkbench";

const subjectId = "30e5b0b8-2dba-48c3-81ca-9eaed5c22c62";
const workbench = {
  subject_id: subjectId,
  sources: [
    {
      id: "61cb719a-6432-4381-911e-d4447ecf6332",
      requested_url: "https://research.example/report",
      state: "completed",
      proposed_role: "primary",
      relationship_status: "provisional",
      relationship_evidence: "model_proposal",
      source_document_id: "f7fd2882-da41-4d3c-9bea-e592b6d2524a",
      attempt_count: 1,
      error_reason: null,
      fetch_lease_expires_at: null,
      latest_attempt: {
        id: "4c84c931-989b-498b-84b0-60901671321d",
        requested_url: "https://research.example/report",
        final_url: "https://research.example/final",
        redirect_chain: ["https://research.example/final"],
        attempted_at: "2026-08-10T10:00:00Z",
        completed_at: "2026-08-10T10:00:01Z",
        http_status: 200,
        declared_content_type: "text/plain",
        detected_content_type: "text/html",
        encoded_size: 800,
        encoded_sha256: "a".repeat(64),
        decoded_size: 1234,
        decoded_sha256: "b".repeat(64),
        content_encoding: "gzip",
        outcome: "succeeded",
        failure_reason: null,
      },
      title: "Rapport ExampleRAT",
      publisher: "Example Research",
      published_at: "2026-08-10",
      tlp: "AMBER",
      logical_filename:
        "2026-08-10_TLP AMBER_Rapport ExampleRAT_Example Research.html",
      detected_mime_type: "text/html",
    },
  ],
  claims: [
    {
      id: "20658589-a6d5-4af5-b026-d5c6fcb3b7f0",
      kind: "infection_chain",
      original_value: "PowerShell lance ExampleRAT",
      current_value: "PowerShell lance ExampleRAT",
      status: "extracted",
      source_id: "f7fd2882-da41-4d3c-9bea-e592b6d2524a",
      source_span: { start: 10, end: 39 },
      passage: "PowerShell lance ExampleRAT",
      extraction_payload: { confidence: "high" },
    },
  ],
  indicators: [
    {
      id: "c6c38491-e0a3-4315-a64e-e27946a350a4",
      kind: "domain",
      original_value: "evil[.]example",
      normalized_value: "evil.example",
      current_value: "evil.example",
      status: "extracted",
      source_id: "f7fd2882-da41-4d3c-9bea-e592b6d2524a",
      source_span: { start: 40, end: 54 },
    },
  ],
};

function renderWorkbench() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <SubjectWorkbench subjectId={subjectId} />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("SubjectWorkbench", () => {
  it("affiche les détails archivés et conserve la relation LLM provisoire", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(workbench)));
    renderWorkbench();

    expect(
      await screen.findByRole("heading", {
        name: "Rapport ExampleRAT",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("https://research.example/final"),
    ).toBeInTheDocument();
    expect(screen.getByText("a".repeat(64))).toBeInTheDocument();
    expect(screen.getByText("b".repeat(64))).toBeInTheDocument();
    expect(screen.getByText("SHA-256 brut encodé")).toBeInTheDocument();
    expect(screen.getByText("SHA-256 contenu décodé")).toBeInTheDocument();
    expect(
      screen.getByText("provisional", { selector: "strong" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/aucun JavaScript n’est exécuté/),
    ).toBeInTheDocument();
    expect(screen.getByText("Archivée — prête pour l’analyse")).toBeVisible();
    expect(screen.getByRole("link", { name: "Télécharger" })).toHaveAttribute(
      "href",
      expect.stringContaining("/download"),
    );
    expect(
      screen.getByText("Détails techniques").parentElement,
    ).not.toHaveAttribute("open");
  });

  it("présente les passages surlignés et les IOC originaux et normalisés", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(workbench)));
    const user = userEvent.setup();
    renderWorkbench();
    await screen.findByText("https://research.example/final");

    await user.click(screen.getByRole("button", { name: "Preuves" }));
    expect(
      screen.getByText("PowerShell lance ExampleRAT", { selector: "mark" }),
    ).toBeVisible();

    await user.click(screen.getByRole("button", { name: "IOC" }));
    expect(screen.getByText(/Original : evil\[\.\]example/)).toHaveTextContent(
      "normalisé : evil.example",
    );
  });
});
