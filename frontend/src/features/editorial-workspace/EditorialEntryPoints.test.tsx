import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../../App";
import type {
  EditorialThreadRead,
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import type { ProductionBoard } from "../../api/production";
import type { ReviewItem } from "../../api/publication";
import { SubjectProduction } from "../../components/SubjectProduction";
import { SubjectWorkbench } from "../../components/SubjectWorkbench";
import { ProductionConsole } from "../edition-workflow/ProductionConsole";
import { EditionPreviewPanel } from "../edition-workflow/EditionPreviewPanel";
import { ReviewItemCard } from "../edition-workflow/ReviewItemCard";
import { ArticleTab } from "../subject/ArticleTab";

const subjectId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const titleId = "00000000-0000-4000-8000-000000000000";
const headingId = "11111111-1111-4111-8111-111111111111";
const paragraphId = "22222222-2222-4222-8222-222222222222";
const threadEtag = '"vitest-editorial-thread-empty"';
const threadBinding = {
  subject_id: subjectId,
  production_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  occurrence_id: paragraphId,
  artifact_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
  artifact_version: 4,
  canonical_sha256: "a".repeat(64),
};
const unopenedThread: EditorialThreadRead = {
  kind: "unopened",
  binding: threadBinding,
  current: threadBinding,
  bridge_status: "ready",
  event_sequence: 0,
  timeline: [],
  etag: threadEtag,
};

const snapshot: EditorialWorkspaceSnapshot = {
  subject_id: subjectId,
  production_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  publication_kind: "article",
  synthesis_artifact_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
  synthesis_version: 4,
  synthesis_canonical_sha256: "a".repeat(64),
  enrichment_artifact_id: null,
  enrichment_version: null,
  publication_artifact_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  publication_version: 3,
  pipeline_generation: 2,
  blocks: [
    {
      block_id: titleId,
      kind: "title",
      parent_block_id: null,
      position: 0,
      text: "Synthetic editorial article",
      evidence_refs: [],
      chart: null,
      revision_target: { block_id: titleId, legacy_target_id: null },
      before_hash: null,
      allowed_operations: [],
      revision_status: "ready",
    },
    {
      block_id: headingId,
      kind: "section_heading",
      parent_block_id: null,
      position: 1,
      text: "Contexte",
      evidence_refs: [],
      chart: null,
      revision_target: { block_id: headingId, legacy_target_id: null },
      before_hash: null,
      allowed_operations: ["rewrite_section", "delete_section"],
      revision_status: "ready",
    },
    {
      block_id: paragraphId,
      kind: "paragraph",
      parent_block_id: headingId,
      position: 2,
      text: "Le paragraphe exact reste sélectionné.",
      evidence_refs: [
        { source_title: "Rapport synthétique", passage: "Contexte exact." },
      ],
      chart: null,
      revision_target: { block_id: paragraphId, legacy_target_id: null },
      before_hash: null,
      allowed_operations: [
        "rewrite_paragraph",
        "delete_paragraph",
        "insert_paragraph",
      ],
      revision_status: "ready",
    },
  ],
  enrichments: [],
  document_outline: [
    { block_id: headingId, label: "Contexte", children: [paragraphId] },
  ],
  capabilities: {
    can_discuss: false,
    can_preview_revision: false,
    manual_edit: false,
    can_add_enrichment: false,
    can_preview_pdf: true,
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
  snapshot_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
};

function renderApp(editorResponseValue: Response | Promise<Response>) {
  window.history.replaceState({}, "", `/subjects/${subjectId}/editor`);
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (url.includes("/api/editions")) {
        return Promise.resolve(
          Response.json({ items: [], total: 0, page: 1, page_size: 20 }),
        );
      }
      if (url.includes("/editor/threads/")) {
        const requestUrl = new URL(url, window.location.origin);
        const afterSequence = Number(
          requestUrl.searchParams.get("after_sequence") ?? "0",
        );
        const headers = new Headers(
          init?.headers ??
            (input instanceof Request ? input.headers : undefined),
        );
        if (headers.get("If-None-Match") === threadEtag) {
          return Promise.resolve(
            new Response(null, { status: 304, headers: { ETag: threadEtag } }),
          );
        }
        return Promise.resolve(
          new Response(
            JSON.stringify({
              ...unopenedThread,
              timeline: unopenedThread.timeline.filter(
                (event) => event.sequence > afterSequence,
              ),
            }),
            {
              status: 200,
              headers: {
                "Content-Type": "application/json",
                ETag: threadEtag,
              },
            },
          ),
        );
      }
      return Promise.resolve(editorResponseValue).then((response) =>
        response.clone(),
      );
    }),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
}

function renderWithQueryClient(children: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>{children}</QueryClientProvider>,
  );
}

function editorResponse(status = 200, body: unknown = snapshot) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/editions");
});

describe("atelier éditorial", () => {
  it("test_editor_route_precedes_subject_detail_route", async () => {
    window.history.replaceState({}, "", `/subjects/${subjectId}/editor`);
    renderApp(editorResponse());

    expect(
      await screen.findByRole("link", { name: /retour au sujet/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /retour au sujet/i }),
    ).toHaveAttribute("href", `/subjects/${subjectId}`);
  });

  it("test_forbidden_or_missing_subject_shows_safe_route_state", async () => {
    window.history.replaceState({}, "", `/subjects/${subjectId}/editor`);
    renderApp(editorResponse(403, { detail: { code: "subject_forbidden" } }));

    expect(
      await screen.findByRole("alert", {
        name: /atelier éditorial indisponible/i,
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText(subjectId)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/Synthetic editorial article/i),
    ).not.toBeInTheDocument();
  });

  it("test_layout_keeps_selected_block_context", async () => {
    window.history.replaceState({}, "", `/subjects/${subjectId}/editor`);
    renderApp(editorResponse());
    const user = userEvent.setup();
    await screen.findAllByText("Le paragraphe exact reste sélectionné.");

    const paragraph = document.querySelector(
      `[data-block-id="${paragraphId}"] .editorial-document__text`,
    );
    if (!(paragraph instanceof HTMLElement)) {
      throw new Error("Le paragraphe synthétique est absent du document.");
    }
    await user.click(paragraph);
    await user.click(screen.getByRole("button", { name: "Sources" }));

    expect(screen.getByText("Section : Contexte")).toBeInTheDocument();
    expect(screen.getByText("Contexte exact.")).toBeInTheDocument();
    expect(
      document.querySelector(`[data-block-id="${paragraphId}"]`),
    ).toHaveAttribute("data-selected", "true");
  });

  it("returns to the document for another target and closes the mobile inspector", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((media: string) => ({
        matches: media === "(max-width: 767px)",
        media,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );
    renderApp(editorResponse());
    const user = userEvent.setup();
    await screen.findAllByText("Le paragraphe exact reste sélectionné.");

    const paragraph = document.querySelector(
      `.editorial-document__block[data-block-id="${paragraphId}"] .editorial-document__text`,
    );
    if (!(paragraph instanceof HTMLElement)) {
      throw new Error("Le paragraphe synthétique est absent du document.");
    }
    await user.click(paragraph);
    const inspectorToggle = document.querySelector(
      ".editorial-workspace__inspector-toggle",
    );
    if (!(inspectorToggle instanceof HTMLButtonElement)) {
      throw new Error("Le bouton inspecteur mobile est absent.");
    }
    fireEvent.click(inspectorToggle);
    const inspectorColumn = document.querySelector(
      ".editorial-workspace__inspector-column",
    );
    expect(inspectorColumn).toHaveAttribute("data-panel-open", "true");

    await user.click(
      await screen.findByRole("button", { name: "Changer de cible" }),
    );

    await waitFor(() =>
      expect(inspectorColumn).toHaveAttribute("data-panel-open", "false"),
    );
    const selectedBlock = document.querySelector(
      `.editorial-document__block[data-block-id="${paragraphId}"]`,
    );
    await waitFor(() => expect(selectedBlock).toHaveFocus());
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("uses server order for enrichments without duplicating projected blocks", async () => {
    const enrichment: EditorialWorkspaceBlock = {
      block_id: "33333333-3333-4333-8333-333333333333",
      kind: "figure",
      parent_block_id: headingId,
      position: 3,
      text: "Figure synthétique",
      evidence_refs: [],
      chart: null,
      revision_target: {
        block_id: "33333333-3333-4333-8333-333333333333",
        legacy_target_id: null,
      },
      before_hash: null,
      allowed_operations: [],
      revision_status: "ready",
    };
    renderApp(
      editorResponse(200, {
        ...snapshot,
        blocks: [...snapshot.blocks, enrichment],
        enrichments: [enrichment],
      }),
    );

    expect(await screen.findAllByText("Figure synthétique")).toHaveLength(2);
    expect(
      screen.queryByText(/n’est pas affiché dans le document/i),
    ).not.toBeInTheDocument();
  });

  it("allows sources from the authorized snapshot when no write operation is allowed", async () => {
    const readOnlySnapshot: EditorialWorkspaceSnapshot = {
      ...snapshot,
      blocks: snapshot.blocks.map((block) =>
        block.block_id === paragraphId
          ? { ...block, allowed_operations: [] }
          : block,
      ),
    };
    renderApp(editorResponse(200, readOnlySnapshot));
    await screen.findAllByText("Le paragraphe exact reste sélectionné.");
    const paragraph = document.querySelector(
      `[data-block-id="${paragraphId}"] .editorial-document__text`,
    );
    if (!(paragraph instanceof HTMLElement)) {
      throw new Error("Le paragraphe synthétique est absent du document.");
    }
    await userEvent.setup().click(paragraph);

    expect(
      await screen.findByRole("button", { name: "Sources" }),
    ).toBeVisible();
    expect(screen.getByText("Contexte exact.")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^Modifier/ }),
    ).not.toBeInTheDocument();
  });

  it("annonce le chargement avant de recevoir le snapshot", async () => {
    let resolveResponse: (response: Response) => void = () => undefined;
    const pending = new Promise<Response>((resolve) => {
      resolveResponse = resolve;
    });
    renderApp(pending);

    expect(screen.getByRole("status")).toHaveTextContent(
      "Chargement de l’atelier éditorial",
    );
    resolveResponse(editorResponse());
    expect(
      await screen.findByRole("link", { name: /retour au sujet/i }),
    ).toBeInTheDocument();
  });

  it("affiche l’état vide sans confondre absence et refus d’accès", async () => {
    renderApp(
      editorResponse(200, { ...snapshot, blocks: [], enrichments: [] }),
    );

    expect(
      await screen.findByText("Aucun contenu produit"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("annonce un PDF obsolète et propose une régénération explicite", async () => {
    renderApp(
      editorResponse(200, {
        ...snapshot,
        preview_status: "STALE",
        capabilities: { ...snapshot.capabilities, can_regenerate_pdf: true },
      }),
    );

    expect(
      await screen.findByRole("status", { name: /PDF obsolète/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Régénérer le PDF" }),
    ).toBeInTheDocument();
  });

  it("garde le kind indéterminé en lecture seule", async () => {
    renderApp(editorResponse(200, { ...snapshot, publication_kind: null }));

    expect(
      await screen.findAllByText(/Type de publication indéterminé/),
    ).not.toHaveLength(0);
    expect(screen.getByText("Type indéterminé")).toBeInTheDocument();
  });

  it("masque les actions d’écriture si le type reste indéterminé", async () => {
    const writeEnabledSnapshot: EditorialWorkspaceSnapshot = {
      ...snapshot,
      publication_kind: null,
      capabilities: {
        ...snapshot.capabilities,
        can_discuss: true,
        manual_edit: true,
        can_add_enrichment: true,
      },
      blocks: snapshot.blocks.map((block) =>
        block.kind === "paragraph"
          ? {
              ...block,
              allowed_operations: [
                "rewrite_paragraph",
                "discuss",
                "insert_after",
                "custom_write_operation",
              ],
            }
          : block,
      ),
    };
    renderApp(editorResponse(200, writeEnabledSnapshot));
    const user = userEvent.setup();
    await screen.findAllByText("Le paragraphe exact reste sélectionné.");
    const paragraph = document.querySelector(
      `[data-block-id="${paragraphId}"] .editorial-document__text`,
    );
    if (!(paragraph instanceof HTMLElement)) {
      throw new Error("Le paragraphe synthétique est absent du document.");
    }
    await user.click(paragraph);

    expect(
      screen.queryByRole("button", { name: /^Modifier/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^Discuter/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^Ajouter après/ }),
    ).not.toBeInTheDocument();
    expect(
      document.querySelector(".editorial-block-actions__more"),
    ).not.toBeInTheDocument();
  });

  it("donne le focus au composeur de discussion depuis le bouton Discuter", async () => {
    const discussSnapshot: EditorialWorkspaceSnapshot = {
      ...snapshot,
      capabilities: { ...snapshot.capabilities, can_discuss: true },
      blocks: snapshot.blocks.map((block) =>
        block.kind === "paragraph"
          ? { ...block, allowed_operations: ["rewrite_paragraph", "discuss"] }
          : block,
      ),
    };
    renderApp(editorResponse(200, discussSnapshot));
    const user = userEvent.setup();
    await screen.findAllByText("Le paragraphe exact reste sélectionné.");
    const paragraph = document.querySelector(
      `[data-block-id="${paragraphId}"] .editorial-document__text`,
    );
    if (!(paragraph instanceof HTMLElement)) {
      throw new Error("Le paragraphe synthétique est absent du document.");
    }
    await user.click(paragraph);

    const discuss = await screen.findByRole("button", { name: "Discuter" });
    expect(discuss).toBeEnabled();
    expect(discuss).not.toHaveAttribute("title");
    const composer = await screen.findByRole("textbox", {
      name: "Votre message à Bridge",
    });

    await user.click(discuss);

    expect(composer).toHaveFocus();
  });

  it("annonce la sélection comme un statut sans rendre le bloc sélectionné en bouton", async () => {
    renderApp(editorResponse());
    const user = userEvent.setup();
    await screen.findAllByText("Le paragraphe exact reste sélectionné.");
    const paragraph = document.querySelector(
      `[data-block-id="${paragraphId}"] .editorial-document__text`,
    );
    if (!(paragraph instanceof HTMLElement)) {
      throw new Error("Le paragraphe synthétique est absent du document.");
    }
    await user.click(paragraph);

    const selectedStatus = document.querySelector(
      ".editorial-document__selected-status",
    );
    expect(selectedStatus).toHaveAttribute("role", "status");
    expect(selectedStatus).toHaveTextContent("Bloc sélectionné");
    expect(selectedStatus?.tagName).not.toBe("BUTTON");
    expect(
      screen.queryByRole("button", { name: /Bloc sélectionné/ }),
    ).not.toBeInTheDocument();
  });

  it("présente une erreur de lecture sans exposer son détail", async () => {
    renderApp(
      editorResponse(500, {
        detail: { code: "internal_error", message: "private backend detail" },
      }),
    );

    expect(
      await screen.findByText("L’atelier éditorial n’a pas pu être chargé."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("private backend detail"),
    ).not.toBeInTheDocument();
  });

  it("parcourt le plan avec les flèches et restaure le focus après Échap", async () => {
    renderApp(editorResponse());
    const user = userEvent.setup();
    const items = await screen.findAllByRole("treeitem");
    const firstItem = items[0];
    const secondItem = items[1];
    if (!firstItem || !secondItem) {
      throw new Error("Le plan synthétique doit contenir plusieurs blocs.");
    }

    firstItem.focus();
    await user.keyboard("{ArrowDown}");
    await screen.findByText(
      "Aucune discussion n’a encore été ouverte pour ce bloc.",
    );
    expect(secondItem).toHaveFocus();
    expect(secondItem).toHaveAttribute("aria-selected", "true");

    const menuToggle = await screen.findByRole("button", {
      name: "Historique et détails",
    });
    expect(
      document.querySelector(".editorial-block-actions__more summary"),
    ).toHaveTextContent("Autres actions du bloc");
    expect(
      screen.queryByRole("button", { name: "Plus" }),
    ).not.toBeInTheDocument();
    await user.click(menuToggle);
    expect(menuToggle).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{Escape}");

    await waitFor(() => expect(menuToggle).toHaveFocus());
    expect(menuToggle).toHaveAttribute("aria-expanded", "false");
  });

  it("garde un retour clavier depuis les détails techniques", async () => {
    renderApp(editorResponse());
    const user = userEvent.setup();
    const menuToggle = await screen.findByRole("button", {
      name: "Historique et détails",
    });
    await user.click(menuToggle);
    await user.click(
      screen.getByRole("button", { name: "Détails techniques" }),
    );

    const summary = document.querySelector(
      ".editorial-technical-details summary",
    );
    await waitFor(() => expect(summary).toHaveFocus());
    expect(
      document.querySelector(".editorial-technical-details"),
    ).toHaveAttribute("open");
    await user.keyboard("{Escape}");

    await waitFor(() => expect(menuToggle).toHaveFocus());
    expect(
      document.querySelector(".editorial-technical-details"),
    ).not.toHaveAttribute("open");
  });
});

it("expose le lien depuis la consultation d’un article publié", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        Response.json({
          subject_id: subjectId,
          run_id: "run-entry-point",
          pipeline_generation: 1,
          artifact_id: "artifact-entry-point",
          artifact_version: 1,
          artifact_input_hash: "f".repeat(64),
          status: "verified",
          schema_version: "4",
          publication_kind: "article",
          canonical_content: {
            schema_version: "4",
            subject_id: subjectId,
            publication_language: "fr",
            title: "Synthetic editorial article",
            lead: [],
            sections: [],
            timeline: [],
            indicators: [],
            sources: [],
            uncertainties: [],
            tables: [],
            diagrams: [],
            figures: [],
          },
        }),
      ),
    ),
  );
  renderWithQueryClient(
    <ArticleTab subjectId={subjectId} onOpenPipeline={vi.fn()} />,
  );

  expect(
    await screen.findByRole("link", { name: "Ouvrir l’atelier éditorial" }),
  ).toHaveAttribute("href", `/subjects/${subjectId}/editor`);
});

it("expose le lien dans les lignes de production et de revue", async () => {
  const editionId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const board: ProductionBoard = {
    edition_id: editionId,
    subjects: [],
    active_batch: {
      batch_id: "batch-entry-point",
      edition_id: editionId,
      status: "completed",
      phase: "initial",
      next_dispatch_at: null,
      items: 1,
      completed: 1,
      needs_review: 0,
      failed: 0,
      cancelled: 0,
      item_details: [
        {
          position: 1,
          subject_id: subjectId,
          title: "Synthetic editorial article",
          run_id: "run-entry-point",
          status: "ready",
          current_stage: "assembly",
          pipeline_generation: 1,
          auto_recovery_count: 0,
          error_code: null,
          error_message: null,
        },
      ],
      created_at: "2026-01-01T00:00:00Z",
      started_at: "2026-01-01T00:00:00Z",
      finished_at: "2026-01-01T00:01:00Z",
    },
    recent_batches: [],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(Response.json(board))),
  );
  renderWithQueryClient(<ProductionConsole editionId={editionId} />);

  expect(
    await screen.findByRole("link", { name: "Ouvrir l’atelier éditorial" }),
  ).toHaveAttribute("href", `/subjects/${subjectId}/editor`);
});

it("expose le lien sur une carte de revue", () => {
  const item: ReviewItem = {
    position: 1,
    subject_id: subjectId,
    title: "Synthetic editorial article",
    run_id: "run-entry-point",
    pipeline_generation: 1,
    run_status: "ready",
    document_artifact_id: "artifact-entry-point",
    document_artifact_version: 1,
    document_input_hash: "f".repeat(64),
    effective_decision_id: null,
    effective_decision: "include",
    included: true,
    blocking: false,
    rejected_indicator_count: 0,
    rejected_rule_count: 0,
    published_rule_count: 0,
    can_retry: false,
    retry_stage: null,
    requires_reconciliation: false,
    reconciliation: null,
    error_code: null,
    error_message: null,
  };
  renderWithQueryClient(
    <ul>
      <ReviewItemCard editionId="edition-entry-point" item={item} readOnly />
    </ul>,
  );

  expect(
    screen.getByRole("link", { name: "Ouvrir l’atelier éditorial" }),
  ).toHaveAttribute("href", `/subjects/${subjectId}/editor`);
});

it("test_all_five_entry_surfaces_use_editorial_label_and_subject_id", async () => {
  const editionId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const briefSubjectId = "subject-brief-entry-point";
  const subject = {
    id: subjectId,
    edition_id: editionId,
    title: "Synthetic editorial article",
    slug: "synthetic-editorial-article",
    tlp: "CLEAR",
    version: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
  const publication = {
    schema_version: "4",
    subject_id: subjectId,
    publication_language: "fr",
    title: "Synthetic editorial article",
    lead: [],
    sections: [],
    timeline: [],
    indicators: [],
    sources: [],
    uncertainties: [],
    tables: [],
    diagrams: [],
    figures: [],
  };
  const content = {
    subject_id: subjectId,
    run_id: "run-entry-point",
    pipeline_generation: 1,
    artifact_id: "artifact-entry-point",
    artifact_version: 1,
    artifact_input_hash: "f".repeat(64),
    status: "verified",
    schema_version: "4",
    publication_kind: "article",
    canonical_content: publication,
  };
  const subjectStatus = {
    subject_id: subjectId,
    edition_id: editionId,
    title: "Synthetic editorial article",
    status: "ready",
    current_stage: "assembly",
    progress_current: 7,
    progress_total: 7,
    run_id: "run-entry-point",
    pipeline_generation: 1,
    created_at: "2026-01-01T00:00:00Z",
    started_at: "2026-01-01T00:00:00Z",
    finished_at: "2026-01-01T00:01:00Z",
    error_code: null,
    error_message: null,
    error_details: null,
    recovery_disposition: "auto",
    warnings: [],
    stages: Object.fromEntries(
      [
        "sources",
        "references",
        "extraction",
        "relevance_projection",
        "synthesis",
        "editorial_enrichment",
        "assembly",
      ].map((stage) => [
        stage,
        {
          status: "succeeded",
          version: null,
          error_code: null,
          error_message: null,
        },
      ]),
    ),
  };
  const board: ProductionBoard = {
    edition_id: editionId,
    subjects: [],
    active_batch: {
      batch_id: "batch-entry-point",
      edition_id: editionId,
      status: "completed",
      phase: "initial",
      next_dispatch_at: null,
      items: 1,
      completed: 1,
      needs_review: 0,
      failed: 0,
      cancelled: 0,
      item_details: [
        {
          position: 1,
          subject_id: subjectId,
          title: "Synthetic editorial article",
          run_id: "run-entry-point",
          status: "ready",
          current_stage: "assembly",
          pipeline_generation: 1,
          auto_recovery_count: 0,
          error_code: null,
          error_message: null,
        },
      ],
      created_at: "2026-01-01T00:00:00Z",
      started_at: "2026-01-01T00:00:00Z",
      finished_at: "2026-01-01T00:01:00Z",
    },
    recent_batches: [],
  };
  const briefDocument = {
    schema_version: "5",
    subject_id: briefSubjectId,
    publication_language: "fr",
    title: "Chronologie courte",
    lead: [],
    sections: [],
    timeline: [],
    indicators: [],
    sources: [],
    uncertainties: [],
    tables: [],
    diagrams: [],
    figures: [],
    references: [],
    original_indicators: [],
    rich_text: {
      schema_version: "1",
      policy_version: "semantic-policy-v1",
      paragraphs: [],
    },
  };
  const articleDocument = { ...briefDocument, subject_id: subjectId };
  const preview = {
    edition_id: editionId,
    edition_version: 1,
    composition_id: "composition-entry-point",
    composition_version: 1,
    preview_input_hash: "a".repeat(64),
    artifacts: [],
    classifications: [
      {
        position: 1,
        subject_id: subjectId,
        kind: "article",
        number: "01",
        anchor: "article01",
      },
      {
        position: 2,
        subject_id: briefSubjectId,
        kind: "brief",
        number: "01",
        anchor: "brief01",
      },
    ],
    document: {
      schema_version: "2",
      edition: {
        id: editionId,
        country: "France",
        country_code: "FR",
        period_start: "2026-01-01",
        period_end: "2026-01-31",
        tlp: "GREEN",
        languages: ["fr"],
        state: "OPEN",
        version: 1,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
      publications: [
        {
          position: 1,
          subject_id: subjectId,
          document: articleDocument,
        },
        {
          position: 2,
          subject_id: briefSubjectId,
          document: briefDocument,
        },
      ],
    },
    partial: false,
    missing_subject_ids: [],
    deferred_subject_ids: [],
    stale: false,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      if (url === `/api/subjects/${subjectId}`) {
        return Promise.resolve(Response.json(subject));
      }
      if (url === `/api/subjects/${subjectId}/content`) {
        return Promise.resolve(Response.json(content));
      }
      if (url === `/api/subjects/${subjectId}/production/runs`) {
        return Promise.resolve(Response.json([]));
      }
      if (url === `/api/subjects/${subjectId}/production`) {
        return Promise.resolve(Response.json(subjectStatus));
      }
      if (url === `/api/editions/${editionId}/production`) {
        return Promise.resolve(Response.json(board));
      }
      if (url === `/api/editions/${editionId}/composition/preview`) {
        return Promise.resolve(Response.json(preview));
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    }),
  );
  const reviewItem: ReviewItem = {
    position: 1,
    subject_id: subjectId,
    title: "Synthetic editorial article",
    run_id: "run-entry-point",
    pipeline_generation: 1,
    run_status: "ready",
    document_artifact_id: "artifact-entry-point",
    document_artifact_version: 1,
    document_input_hash: "f".repeat(64),
    effective_decision_id: null,
    effective_decision: "include",
    included: true,
    blocking: false,
    rejected_indicator_count: 0,
    rejected_rule_count: 0,
    published_rule_count: 0,
    can_retry: false,
    retry_stage: null,
    requires_reconciliation: false,
    reconciliation: null,
    error_code: null,
    error_message: null,
  };
  renderWithQueryClient(
    <>
      <SubjectWorkbench subjectId={subjectId} />
      <ArticleTab subjectId={subjectId} onOpenPipeline={vi.fn()} />
      <SubjectProduction subjectId={subjectId} editionId={editionId} />
      <ProductionConsole editionId={editionId} readOnly />
      <ul>
        <ReviewItemCard editionId={editionId} item={reviewItem} readOnly />
      </ul>
      <EditionPreviewPanel editionId={editionId} compositionPreview />
    </>,
  );

  await waitFor(() =>
    expect(
      screen.getAllByRole("link", { name: "Ouvrir l’atelier éditorial" }),
    ).toHaveLength(8),
  );
  const links = screen.getAllByRole("link", {
    name: "Ouvrir l’atelier éditorial",
  });
  expect(links.map((link) => link.getAttribute("href")).sort()).toEqual(
    [
      ...Array.from({ length: 7 }, () => `/subjects/${subjectId}/editor`),
      `/subjects/${briefSubjectId}/editor`,
    ].sort(),
  );
});
