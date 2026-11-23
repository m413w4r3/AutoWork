import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  ArticleAnchorCatalogViewV1,
  ArticleAnchorViewV1,
} from "../api/articleAnchors";
import type { NoteProposalViewV1 } from "../api/opinionProposals";
import {
  createTestQueryClient,
  errorResponse,
  installFetch,
  jsonResponse,
  wrapperFor,
} from "../api/testSupport";
import { AnchorPicker, anchorOccurrences, groupAnchors } from "./AnchorPicker";
import type { AnalystNoteV1 } from "./types";

const INVESTIGATION_ID = "11111111-1111-4111-8111-111111111111";
const BASE_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const OTHER_BASE_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const BASE_SHA = "a".repeat(64);
const DUP_SHA = "b".repeat(64);

function makeAnchor(
  overrides: Partial<ArticleAnchorViewV1> & Pick<ArticleAnchorViewV1, "anchor">,
): ArticleAnchorViewV1 {
  return {
    text: `Texte de ${overrides.anchor}.`,
    text_sha256: "0".repeat(64),
    section_index: null,
    paragraph_index: 0,
    ...overrides,
  };
}

function makeCatalog(
  overrides: Partial<ArticleAnchorCatalogViewV1> = {},
): ArticleAnchorCatalogViewV1 {
  return {
    base_publication_artifact_id: BASE_ID,
    base_publication_version: 3,
    base_publication_sha256: BASE_SHA,
    source: "pinned_opinion_base",
    anchors: [
      makeAnchor({
        anchor: "lead:0001",
        text: "Le chapô observe un service de commande.",
        text_sha256: "1".repeat(64),
        paragraph_index: 0,
      }),
      makeAnchor({
        anchor: "lead:0002",
        text: "Paragraphe dupliqué.",
        text_sha256: DUP_SHA,
        paragraph_index: 1,
      }),
      makeAnchor({
        anchor: "lead:0003",
        text: "Paragraphe dupliqué.",
        text_sha256: DUP_SHA,
        paragraph_index: 2,
      }),
      makeAnchor({
        anchor: "section:2:paragraph:0001",
        text: "La section documente une persistance.",
        text_sha256: "2".repeat(64),
        section_index: 2,
        paragraph_index: 0,
      }),
      makeAnchor({
        anchor: "section:10:paragraph:0001",
        text: "La dixième section.",
        text_sha256: "3".repeat(64),
        section_index: 10,
        paragraph_index: 0,
      }),
    ],
    elements: [],
    truncated: false,
    ...overrides,
  };
}

function makeNote(anchor: string): AnalystNoteV1 {
  return {
    id: "44444444-4444-4444-8444-444444444444",
    anchor,
    text: "Note existante.",
    state: "observed",
    relation: "confirms",
    finding_refs: [],
    evidence_refs: [],
  };
}

function makeProposal(
  overrides: Partial<NoteProposalViewV1> = {},
): NoteProposalViewV1 {
  return {
    id: "33333333-3333-4333-8333-333333333333",
    investigation_id: INVESTIGATION_ID,
    opinion_id: "22222222-2222-4222-8222-222222222222",
    task_id: null,
    anchor: "lead:0001",
    target_block_type: "lead",
    bridge_block_type: "key_findings",
    consultation_type: "draft_text",
    base_note_id: null,
    note_state: "observed",
    note_relation: "confirms",
    cited_finding_refs: [],
    cited_observation_refs: [],
    proposed_text: "Texte Bridge proposé.",
    proposed_text_sha256: "9".repeat(64),
    bridge_invocation_id: "bridge-turn-1",
    bridge_output_sha256: "8".repeat(64),
    bridge_response_observation_id: null,
    tool_invocation_id: null,
    base_opinion_version: 3,
    base_opinion_sha256: "7".repeat(64),
    base_publication_artifact_id: BASE_ID,
    base_publication_version: 3,
    base_publication_sha256: BASE_SHA,
    anchor_before_sha256: "1".repeat(64),
    status: "proposed",
    status_version: 1,
    accepted_opinion_version: null,
    analyst_decision_id: null,
    idempotency_key: "idem-1",
    materialised: true,
    created_at: "2026-11-22T09:00:00Z",
    updated_at: "2026-11-22T09:00:00Z",
    ...overrides,
  };
}

function Harness({
  initialAnchor,
  notes = [],
  proposal = null,
}: {
  initialAnchor: string;
  notes?: AnalystNoteV1[];
  proposal?: NoteProposalViewV1 | null;
}) {
  const [anchor, setAnchor] = useState(initialAnchor);
  return (
    <AnchorPicker
      investigationId={INVESTIGATION_ID}
      anchor={anchor}
      onAnchorChange={setAnchor}
      notes={notes}
      proposal={proposal}
    />
  );
}

function renderPicker({
  initialAnchor = "lead:0001",
  notes = [],
  proposal = null,
  catalog = makeCatalog(),
}: {
  initialAnchor?: string;
  notes?: AnalystNoteV1[];
  proposal?: NoteProposalViewV1 | null;
  catalog?: ArticleAnchorCatalogViewV1;
} = {}) {
  let anchorReads = 0;
  const recorder = installFetch((request) => {
    if (
      request.url === `/api/investigations/${INVESTIGATION_ID}/article-anchors`
    ) {
      anchorReads += 1;
      return jsonResponse(catalog);
    }
    return errorResponse(500, "unexpected_request", request.url);
  });
  const client = createTestQueryClient();
  const result = render(
    <Harness initialAnchor={initialAnchor} notes={notes} proposal={proposal} />,
    { wrapper: wrapperFor(client) },
  );
  return {
    ...result,
    calls: recorder.calls,
    anchorReadCount: () => anchorReads,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("AnchorPicker — sélecteur canonique", () => {
  it("charge la base de publication et groupe lead/sections par index numérique", async () => {
    const { container, calls } = renderPicker();

    const select = await screen.findByRole("combobox", {
      name: "Ancre de publication",
    });
    expect(calls.map((call) => call.url)).toContain(
      `/api/investigations/${INVESTIGATION_ID}/article-anchors`,
    );
    expect(calls[0]?.init?.method).toBe("GET");
    expect(
      screen.getByText(
        (_, element) =>
          element?.tagName === "P" &&
          (element.textContent ?? "").includes("artefact") &&
          (element.textContent ?? "").includes(BASE_ID),
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/version 3/)).toBeInTheDocument();
    expect(screen.getByText(BASE_SHA.slice(0, 12) + "…")).toBeInTheDocument();

    const groupLabels = [...container.querySelectorAll("optgroup")].map(
      (group) => group.label,
    );
    expect(groupLabels).toEqual([
      "Chapô de l’article",
      "Section 2",
      "Section 10",
    ]);
    const optionValues = within(select)
      .getAllByRole("option")
      .map((option) => (option as HTMLOptionElement).value);
    expect(optionValues).toEqual([
      "",
      "lead:0001",
      "lead:0002",
      "lead:0003",
      "section:2:paragraph:0001",
      "section:10:paragraph:0001",
    ]);
    expect(
      within(select).getByRole("option", {
        name: /lead:0001 — Le chapô observe un service de commande\./,
      }),
    ).toBeInTheDocument();
  });

  it("affiche le texte complet borné et le badge d’occurrence des paragraphes identiques", async () => {
    renderPicker({ initialAnchor: "lead:0002" });

    expect(
      await screen.findByText("Paragraphe dupliqué.", { selector: "p[title]" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("occurrence 1 sur 2", { selector: "li.badge" }),
    ).toBeInTheDocument();

    renderPicker({ initialAnchor: "lead:0003" });
    const badges = await screen.findAllByText("occurrence 2 sur 2", {
      selector: "li.badge",
    });
    expect(badges.length).toBeGreaterThan(0);
  });

  it("signale l’ancre qui remplacerait une note existante", async () => {
    const user = userEvent.setup();
    renderPicker({
      initialAnchor: "lead:0001",
      notes: [makeNote("lead:0001")],
    });

    expect(
      await screen.findByText("remplace la note existante", {
        selector: "li.badge",
      }),
    ).toBeInTheDocument();

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Ancre de publication" }),
      "lead:0002",
    );
    expect(
      screen.queryByText("remplace la note existante", {
        selector: "li.badge",
      }),
    ).toBeNull();
  });

  it("affiche un bandeau base périmée quand la proposition a épinglé une autre base", async () => {
    const user = userEvent.setup();
    const { anchorReadCount } = renderPicker({
      proposal: makeProposal({ base_publication_artifact_id: OTHER_BASE_ID }),
    });

    expect(await screen.findByText(/Base périmée/)).toBeInTheDocument();
    const reload = screen.getByRole("button", { name: "Recharger les ancres" });
    const before = anchorReadCount();
    await user.click(reload);
    await waitFor(() => expect(anchorReadCount()).toBeGreaterThan(before));
  });

  it("refuse explicitement une ancre absente et propose de recharger", async () => {
    const user = userEvent.setup();
    const { anchorReadCount } = renderPicker({ initialAnchor: "lead:0099" });

    expect(
      await screen.findByText(/n’apparaît pas dans la base de publication/),
    ).toBeInTheDocument();
    expect(screen.getAllByText(/invalid_note_anchor/).length).toBeGreaterThan(
      0,
    );
    const before = anchorReadCount();
    await user.click(
      screen.getByRole("button", { name: "Recharger les ancres" }),
    );
    await waitFor(() => expect(anchorReadCount()).toBeGreaterThan(before));
  });

  it("garde la saisie manuelle derrière un mode diagnostic sans datalist", async () => {
    const user = userEvent.setup();
    const { container } = renderPicker();

    const summary = await screen.findByText(
      "Diagnostic — saisie manuelle d’ancre",
    );
    const details = summary.closest("details");
    expect(details).not.toBeNull();
    expect(details?.open).toBe(false);
    expect(container.querySelector("datalist")).toBeNull();
    expect(
      screen.getByText(/le serveur reste l’autorité/i),
    ).toBeInTheDocument();
    await screen.findByRole("combobox", { name: "Ancre de publication" });

    await user.click(summary);
    const manual = screen.getByRole("textbox", { name: "Ancre manuelle" });
    await user.clear(manual);
    await user.type(manual, "section:4:paragraph:0002");
    expect(manual).toHaveValue("section:4:paragraph:0002");
    expect(
      screen.getByText(/n’apparaît pas dans la base de publication/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "Ancre de publication" }),
    ).toHaveValue("");
  });

  it("signale des ancres indisponibles et permet de recharger", async () => {
    const user = userEvent.setup();
    let reads = 0;
    installFetch(() => {
      reads += 1;
      return errorResponse(503, "article_anchors_unavailable");
    });
    const client = createTestQueryClient();
    render(<Harness initialAnchor="lead:0001" />, {
      wrapper: wrapperFor(client),
    });

    expect(
      await screen.findByText(/Ancres de publication indisponibles/),
    ).toBeInTheDocument();
    const before = reads;
    await user.click(
      screen.getByRole("button", { name: "Recharger les ancres" }),
    );
    await waitFor(() => expect(reads).toBeGreaterThan(before));
  });
});

describe("AnchorPicker — helpers", () => {
  it("numérote les occurrences dans l’ordre des ancres", () => {
    const occurrences = anchorOccurrences(makeCatalog().anchors);
    expect(occurrences.get("lead:0002")).toEqual({ index: 1, total: 2 });
    expect(occurrences.get("lead:0003")).toEqual({ index: 2, total: 2 });
    expect(occurrences.has("lead:0001")).toBe(false);
  });

  it("trie les sections par index et non par texte d’ancre", () => {
    const groups = groupAnchors(makeCatalog().anchors);
    expect(groups.map((group) => group.key)).toEqual([
      "lead",
      "section:2",
      "section:10",
    ]);
  });
});
