import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  InvestigationFinding,
  InvestigationObservation,
} from "../../../api/investigation";
import type {
  NoteContentBlockV1,
  NoteProposalDiffViewV1,
  NoteProposalViewV1,
  ResolvedNoteContentBlockV1,
} from "../api/opinionProposals";
import {
  bodyOf,
  createTestQueryClient,
  installFetch,
  jsonResponse,
  wrapperFor,
} from "../api/testSupport";
import { NoteProposalReview } from "./NoteProposalReview";

const INVESTIGATION_ID = "11111111-1111-4111-8111-111111111111";
const PROPOSAL_ID = "22222222-2222-4222-8222-222222222222";
const OPINION_ID = "33333333-3333-4333-8333-333333333333";
const NOTE_ID = "44444444-4444-4444-8444-444444444444";
const OBSERVATION_ID = "55555555-5555-4555-8555-555555555555";
const SUBJECT_ID = "66666666-6666-4666-8666-666666666666";
const BASE_ID = "77777777-7777-4777-8777-777777777777";
const FINDING_ID = "88888888-8888-4888-8888-888888888888";
const ELEMENT_HASH = "a".repeat(64);
const BLOB_HASH = "b".repeat(64);
const EXCERPT_HASH = "c".repeat(64);
const ASSET_HASH = "d".repeat(64);
const BASE_SHA = "e".repeat(64);
const PROPOSED_HASH = "f".repeat(64);

const REVIEW_FINDING: InvestigationFinding = {
  id: FINDING_ID,
  logical_id: "finding-network-1",
  current_version_id: "99999999-9999-4999-8999-999999999999",
  version: 1,
  revision_chain: [],
  kind: "network_behaviour",
  assessment: "supported",
  novelty: null,
  statement: "Le serveur répond à une requête de commande.",
  scope: "Échantillon A",
  limitations: [],
  observation_refs: [OBSERVATION_ID],
  claim_refs: [],
  created_at: "2026-11-22T08:00:00Z",
};

const REVIEW_OBSERVATION: InvestigationObservation = {
  id: OBSERVATION_ID,
  kind: "binary_analysis",
  sha256: BLOB_HASH,
  source: "analysis-worker",
  source_version: "1",
  observed_at: "2026-11-22T08:00:00Z",
  object_type: "binary",
  object_id: "sample-1",
  limitations: [],
  invocation_ref: null,
};

const CONTENT_BLOCKS: NoteContentBlockV1[] = [
  {
    block_type: "article_element",
    kind: "table",
    element_key: "table-commands",
    element_sha256: ELEMENT_HASH,
    anchor: "lead:0001",
  },
  {
    block_type: "canonical_code",
    observation_id: OBSERVATION_ID,
    blob_sha256: BLOB_HASH,
    offset: 4,
    length: 18,
    excerpt_sha256: EXCERPT_HASH,
    language: "bash",
  },
  {
    block_type: "ioc_table",
    observation_ids: [OBSERVATION_ID],
    title: "Tableau IOC (1)",
  },
  {
    block_type: "protocol_figure",
    kind: "sequence",
    asset_sha256: ASSET_HASH,
  },
];

const RESOLVED_BLOCKS: ResolvedNoteContentBlockV1[] = [
  {
    ...(CONTENT_BLOCKS[0] as Extract<
      NoteContentBlockV1,
      { block_type: "article_element" }
    >),
    title: "Commandes observées",
    caption: "Commandes exécutées par l’échantillon.",
  },
  {
    ...(CONTENT_BLOCKS[1] as Extract<
      NoteContentBlockV1,
      { block_type: "canonical_code" }
    >),
    excerpt: "cmd.exe /c whoami",
    observation_source: "analysis-worker",
    observation_object_type: "binary",
    observation_object_id: "sample-1",
  },
  {
    ...(CONTENT_BLOCKS[2] as Extract<
      NoteContentBlockV1,
      { block_type: "ioc_table" }
    >),
    rows: [
      {
        observation_id: OBSERVATION_ID,
        object_type: "binary",
        object_id: "sample-1",
        source: "analysis-worker",
        observed_at: "2026-11-22T08:00:00Z",
        blob_sha256: BLOB_HASH,
      },
    ],
  },
  {
    ...(CONTENT_BLOCKS[3] as Extract<
      NoteContentBlockV1,
      { block_type: "protocol_figure" }
    >),
    caption: "Séquence des échanges observée.",
  },
];

function makeProposal(): NoteProposalViewV1 {
  return {
    id: PROPOSAL_ID,
    investigation_id: INVESTIGATION_ID,
    opinion_id: OPINION_ID,
    task_id: null,
    anchor: "lead:0001",
    target_block_type: "lead",
    bridge_block_type: "key_findings",
    consultation_type: "draft_text",
    base_note_id: NOTE_ID,
    note_state: "observed",
    note_relation: "confirms",
    cited_finding_refs: [FINDING_ID],
    cited_observation_refs: [OBSERVATION_ID],
    proposed_text: "Texte Bridge proposé pour le chapô.",
    proposed_text_sha256: PROPOSED_HASH,
    bridge_invocation_id: "bridge-turn-1",
    bridge_output_sha256: "1".repeat(64),
    bridge_response_observation_id: OBSERVATION_ID,
    tool_invocation_id: null,
    base_opinion_version: 3,
    base_opinion_sha256: "2".repeat(64),
    base_publication_artifact_id: BASE_ID,
    base_publication_version: 3,
    base_publication_sha256: BASE_SHA,
    anchor_before_sha256: "3".repeat(64),
    content_blocks: CONTENT_BLOCKS,
    status: "proposed",
    status_version: 4,
    accepted_opinion_version: null,
    analyst_decision_id: null,
    idempotency_key: "idem-1",
    materialised: true,
    created_at: "2026-11-22T09:00:00Z",
    updated_at: "2026-11-22T09:00:00Z",
  };
}

function makeDiff(
  overrides: Partial<NoteProposalDiffViewV1> = {},
): NoteProposalDiffViewV1 {
  const proposal = makeProposal();
  return {
    proposal,
    current_opinion_version: 3,
    current_opinion_sha256: "2".repeat(64),
    current_note_id: NOTE_ID,
    current_note_text: "Note actuelle du chapô.",
    proposed_text: proposal.proposed_text,
    text_changed: true,
    anchor: proposal.anchor,
    target_block_type: proposal.target_block_type,
    anchor_excerpt: "Extrait du chapô.",
    cited_finding_refs: proposal.cited_finding_refs,
    cited_observation_refs: [OBSERVATION_ID],
    content_blocks: CONTENT_BLOCKS,
    resolved_content_blocks: RESOLVED_BLOCKS,
    content_blocks_valid: true,
    content_block_error: null,
    limits: [],
    stale: false,
    accept_ready: true,
    ...overrides,
  };
}

function renderReview(
  diff: NoteProposalDiffViewV1,
  onRequestCorrection: (seed: {
    anchor: string;
    blockType: NoteProposalViewV1["bridge_block_type"];
    contentBlocks: NoteContentBlockV1[];
    findingRefs: string[];
    observationRefs: string[];
    noteState: NoteProposalViewV1["note_state"];
    noteRelation: NoteProposalViewV1["note_relation"];
    taskId: string | null;
  }) => void = () => undefined,
) {
  let reads = 0;
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  installFetch((request) => {
    calls.push({ url: request.url, init: request.init });
    if (
      request.url ===
      `/api/investigations/${INVESTIGATION_ID}/note-proposals/${PROPOSAL_ID}`
    ) {
      reads += 1;
      return jsonResponse(diff);
    }
    if (
      request.url === `/api/investigations/${INVESTIGATION_ID}/article-anchors`
    ) {
      return jsonResponse({
        base_publication_artifact_id: BASE_ID,
        base_publication_version: 3,
        base_publication_sha256: BASE_SHA,
        source: "pinned_opinion_base",
        anchors: [
          {
            anchor: "lead:0001",
            text: "Texte du chapô source.",
            text_sha256: "0".repeat(64),
            section_index: null,
            paragraph_index: 0,
          },
        ],
        elements: [],
        truncated: false,
        sections: [],
      });
    }
    if (
      request.url ===
      `/api/investigations/${INVESTIGATION_ID}/note-proposals/${PROPOSAL_ID}/accept`
    ) {
      return jsonResponse({
        proposal: { ...diff.proposal, status: "accepted", status_version: 5 },
        opinion_id: OPINION_ID,
        opinion_version: 4,
        opinion_sha256: "9".repeat(64),
        decision_id: "88888888-8888-4888-8888-888888888888",
        note_id: NOTE_ID,
        idempotent_replay: false,
      });
    }
    return jsonResponse({ detail: { code: "unexpected_request" } }, 500);
  });
  const client = createTestQueryClient();
  const result = render(
    <NoteProposalReview
      investigationId={INVESTIGATION_ID}
      proposalId={PROPOSAL_ID}
      findings={[REVIEW_FINDING]}
      observations={[REVIEW_OBSERVATION]}
      subjectId={SUBJECT_ID}
      onClose={() => undefined}
      onRequestCorrection={onRequestCorrection}
    />,
    { wrapper: wrapperFor(client) },
  );
  return { ...result, calls, readCount: () => reads };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("NoteProposalReview — blocs de contenu", () => {
  it("affiche les blocs résolus, leur provenance et le lien vers l’atelier", async () => {
    renderReview(makeDiff());

    expect(
      await screen.findByRole("heading", {
        name: "Aperçu lecture seule des blocs vérifiés",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Commandes observées/)).toBeInTheDocument();
    expect(
      screen.getByText(/cross-référence vers l’ancre/),
    ).toBeInTheDocument();
    expect(screen.getByText("cmd.exe /c whoami")).toBeInTheDocument();
    expect(
      screen.getByRole("table", { name: "Tableau IOC (1)" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Séquence des échanges observée/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", {
        name: "Ouvrir dans l’atelier éditorial",
      }),
    ).toHaveAttribute("href", `/subjects/${SUBJECT_ID}/editor`);
  });

  it("accepte une proposition dont les blocs sont valides, sans envoyer de texte", async () => {
    const user = userEvent.setup();
    const { calls } = renderReview(makeDiff());

    await screen.findByRole("button", { name: "Accepter" });
    await user.click(
      screen.getByRole("checkbox", {
        name: /J’ai relu ce texte et ses références/,
      }),
    );
    const accept = screen.getByRole("button", {
      name: "Accepter",
    });
    expect(accept).toBeEnabled();
    await user.click(accept);

    await waitFor(() => {
      expect(
        calls.filter((call) => call.url.endsWith(`/${PROPOSAL_ID}/accept`))
          .length,
      ).toBe(1);
    });
    const request = calls.find((call) =>
      call.url.endsWith(`/${PROPOSAL_ID}/accept`),
    );
    const body = bodyOf(request?.init) as Record<string, unknown>;
    expect(body).toMatchObject({
      expected_status_version: 4,
      expected_base_version: 3,
      expected_base_sha256: "2".repeat(64),
    });
    expect(body).not.toHaveProperty("text");
    expect(body).not.toHaveProperty("proposed_text");
    expect(body).not.toHaveProperty("content_blocks");
  });

  it("présente les sections dans l’ordre demandé, avec une provenance repliée", async () => {
    const user = userEvent.setup();
    renderReview(makeDiff());
    await screen.findByRole("heading", { name: "Texte proposé" });

    const headings = [
      "Texte proposé",
      "Diff avec la Note actuelle",
      "Références citées",
      "Aperçu à cet emplacement",
      "Décision",
    ].map((name) => screen.getByRole("heading", { name }));
    expect(
      headings.every((heading, index) =>
        index === headings.length - 1
          ? true
          : Boolean(
              headings[index + 1] &&
              (heading.compareDocumentPosition(headings[index + 1]!) &
                Node.DOCUMENT_POSITION_FOLLOWING) !==
                0,
            ),
      ),
    ).toBe(true);
    expect(
      screen.getByRole("heading", {
        name: "Proposition Bridge — version 4",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("Emplacement").parentElement).toHaveTextContent(
      "Chapô, paragraphe 1 · « Extrait du chapô. »",
    );
    expect(screen.getByText("1 finding · 1 observation")).toBeInTheDocument();
    expect(
      screen.getByText(/Le serveur répond à une requête de commande/),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Analyse binaire · analysis-worker · binary sample-1"),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Différences" }));
    expect(screen.getByText(/Supprimé/)).toBeInTheDocument();
    expect(screen.getByText(/Ajouté/)).toBeInTheDocument();

    const technical = screen
      .getByText("Provenance technique")
      .closest("details");
    expect(technical).not.toHaveAttribute("open");
    expect(technical).toHaveTextContent(FINDING_ID);
    expect(technical).toHaveTextContent(OBSERVATION_ID);
    expect(
      screen
        .getAllByRole("button")
        .every((button) => !/[a-f0-9]{12,}/i.test(button.textContent ?? "")),
    ).toBe(true);
  });

  it("n’affiche pas de diff sans Note existante", async () => {
    renderReview(makeDiff({ current_note_id: null, current_note_text: null }));

    await screen.findByRole("heading", { name: "Texte proposé" });
    expect(
      screen.queryByRole("heading", { name: "Diff avec la Note actuelle" }),
    ).not.toBeInTheDocument();
  });

  it("transmet la demande de correction au panneau Bridge sans mutation d’acceptation", async () => {
    const user = userEvent.setup();
    const onRequestCorrection = vi.fn();
    const { calls } = renderReview(makeDiff(), onRequestCorrection);

    await user.click(
      await screen.findByRole("button", {
        name: "Demander une correction à Bridge",
      }),
    );

    expect(onRequestCorrection).toHaveBeenCalledWith({
      anchor: "lead:0001",
      blockType: "key_findings",
      contentBlocks: CONTENT_BLOCKS,
      findingRefs: [FINDING_ID],
      observationRefs: [OBSERVATION_ID],
      noteState: "observed",
      noteRelation: "confirms",
      taskId: null,
    });
    expect(
      calls.some((call) => call.url.endsWith(`/${PROPOSAL_ID}/accept`)),
    ).toBe(false);
  });

  it("refuse l’acceptation quand un bloc est périmé et recharge la proposition", async () => {
    const user = userEvent.setup();
    const diff = makeDiff({
      resolved_content_blocks: [],
      content_blocks_valid: false,
      content_block_error: "note_block_ref_stale",
      accept_ready: false,
    });
    const { readCount } = renderReview(diff);

    expect(
      await screen.findByText(
        /Un élément d’article référencé a changé \(hash ou ancre\)/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("note_block_ref_stale")).toBeInTheDocument();
    await user.click(
      screen.getByRole("checkbox", {
        name: /J’ai relu ce texte et ses références/,
      }),
    );
    expect(screen.getByRole("button", { name: "Accepter" })).toBeDisabled();

    const readsBefore = readCount();
    await user.click(
      screen.getByRole("button", { name: "Recharger la proposition" }),
    );
    await waitFor(() => expect(readCount()).toBeGreaterThan(readsBefore));
  });
});
