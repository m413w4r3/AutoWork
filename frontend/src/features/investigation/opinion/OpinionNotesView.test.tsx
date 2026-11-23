import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  InvestigationFinding,
  InvestigationObservation,
  InvestigationView,
} from "../../../api/investigation";
import type { InvestigationActivityPage } from "../../../api/investigationOperator";
import type { ArticleAnchorCatalogViewV1 } from "../api/articleAnchors";
import type {
  NoteAcceptanceViewV1,
  NoteProposalDiffViewV1,
  NoteProposalViewV1,
} from "../api/opinionProposals";
import type { TaskPageView } from "../api/tasks";
import {
  bodyOf,
  createTestQueryClient,
  errorResponse,
  headersOf,
  installFetch,
  jsonResponse,
  wrapperFor,
} from "../api/testSupport";
import { OpinionNotesView } from "./OpinionNotesView";
import type {
  AnalystNoteV1,
  OpinionReadPayloadV1,
  OpinionVersionV1,
} from "./types";

const INVESTIGATION_ID = "11111111-1111-4111-8111-111111111111";
const OPINION_ID = "22222222-2222-4222-8222-222222222222";
const PROPOSAL_ID = "33333333-3333-4333-8333-333333333333";
const NOTE_LEAD_ID = "44444444-4444-4444-8444-444444444444";
const NOTE_SECTION_ID = "55555555-5555-4555-8555-555555555555";
const FINDING_ID = "66666666-6666-4666-8666-666666666666";
const OBSERVATION_ID = "77777777-7777-4777-8777-777777777777";
const DECISION_ID = "88888888-8888-4888-8888-888888888888";
const TASK_ID = "99999999-9999-4999-8999-999999999999";
const BRIDGE_RESPONSE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const CONTENT_HASH = "a".repeat(64);
const PROPOSED_HASH = "b".repeat(64);
const BASE_HASH = "c".repeat(64);
const APPROVED_HASH = "9".repeat(64);

function makeNote(overrides: Partial<AnalystNoteV1> = {}): AnalystNoteV1 {
  return {
    id: NOTE_LEAD_ID,
    anchor: "lead:0001",
    text: "Le chapô observe un service de commande.",
    state: "observed",
    relation: "confirms",
    finding_refs: [FINDING_ID],
    evidence_refs: [OBSERVATION_ID],
    bridge_authored: true,
    bridge_block_type: "key_findings",
    bridge_draft_ref: {
      invocation_id: "bridge-turn-1",
      text_sha256: "d".repeat(64),
    },
    bridge_modified_by_operator: false,
    ...overrides,
  };
}

function makeSectionNote(): AnalystNoteV1 {
  return makeNote({
    id: NOTE_SECTION_ID,
    anchor: "section:2:paragraph:0007",
    text: "La section documente une persistance.",
    state: "inferred",
    relation: "qualifies",
    bridge_block_type: "network_and_infrastructure",
  });
}

function makeOpinion(
  overrides: Partial<OpinionVersionV1> = {},
): OpinionVersionV1 {
  const blocks = [
    {
      type: "key_findings" as const,
      text: "Une variante distincte est étayée.",
      finding_refs: [FINDING_ID],
      evidence_refs: [OBSERVATION_ID],
      qualification: "fact" as const,
      certainty: 0.9,
    },
  ];
  return {
    schema: "analyst-opinion.v1",
    id: OPINION_ID,
    version: 3,
    investigation_id: INVESTIGATION_ID,
    synthesis_artifact_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    synthesis_sha256: "1".repeat(64),
    base_publication_artifact_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    base_publication_sha256: BASE_HASH,
    blocks,
    protocol_figures: [
      {
        kind: "sequence",
        model_sha256: "2".repeat(64),
        spec_sha256: "3".repeat(64),
        asset_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        asset_sha256: "4".repeat(64),
        preview_sha256: "5".repeat(64),
        pin_sha256: "6".repeat(64),
        compiler_name: "typst",
        compiler_version: "1",
        compiler_policy_version: "1",
        caption: "Séquence des échanges observée.",
      },
    ],
    unresolved_points: ["La date de première apparition reste inconnue."],
    author_id: "system:opinion-writer",
    reviewer_id: null,
    status: "draft",
    created_at: "2026-11-22T09:00:00Z",
    content_sha256: CONTENT_HASH,
    renderable_blocks: blocks,
    implant_materials_section: null,
    provenance: {
      investigation_id: INVESTIGATION_ID,
      synthesis_artifact_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      synthesis_sha256: "1".repeat(64),
      base_publication_artifact_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
      base_publication_sha256: BASE_HASH,
    },
    editorial_review: {
      status: "clear",
      bridge_drafting: {
        status: "complete",
        section_count: 2,
        bridge_authored_section_count: 2,
        unlinked_sections: [],
        requires_acknowledgement: false,
        acknowledged: true,
        resume_available: false,
        diagnosis: null,
      },
      publication_text_markers: [],
      missing_annex_references: [],
      warnings: [],
    },
    observations: [makeNote(), makeSectionNote()],
    ...overrides,
  };
}

function makePayload(
  opinion: OpinionVersionV1,
  citationViolations: OpinionReadPayloadV1["citation_violations"] = [],
): OpinionReadPayloadV1 {
  return {
    draft: opinion,
    history: [opinion],
    citation_violations: citationViolations,
    renderable_blocks: opinion.renderable_blocks,
    provenance: opinion.provenance,
  };
}

function makeFinding(): InvestigationFinding {
  return {
    id: FINDING_ID,
    logical_id: "finding-logical-1",
    current_version_id: FINDING_ID,
    version: 1,
    revision_chain: [],
    kind: "network_behaviour",
    assessment: "supported",
    novelty: "new_to_article",
    statement: "Le service de commande contacte un domaine fixe.",
    scope: "échantillon",
    limitations: [],
    observation_refs: [OBSERVATION_ID],
    claim_refs: [],
    created_at: "2026-11-22T08:00:00Z",
  };
}

function makeObservation(): InvestigationObservation {
  return {
    id: OBSERVATION_ID,
    kind: "binary_analysis",
    sha256: "e".repeat(64),
    source: "analysis-worker",
    source_version: "1",
    observed_at: "2026-11-22T08:00:00Z",
    object_type: "binary",
    object_id: "sample-1",
    limitations: [],
    invocation_ref: null,
  };
}

function makeInvestigation(): InvestigationView {
  const dimension = { limit: null, consumed: 0, reserved: 0, remaining: null };
  return {
    id: INVESTIGATION_ID,
    subject_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    production_run_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    synthesis_artifact_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    synthesis_sha256: "1".repeat(64),
    prose_authority: "bridge_only",
    status: "running",
    version: 7,
    stop_reason: null,
    agenda: [],
    hypotheses: [],
    findings: [makeFinding()],
    observations: [makeObservation()],
    sample_associations: [],
    opinion_draft: null,
    budget: {
      bridge_turns: dimension,
      vt_units: dimension,
      shodan_credits: dimension,
      downloads: dimension,
      tool_seconds: dimension,
      context_tokens: dimension,
      context_bytes: dimension,
    },
    blocked_decisions: [],
    available_tools: [],
    unavailable_tools: [],
    unavailable_tool_reasons: {},
    envelope_approved: false,
    allowed_providers: [],
    acquire_hits_allowed: false,
  };
}

function makeActivity(): InvestigationActivityPage {
  return {
    schema_version: "investigation-activity.v1",
    investigation: {
      id: INVESTIGATION_ID,
      subject_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      production_run_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
      synthesis_artifact_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      synthesis_sha256: "1".repeat(64),
      status: "running",
      version: 7,
      stop_reason: null,
      execution_mode: "operator_led",
    },
    operator: {
      principal: null,
      model: null,
      effort: null,
      active_subagent_count: null,
      metadata_source: "unavailable_in_autowork",
    },
    working_state: {
      version: 5,
      turn_index: 3,
      stop_reason: null,
      hypothesis_count: 0,
      open_hypothesis_count: 0,
    },
    last_activity_at: null,
    last_activity_source: null,
    active_backend_job_count: 0,
    budget: null,
    timeline: [],
    next_cursor: null,
    has_more: false,
    blockers: [],
    counts: { inconclusive_findings: 0 },
    generated_at: "2026-11-22T09:00:00Z",
  };
}

function makeTasks(): TaskPageView {
  return {
    total: 1,
    items: [
      {
        id: TASK_ID,
        investigation_id: INVESTIGATION_ID,
        task_key: "T-1",
        plan_version_id: null,
        parent_task_id: null,
        depth: 0,
        title: "Vérifier la persistance",
        question: "La persistance est-elle réelle ?",
        objective: "Confirmer la persistance observée.",
        priority: "normal",
        claim_refs: [],
        evidence_refs: [],
        sample_refs: [],
        required_capabilities: [],
        allowed_workspace_paths: [],
        execution_policy: {},
        expected_outputs: [],
        status: "ready",
        status_version: 1,
        result_verification_status: "unverified",
        blocked_reason: null,
        assigned_actor_kind: null,
        assigned_actor_id: null,
        lease_id: null,
        lease_session_id: null,
        lease_fence: 0,
        lease_expires_at: null,
        heartbeat_at: null,
        handoff_ref: null,
        result_refs: [],
        event_sequence: 0,
        created_by: { actor_kind: "human", actor_id: "analyst:test" },
        created_at: "2026-11-22T08:00:00Z",
        updated_at: "2026-11-22T08:00:00Z",
      },
    ],
  };
}

function makeAnchorCatalog(): ArticleAnchorCatalogViewV1 {
  return {
    base_publication_artifact_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    base_publication_version: 3,
    base_publication_sha256: BASE_HASH,
    source: "pinned_opinion_base",
    anchors: [
      {
        anchor: "lead:0001",
        text: "Le chapô observe un service de commande.",
        text_sha256: "d".repeat(64),
        section_index: null,
        paragraph_index: 0,
      },
      {
        anchor: "section:2:paragraph:0007",
        text: "La section documente une persistance.",
        text_sha256: "e".repeat(64),
        section_index: 2,
        paragraph_index: 6,
      },
    ],
    elements: [],
    truncated: false,
  };
}

function makeProposal(
  overrides: Partial<NoteProposalViewV1> = {},
): NoteProposalViewV1 {
  return {
    id: PROPOSAL_ID,
    investigation_id: INVESTIGATION_ID,
    opinion_id: OPINION_ID,
    task_id: null,
    anchor: "lead:0001",
    target_block_type: "lead",
    bridge_block_type: "key_findings",
    consultation_type: "draft_text",
    base_note_id: null,
    note_state: "observed",
    note_relation: "confirms",
    cited_finding_refs: [FINDING_ID],
    cited_observation_refs: [OBSERVATION_ID],
    proposed_text: "Texte Bridge proposé pour le chapô.",
    proposed_text_sha256: PROPOSED_HASH,
    bridge_invocation_id: "bridge-turn-9",
    bridge_output_sha256: "f".repeat(64),
    bridge_response_observation_id: BRIDGE_RESPONSE_ID,
    tool_invocation_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    base_opinion_version: 3,
    base_opinion_sha256: CONTENT_HASH,
    base_publication_artifact_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    base_publication_version: 3,
    base_publication_sha256: BASE_HASH,
    anchor_before_sha256: "d".repeat(64),
    status: "proposed",
    status_version: 4,
    accepted_opinion_version: null,
    analyst_decision_id: null,
    idempotency_key: "idem-proposal-1",
    materialised: true,
    created_at: "2026-11-22T09:10:00Z",
    updated_at: "2026-11-22T09:10:00Z",
    ...overrides,
  };
}

function makeDiff(
  proposal: NoteProposalViewV1,
  overrides: Partial<NoteProposalDiffViewV1> = {},
): NoteProposalDiffViewV1 {
  return {
    proposal,
    current_opinion_version: 3,
    current_opinion_sha256: CONTENT_HASH,
    current_note_id: NOTE_LEAD_ID,
    current_note_text: "Note actuelle du chapô.",
    proposed_text: proposal.proposed_text,
    text_changed: true,
    anchor: proposal.anchor,
    target_block_type: proposal.target_block_type,
    anchor_excerpt: "Extrait du chapô.",
    cited_finding_refs: proposal.cited_finding_refs,
    cited_observation_refs: proposal.cited_observation_refs,
    limits: ["Limite signalée par Bridge."],
    stale: false,
    accept_ready: true,
    ...overrides,
  };
}

function makeAcceptance(proposal: NoteProposalViewV1): NoteAcceptanceViewV1 {
  return {
    proposal: {
      ...proposal,
      status: "accepted",
      status_version: 5,
      accepted_opinion_version: 4,
      analyst_decision_id: DECISION_ID,
    },
    opinion_id: OPINION_ID,
    opinion_version: 4,
    opinion_sha256: "8".repeat(64),
    decision_id: DECISION_ID,
    note_id: NOTE_LEAD_ID,
    idempotent_replay: false,
  };
}

interface RenderOptions {
  opinion?: OpinionVersionV1;
  proposals?: NoteProposalViewV1[];
  diff?: NoteProposalDiffViewV1;
  requestFailure?: Response;
  acceptFailure?: Response;
  approveFailure?: Response;
  ingestPending?: boolean;
}

function renderView(options: RenderOptions = {}) {
  let currentOpinion = options.opinion ?? makeOpinion();
  const proposal = options.proposals?.[0] ?? makeProposal();
  const proposals = options.proposals ?? [proposal];
  const diff = options.diff ?? makeDiff(proposal);
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  let opinionReads = 0;
  let diffReads = 0;

  installFetch((request) => {
    calls.push({ url: request.url, init: request.init });
    const method = request.init?.method ?? "GET";
    const url = request.url;
    if (url === `/api/investigations/${INVESTIGATION_ID}/opinion`) {
      opinionReads += 1;
      return jsonResponse(makePayload(currentOpinion));
    }
    if (url === `/api/investigations/${INVESTIGATION_ID}`) {
      return jsonResponse(makeInvestigation());
    }
    if (url === `/api/investigations/${INVESTIGATION_ID}/tasks`) {
      return jsonResponse(makeTasks());
    }
    if (url.startsWith(`/api/investigations/${INVESTIGATION_ID}/activity`)) {
      return jsonResponse(makeActivity());
    }
    if (url === `/api/investigations/${INVESTIGATION_ID}/article-anchors`) {
      return jsonResponse(makeAnchorCatalog());
    }
    if (
      url.startsWith(`/api/investigations/${INVESTIGATION_ID}/note-proposals?`)
    ) {
      return jsonResponse(proposals);
    }
    if (url === `/api/investigations/${INVESTIGATION_ID}/note-proposals`) {
      if (method === "POST") {
        if (options.requestFailure !== undefined) return options.requestFailure;
        return jsonResponse({
          proposal,
          invocation_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
          invocation_status: "succeeded",
          idempotent_replay: false,
          materialised: true,
        });
      }
      return jsonResponse(proposals);
    }
    if (
      url ===
      `/api/investigations/${INVESTIGATION_ID}/note-proposals/${PROPOSAL_ID}`
    ) {
      diffReads += 1;
      return jsonResponse(diff);
    }
    if (
      url ===
      `/api/investigations/${INVESTIGATION_ID}/note-proposals/${PROPOSAL_ID}/accept`
    ) {
      if (options.acceptFailure !== undefined) return options.acceptFailure;
      return jsonResponse(makeAcceptance(proposal));
    }
    if (
      url ===
      `/api/investigations/${INVESTIGATION_ID}/note-proposals/${PROPOSAL_ID}/reject`
    ) {
      return jsonResponse({
        ...proposal,
        status: "rejected",
        status_version: 5,
      });
    }
    if (
      url ===
      `/api/investigations/${INVESTIGATION_ID}/note-proposals/${PROPOSAL_ID}/ingest`
    ) {
      if (options.ingestPending === true) {
        return jsonResponse(
          {
            detail: {
              code: "bridge_response_pending",
              message: "Bridge n’a pas encore répondu.",
            },
          },
          202,
        );
      }
      return jsonResponse({ ...proposal, status_version: 5 });
    }
    if (url === `/api/opinions/${OPINION_ID}/approve`) {
      if (options.approveFailure !== undefined) return options.approveFailure;
      currentOpinion = makeOpinion({
        status: "approved",
        version: 4,
        content_sha256: APPROVED_HASH,
      });
      return jsonResponse({
        approved: currentOpinion,
        reviewed_version: 3,
        approved_version: 4,
      });
    }
    if (url === `/api/opinions/${OPINION_ID}/publish`) {
      return jsonResponse({
        publication: {
          artifact_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
          run_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
          subject_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
          version: 5,
          sha256: "7".repeat(64),
          previous_publication_artifact_id:
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        },
        composition_repin_required: true,
        message:
          "Nouvelle PUBLICATION créée. Le réépinglage reste une action de l’éditeur.",
      });
    }
    return errorResponse(500, "unexpected_request", url);
  });

  const client = createTestQueryClient();
  const result = render(
    <OpinionNotesView investigationId={INVESTIGATION_ID} />,
    {
      wrapper: wrapperFor(client),
    },
  );
  return {
    ...result,
    calls,
    opinionReadCount: () => opinionReads,
    diffReadCount: () => diffReads,
  };
}

async function openProposalReview() {
  await screen.findByRole("heading", {
    name: "Demander une proposition à Bridge",
  });
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "Examiner la proposition" }));
  await screen.findByRole("button", { name: /Accepter la proposition v4/ });
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
});

describe("OpinionNotesView — notes Bridge par ancre", () => {
  it("liste plusieurs notes à des ancres différentes", async () => {
    renderView();

    const list = await screen.findByRole("region", { name: "Notes par ancre" });
    expect(
      within(list).getByRole("heading", { name: /lead:0001/ }),
    ).toBeInTheDocument();
    expect(
      within(list).getByRole("heading", {
        name: /section:2:paragraph:0007/,
      }),
    ).toBeInTheDocument();
    expect(
      within(list).getByText("Le chapô observe un service de commande."),
    ).toBeInTheDocument();
    expect(
      within(list).getByText("La section documente une persistance."),
    ).toBeInTheDocument();
  });

  it("accepte une proposition avec les pins version/hash et une clé d’idempotence", async () => {
    const user = userEvent.setup();
    const { calls } = renderView();
    await openProposalReview();

    expect(
      screen.getByText(/L’acceptation remplace la note existante/),
    ).toBeInTheDocument();
    const acceptButton = screen.getByRole("button", {
      name: /Accepter la proposition v4/,
    });
    expect(acceptButton).toBeDisabled();

    await user.click(
      screen.getByRole("checkbox", {
        name: /J’ai examiné le texte stocké, le diff et l’aperçu/,
      }),
    );
    expect(acceptButton).toBeEnabled();
    await user.type(
      screen.getByRole("textbox", {
        name: /Raison de la décision \(journalisée, jamais publiée\)/,
      }),
      "Acceptation motivée",
    );
    await user.click(acceptButton);

    await waitFor(() => {
      expect(
        calls.some((call) => call.url.endsWith(`/${PROPOSAL_ID}/accept`)),
      ).toBe(true);
    });
    const acceptRequest = calls.find((call) =>
      call.url.endsWith(`/${PROPOSAL_ID}/accept`),
    );
    expect(acceptRequest).toBeDefined();
    expect(headersOf(acceptRequest?.init)["Idempotency-Key"]).toBeTruthy();
    const body = bodyOf(acceptRequest?.init) as Record<string, unknown>;
    expect(body).toMatchObject({
      expected_status_version: 4,
      expected_base_version: 3,
      expected_base_sha256: CONTENT_HASH,
      reason: "Acceptation motivée",
    });
    expect(body).not.toHaveProperty("text");
    expect(body).not.toHaveProperty("proposed_text");

    expect(await screen.findByText("Note acceptée")).toBeInTheDocument();
    expect(screen.getByText(/Avis v4/)).toBeInTheDocument();
  });

  it("refuse une proposition sans observation de réponse et expose l’attente Bridge", async () => {
    const user = userEvent.setup();
    const pendingProposal = makeProposal({
      materialised: false,
      proposed_text: null,
      proposed_text_sha256: null,
      bridge_invocation_id: null,
      bridge_output_sha256: null,
      bridge_response_observation_id: null,
    });
    renderView({
      proposals: [pendingProposal],
      diff: makeDiff(pendingProposal, {
        proposed_text: null,
        text_changed: false,
        accept_ready: false,
      }),
      ingestPending: true,
    });
    await openProposalReview();

    expect(screen.getAllByText(/Aucun texte stocké/).length).toBeGreaterThan(0);
    expect(
      screen.getByRole("button", { name: /Accepter la proposition v4/ }),
    ).toBeDisabled();

    await user.click(
      screen.getByRole("button", { name: "Ingérer la réponse Bridge" }),
    );
    expect(
      await screen.findByText(
        /Bridge n’a pas encore renvoyé de réponse exploitable/,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/code : bridge_response_pending/),
    ).toBeInTheDocument();
  });

  it("refuse une proposition périmée, affiche le code et recharge les données", async () => {
    const user = userEvent.setup();
    const { diffReadCount } = renderView({
      acceptFailure: errorResponse(409, "stale_note_proposal"),
    });
    await openProposalReview();

    await user.click(
      screen.getByRole("checkbox", {
        name: /J’ai examiné le texte stocké, le diff et l’aperçu/,
      }),
    );
    await user.click(
      screen.getByRole("button", { name: /Accepter la proposition v4/ }),
    );

    expect(
      await screen.findByText(/Proposition périmée : l’avis a changé/),
    ).toBeInTheDocument();
    expect(screen.getByText(/code : stale_note_proposal/)).toBeInTheDocument();
    await waitFor(() => expect(diffReadCount()).toBeGreaterThanOrEqual(2));
  });

  it("ne propose aucune saisie libre de texte éditorial et n’envoie que la question bornée", async () => {
    const user = userEvent.setup();
    const { calls, container } = renderView();

    await screen.findByRole("heading", {
      name: "Demander une proposition à Bridge",
    });
    expect(
      screen.queryByRole("textbox", {
        name: /(texte|proposition).*(avis|note|bloc)/i,
      }),
    ).toBeNull();
    expect(
      await screen.findByRole("combobox", { name: "Ancre de publication" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Base de publication :/)).toBeInTheDocument();
    expect(screen.queryByText(/n’expose aucune lecture des/)).toBeNull();
    expect(container.querySelector("datalist")).toBeNull();
    expect(
      screen.getByText("Diagnostic — saisie manuelle d’ancre"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Enregistrer la révision/i }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Composer depuis les constats/i }),
    ).toBeNull();

    const submit = screen.getByRole("button", { name: "Demander à Bridge" });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);

    await waitFor(() => {
      expect(
        calls.filter(
          (call) =>
            call.url ===
              `/api/investigations/${INVESTIGATION_ID}/note-proposals` &&
            call.init?.method === "POST",
        ).length,
      ).toBe(1);
    });
    const request = calls.find(
      (call) =>
        call.url === `/api/investigations/${INVESTIGATION_ID}/note-proposals` &&
        call.init?.method === "POST",
    );
    const body = bodyOf(request?.init) as Record<string, unknown>;
    expect(body).toMatchObject({
      anchor: "lead:0001",
      target_block_type: "lead",
      bridge_block_type: "key_findings",
      consultation_type: "draft_text",
      expected_working_state_version: 5,
      base_opinion_version: 3,
      base_opinion_sha256: CONTENT_HASH,
    });
    expect(body).not.toHaveProperty("text");
    expect(body).not.toHaveProperty("blocks");
    expect(body).not.toHaveProperty("proposed_text");
    expect(body.question).toEqual(expect.stringContaining("Rédige"));
    expect(body.question).not.toEqual(
      expect.stringContaining("Le chapô observe un service de commande."),
    );
  });

  it("reste entièrement opérationnel au clavier", async () => {
    const user = userEvent.setup();
    const { calls } = renderView();
    await screen.findByRole("heading", {
      name: "Demander une proposition à Bridge",
    });

    const develop = screen.getByRole("radio", { name: "Développer" });
    develop.focus();
    await user.keyboard(" ");
    expect(develop).toBeChecked();

    const submit = screen.getByRole("button", { name: "Demander à Bridge" });
    await waitFor(() => expect(submit).toBeEnabled());
    submit.focus();
    await user.keyboard("{Enter}");

    await waitFor(() => {
      const request = calls.find(
        (call) =>
          call.url ===
            `/api/investigations/${INVESTIGATION_ID}/note-proposals` &&
          call.init?.method === "POST",
      );
      expect(request).toBeDefined();
      const body = bodyOf(request?.init) as { question: string };
      expect(body.question).toEqual(expect.stringContaining("Développe"));
    });
  });

  it("signale Bridge indisponible avec le code typé", async () => {
    const user = userEvent.setup();
    renderView({
      requestFailure: errorResponse(503, "bridge_unavailable", "Bridge arrêté"),
    });
    await screen.findByRole("heading", {
      name: "Demander une proposition à Bridge",
    });
    const submit = screen.getByRole("button", { name: "Demander à Bridge" });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);

    expect(
      await screen.findByText(/Impossible de demander la proposition/),
    ).toBeInTheDocument();
    expect(screen.getByText(/code : bridge_unavailable/)).toBeInTheDocument();
  });

  it("approuve une version puis laisse la publication comme décision distincte", async () => {
    const user = userEvent.setup();
    const { calls } = renderView();
    await screen.findByRole("heading", {
      name: "Demander une proposition à Bridge",
    });

    await user.click(
      screen.getByRole("checkbox", {
        name: /J’ai consulté l’aperçu PDF réel de la version v3/,
      }),
    );
    await user.type(
      screen.getByRole("textbox", {
        name: /Raison de la décision d’approbation/,
      }),
      "Version conforme.",
    );
    await user.click(
      screen.getByRole("button", { name: /Approuver l’avis v3/ }),
    );

    await waitFor(() => {
      const request = calls.find(
        (call) => call.url === `/api/opinions/${OPINION_ID}/approve`,
      );
      expect(request).toBeDefined();
      expect(bodyOf(request?.init)).toMatchObject({
        version: 3,
        expected_sha256: CONTENT_HASH,
        reason: "Version conforme.",
      });
    });
    expect(calls.filter((call) => call.url.endsWith("/publish")).length).toBe(
      0,
    );
    expect(screen.getByText(/ne publie rien/)).toBeInTheDocument();

    expect(
      screen.getByText(/L’approbation n’a créé aucune publication/),
    ).toBeInTheDocument();
    const publishButton = await screen.findByRole("button", {
      name: /Proposer une PUBLICATION depuis l’avis v4/,
    });
    expect(publishButton).toBeDisabled();
    await user.click(
      await screen.findByRole("checkbox", {
        name: /J’ai consulté l’aperçu PDF réel de la version v4/,
      }),
    );
    expect(publishButton).toBeEnabled();
    await user.click(publishButton);

    await waitFor(() => {
      const request = calls.find(
        (call) => call.url === `/api/opinions/${OPINION_ID}/publish`,
      );
      expect(request).toBeDefined();
      expect(bodyOf(request?.init)).toMatchObject({
        version: 4,
        expected_base_publication_sha256: BASE_HASH,
      });
    });
    expect(
      await screen.findByText(/Réépinglage requis dans la composition/),
    ).toBeInTheDocument();
  });

  it("pré-remplit la tâche et l’intention de rédaction depuis un lien de carte tâche", async () => {
    const user = userEvent.setup();
    window.history.replaceState(
      {},
      "",
      `/?vue=opinion&task_id=${TASK_ID}&bridge_intent=draft_text`,
    );
    const { calls } = renderView();

    expect(
      await screen.findByRole("combobox", {
        name: "Tâche liée (facultatif)",
      }),
    ).toHaveValue(TASK_ID);
    expect(screen.getByRole("radio", { name: "Rédiger" })).toBeChecked();

    const submit = screen.getByRole("button", { name: "Demander à Bridge" });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);

    await waitFor(() => {
      const request = calls.find(
        (call) =>
          call.url ===
            `/api/investigations/${INVESTIGATION_ID}/note-proposals` &&
          call.init?.method === "POST",
      );
      expect(request).toBeDefined();
      expect(bodyOf(request?.init)).toMatchObject({
        task_id: TASK_ID,
        consultation_type: "draft_text",
      });
    });
  });

  it("présélectionne la vérification depuis un lien review_text", async () => {
    window.history.replaceState(
      {},
      "",
      `/?vue=opinion&task_id=${TASK_ID}&bridge_intent=review_text`,
    );
    renderView();

    const review = await screen.findByRole("radio", { name: "Vérifier" });
    await waitFor(() => expect(review).toBeChecked());
    expect(
      screen.getByRole("combobox", { name: "Tâche liée (facultatif)" }),
    ).toHaveValue(TASK_ID);
    expect(screen.getByRole("radio", { name: "Rédiger" })).not.toBeChecked();
  });
});
