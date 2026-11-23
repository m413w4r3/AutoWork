/**
 * Synthetic M24 cockpit API for the Playwright workshop spec.
 *
 * Every route the five cockpit views read or write is answered from in-memory
 * fixtures shaped like the frozen `src/features/investigation/api` contract.
 * No backend, sidecar or provider is contacted. Stateful flows (workspace
 * creation, G2 approval, plan acceptance, conflict resolution, proposal
 * acceptance) mutate this local state so invalidated TanStack queries observe
 * a coherent follow-up read.
 */

import type { Page, Route } from "@playwright/test";

import type {
  InvestigationFinding,
  InvestigationObservation,
  InvestigationView,
} from "../../src/api/investigation";
import type { InvestigationActivityPage } from "../../src/api/investigationOperator";
import type {
  InvestigationEnvelopeView,
  InvestigationSampleDetail,
} from "../../src/api/investigationGovernance";
import type {
  BootstrapEnvelopeApprovalV1,
  BootstrapEnvelopeProposalV1,
  BootstrapPlanV1,
  BootstrapSnapshotV1,
  IOCInventoryV1,
} from "../../src/features/investigation/api/bootstrap";
import type { ArticleAnchorCatalogViewV1 } from "../../src/features/investigation/api/articleAnchors";
import type {
  BudgetDimension,
  OperatorAdapterKind,
  SampleQualification,
} from "../../src/features/investigation/api/types";
import type { WorkContributionView } from "../../src/features/investigation/api/contributions";
import type {
  AgentSessionSummaryView,
  AgentSessionView,
} from "../../src/features/investigation/api/agentSessions";
import type {
  NoteAcceptanceViewV1,
  NoteProposalDiffViewV1,
  NoteProposalRequestResponseV1,
  NoteProposalViewV1,
} from "../../src/features/investigation/api/opinionProposals";
import type {
  ConsultationLedgerV1,
  PlanAcceptResponseV1,
  PlanListV1,
  PlanVersionDetailV1,
} from "../../src/features/investigation/api/plans";
import type {
  TaskPageView,
  TaskView,
  TimelinePageV1,
} from "../../src/features/investigation/api/tasks";
import type { WorkspaceAgentStatusResponse } from "../../src/features/investigation/api/workspaceAgent";
import type {
  WorkspaceConflictListV1,
  WorkspaceConflictResolutionV1,
  WorkspaceEnsureResultV1,
  WorkspaceFileListV1,
  WorkspaceFileViewV1,
  WorkspaceStatusV1,
} from "../../src/features/investigation/api/workspace";
import type {
  NoteBridgeReviewResponseV1,
  NoteViewResponseV1,
} from "../../src/features/investigation/api/workspaceNotes";
import type {
  AnalystNoteV1,
  OpinionReadPayloadV1,
  OpinionVersionV1,
} from "../../src/features/investigation/opinion/types";

const ids = {
  edition: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  subject: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  run: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
  synthesis: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  investigation: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
  finding: "11111111-1111-4111-8111-111111111111",
  observation: "22222222-2222-4222-8222-222222222222",
  taskReady: "33333333-3333-4333-8333-333333333333",
  taskProposed: "44444444-4444-4444-8444-444444444444",
  plan: "55555555-5555-4555-8555-555555555555",
  proposal: "66666666-6666-4666-8666-666666666666",
  opinion: "77777777-7777-4777-8777-777777777777",
  conflict: "88888888-8888-4888-8888-888888888888",
  decision: "99999999-9999-4999-8999-999999999999",
  planDecision: "a1a1a1a1-a1a1-41a1-81a1-a1a1a1a1a1a1",
  noteLead: "b1b1b1b1-b1b1-41b1-81b1-b1b1b1b1b1b1",
  noteSection: "b2b2b2b2-b2b2-42b2-82b2-b2b2b2b2b2b2",
  association: "c1c1c1c1-c1c1-41c1-81c1-c1c1c1c1c1c1",
  sample: "d1d1d1d1-d1d1-41d1-81d1-d1d1d1d1d1d1",
  session: "e2e2e2e2-e2e2-42e2-82e2-e2e2e2e2e2e2",
  bridgeInvocation: "e1e1e1e1-e1e1-41e1-81e1-e1e1e1e1e1e1",
  handoff: "e3e3e3e3-e3e3-43e3-83e3-e3e3e3e3e3e3",
  instruction: "e4e4e4e4-e4e4-44e4-84e4-e4e4e4e4e4e4",
  approvalDecision: "f1f1f1f1-f1f1-41f1-81f1-f1f1f1f1f1f1",
  snapshot: "f2f2f2f2-f2f2-42f2-82f2-f2f2f2f2f2f2",
  job: "f3f3f3f3-f3f3-43f3-83f3-f3f3f3f3f3f3",
} as const;

const SHA = {
  synthesis: "a".repeat(64),
  proposal: "b".repeat(64),
  content: "c".repeat(64),
  sample: "d".repeat(64),
  plan: "f".repeat(64),
  note: "1".repeat(64),
  proposed: "2".repeat(64),
  conflictCurrent: "3".repeat(64),
  conflictProposed: "4".repeat(64),
  opinionNext: "5".repeat(64),
} as const;

/** Smallest PDF the browser accepts for the read-only opinion preview frame. */
const MINIMAL_PDF = [
  "%PDF-1.4",
  "1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj",
  "2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj",
  "3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj",
  "trailer<</Root 1 0 R>>",
  "%%EOF",
].join("\n");

export interface RecordedRequest {
  method: string;
  path: string;
  body: unknown;
  idempotencyKey: string | null;
}

export interface WorkshopApiOptions {
  /** No host sidecar: workspace reads/writes fail closed with 503. */
  agentStatus?: "available" | "unavailable";
  /** `unbound` starts with no workspace so the ensure flow can run. */
  workspaceState?: "bound" | "unbound";
  /** The note proposal is already superseded by a newer opinion version. */
  staleProposal?: boolean;
  /** The exact synthesis already has an investigation before any POST. */
  existingInvestigation?: boolean;
  /** The investigation starts with an approved G2 envelope (cockpit lifecycle). */
  envelopeApproved?: boolean;
  /** A human transfer either hits a stale CAS or creates a handoff. */
  transferOutcome?: "stale" | "success";
  /**
   * Host capability probe outcome: a real attested confinement, an unattested
   * confinement (200 with a non-isolated host mode), or an absent host (503).
   */
  capabilitiesState?: "attested" | "unconfined" | "host_unavailable";
  /** The human open action is either accepted or refused by G2/TLP. */
  openSessionOutcome?: "success" | "external_llm_refused";
}

const FINDING: InvestigationFinding = {
  id: ids.finding,
  logical_id: "finding-logical-1",
  current_version_id: ids.finding,
  version: 1,
  revision_chain: [],
  kind: "network_behaviour",
  assessment: "supported",
  novelty: "new_to_article",
  statement: "Le service de commande contacte un domaine fixe.",
  scope: "échantillon",
  limitations: [],
  observation_refs: [ids.observation],
  claim_refs: ["claim-1"],
  created_at: "2026-11-22T08:00:00Z",
};

const OBSERVATION: InvestigationObservation = {
  id: ids.observation,
  kind: "binary_analysis",
  sha256: SHA.sample,
  source: "analysis-worker",
  source_version: "1",
  observed_at: "2026-11-22T08:00:00Z",
  object_type: "binary",
  object_id: "sample-1",
  limitations: [],
  invocation_ref: null,
};

const BUDGET = {
  bridge_turns: { limit: 8, consumed: 1, reserved: 0, remaining: 7 },
  vt_units: { limit: 20, consumed: 2, reserved: 0, remaining: 18 },
  shodan_credits: { limit: 5, consumed: 0, reserved: 0, remaining: 5 },
  downloads: { limit: 2, consumed: 0, reserved: 0, remaining: 2 },
  tool_seconds: { limit: 300, consumed: 4, reserved: 0, remaining: 296 },
  context_tokens: {
    limit: 20_000,
    consumed: 800,
    reserved: 0,
    remaining: 19_200,
  },
  context_bytes: {
    limit: 100_000,
    consumed: 4_000,
    reserved: 0,
    remaining: 96_000,
  },
};

function investigation(): InvestigationView {
  return {
    id: ids.investigation,
    subject_id: ids.subject,
    production_run_id: ids.run,
    synthesis_artifact_id: ids.synthesis,
    synthesis_sha256: SHA.synthesis,
    status: "running",
    prose_authority: "bridge_only",
    version: 1,
    stop_reason: null,
    agenda: [],
    hypotheses: [],
    findings: [FINDING],
    observations: [OBSERVATION],
    sample_associations: [
      {
        id: ids.association,
        sample_sha256: SHA.sample,
        origin: "vt_pivot",
        qualification: "candidate",
        related_sample_sha256: [SHA.synthesis],
        version: 1,
      },
    ],
    opinion_draft: null,
    budget: BUDGET,
    blocked_decisions: [],
    available_tools: ["article.get_claims"],
    unavailable_tools: ["shodan.host"],
    unavailable_tool_reasons: { "shodan.host": "g2_disclosure_denied" },
    envelope_approved: false,
    allowed_providers: [],
    acquire_hits_allowed: false,
  };
}

function activity(): InvestigationActivityPage {
  return {
    schema_version: "investigation-activity.v1",
    investigation: {
      id: ids.investigation,
      subject_id: ids.subject,
      production_run_id: ids.run,
      synthesis_artifact_id: ids.synthesis,
      synthesis_sha256: SHA.synthesis,
      status: "running",
      version: 1,
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
      hypothesis_count: 1,
      open_hypothesis_count: 0,
    },
    last_activity_at: "2026-11-22T10:00:00Z",
    last_activity_source: "investigation",
    active_backend_job_count: 0,
    budget: null,
    timeline: [],
    next_cursor: null,
    has_more: false,
    blockers: [],
    counts: { inconclusive_findings: 0 },
    generated_at: "2026-11-22T10:05:00Z",
  };
}

function workspaceStatus(
  bound: boolean,
  conflictResolved: boolean,
  tasks: number,
): WorkspaceStatusV1 {
  const binding = {
    workspace_id: "ws-m24-01",
    investigation_id: ids.investigation,
    subject_id: ids.subject,
    delivery_code: "IR2611",
    article_slug: "bluemoon-cn",
    synthesis_sha256: SHA.synthesis,
    created_at: "2026-11-22T09:00:00Z",
  };
  return {
    investigation_id: ids.investigation,
    workspace_state: bound ? "bound" : "unbound",
    workspace_available: bound,
    availability_code: bound ? "available" : "not_bound",
    availability_reason: null,
    binding: bound ? binding : null,
    manifest: null,
    generation: bound ? 3 : null,
    cursor: bound ? 42 : null,
    counts: {
      files: bound ? 2 : 0,
      human_notes: bound ? 1 : 0,
      unresolved_conflicts: bound && !conflictResolved ? 1 : 0,
      policy_violations: 0,
      plans: 1,
      tasks,
      findings: 1,
    },
    lastly_observed_at: bound ? "2026-11-22T10:00:00Z" : null,
  };
}

function ensureResult(): WorkspaceEnsureResultV1 {
  const status = workspaceStatus(true, false, 0);
  if (status.binding === null) throw new Error("bound workspace expected");
  return {
    binding: status.binding,
    binding_created: true,
    workspace_created: true,
  };
}

const NOTE_PATH = "notes/humaines/hypothese.md";
const PLAN_FILE_PATH = "plans/initial-v0001.md";

function noteFile(): WorkspaceFileViewV1 {
  return {
    path: NOTE_PATH,
    revision_id: "revision-2",
    sha256: SHA.note,
    size: 120,
    mode: "0640",
    policy_class: "human_editable",
    origin: "filesystem",
    import_status: "applied",
    task_id: null,
    observed_at: "2026-11-22T09:30:00Z",
    conflict: false,
    history: [
      {
        revision_id: "revision-2",
        sha256: SHA.note,
        size: 120,
        observed_at: "2026-11-22T09:30:00Z",
        origin: "filesystem",
        task_id: null,
        supersedes_revision_id: "revision-1",
      },
      {
        revision_id: "revision-1",
        sha256: SHA.content,
        size: 100,
        observed_at: "2026-11-22T09:10:00Z",
        origin: "ui",
        task_id: null,
        supersedes_revision_id: null,
      },
    ],
  };
}

function workspaceFiles(bound: boolean): WorkspaceFileListV1 {
  const planFile: WorkspaceFileViewV1 = {
    path: PLAN_FILE_PATH,
    revision_id: "revision-3",
    sha256: SHA.plan,
    size: 2048,
    mode: "0440",
    policy_class: "generated_immutable",
    origin: "system",
    import_status: "applied",
    task_id: null,
    observed_at: "2026-11-22T09:20:00Z",
    conflict: false,
    history: [],
  };
  return {
    investigation_id: ids.investigation,
    workspace_id: "ws-m24-01",
    workspace_available: bound,
    files: bound ? [noteFile(), planFile] : [],
    next_page_token: null,
  };
}

function noteView(): NoteViewResponseV1 {
  const latest = {
    revision_id: "revision-2",
    path: NOTE_PATH,
    sha256: SHA.note,
    size: 120,
    observed_at: "2026-11-22T09:30:00Z",
    origin: "filesystem" as const,
    supersedes_revision_id: "revision-1",
  };
  return {
    investigation_id: ids.investigation,
    workspace_id: "ws-m24-01",
    path: NOTE_PATH,
    latest,
    versions: [
      latest,
      {
        ...latest,
        revision_id: "revision-1",
        sha256: SHA.content,
        size: 100,
        observed_at: "2026-11-22T09:10:00Z",
        origin: "ui",
        supersedes_revision_id: null,
      },
    ],
    annotations: [],
    linked_task_ids: [ids.taskReady],
    linked_iocs: [{ ioc_type: "domain", value: "c2.example.invalid" }],
    linked_sample_ids: [],
    useful: false,
    dismissed: false,
    attributed_authors: [],
    observation_imported: false,
    is_evidence: false,
    unverified_hypothesis: true,
    contributions: [],
    instructions: [],
    handoffs: [],
    consultations: [],
  };
}

function workspaceConflicts(
  bound: boolean,
  resolved: boolean,
): WorkspaceConflictListV1 {
  return {
    investigation_id: ids.investigation,
    workspace_id: "ws-m24-01",
    workspace_available: bound,
    conflicts:
      bound && !resolved
        ? [
            {
              conflict_id: ids.conflict,
              path: NOTE_PATH,
              conflict_path: "notes/humaines/hypothese.conflict-abc12345.md",
              current_sha256: SHA.conflictCurrent,
              proposed_sha256: SHA.conflictProposed,
              origin: "ui",
              task_id: null,
              observed_at: "2026-11-22T10:00:00Z",
              current: {
                path: NOTE_PATH,
                sha256: SHA.conflictCurrent,
                size: 120,
                text: "Version locale conservée.",
                binary: false,
              },
              proposed: {
                path: "notes/humaines/hypothese.conflict-abc12345.md",
                sha256: SHA.conflictProposed,
                size: 130,
                text: "Version proposée conservée.",
                binary: false,
              },
              diff: "- Version locale conservée.\n+ Version proposée conservée.",
            },
          ]
        : [],
  };
}

function conflictResolution(
  choice: "keep_current" | "keep_proposed" | "merged_text",
): WorkspaceConflictResolutionV1 {
  const list = workspaceConflicts(true, false);
  const conflict = list.conflicts[0];
  if (conflict === undefined) throw new Error("fixture conflict expected");
  return {
    investigation_id: ids.investigation,
    workspace_id: "ws-m24-01",
    conflict_id: ids.conflict,
    path: NOTE_PATH,
    choice,
    decision_id: ids.decision,
    result_revision: {
      revision_id: "revision-4",
      sha256: SHA.conflictProposed,
      size: 130,
      observed_at: "2026-11-22T10:05:00Z",
      origin: "ui",
      task_id: null,
      supersedes_revision_id: "revision-2",
    },
    current: conflict.current,
    proposed: conflict.proposed,
    diff: conflict.diff,
    idempotent_replay: false,
  };
}

function inventory(
  qualification: SampleQualification = "candidate",
): IOCInventoryV1 {
  return {
    schema_version: "vt-bootstrap-inventory.v1",
    generated_at: "2026-11-22T09:00:00Z",
    investigation_id: ids.investigation,
    subject_id: ids.subject,
    production_run_id: ids.run,
    synthesis_artifact_id: ids.synthesis,
    pinned_synthesis_sha256: SHA.synthesis,
    workspace_binding_id: "ws-m24-01",
    workspace_synthesis_sha256: SHA.synthesis,
    seeds: [
      {
        ioc_id: "ioc-seed-1",
        kind: "sample",
        origin: "seed",
        value: SHA.sample,
        hash_type: "sha256",
        hashes: [{ hash_type: "sha256", value: SHA.sample }],
        sources: [
          {
            source_document_ids: ["doc-1"],
            source_refs: ["p. 1"],
            extraction_artifact_id: null,
            extraction_artifact_version: null,
            claim_ids: ["claim-1"],
          },
        ],
        claims: ["claim-1"],
        associations: [
          {
            association_id: ids.association,
            sample_sha256: SHA.sample,
            origin: "seed",
            qualification,
            version: qualification === "candidate" ? 1 : 2,
          },
        ],
        quarantined: [
          {
            sample_id: ids.sample,
            sha256: SHA.sample,
            state: "quarantined",
            association_id: ids.association,
            qualification,
          },
        ],
        limitations: ["Pas de rapport de comportement."],
      },
    ],
    discovered: [
      {
        ioc_id: "ioc-disc-1",
        kind: "domain",
        origin: "discovered",
        value: "c2.example.invalid",
        hash_type: null,
        hashes: [],
        sources: [],
        claims: [],
        associations: [],
        quarantined: [],
        limitations: [],
      },
    ],
    limits: {
      max_iocs: 50,
      max_sources_per_ioc: 10,
      max_claims_per_ioc: 10,
      iocs_considered: 2,
      truncated: false,
    },
    limitations: [],
  };
}

function sampleDetails(
  qualification: SampleQualification = "candidate",
): InvestigationSampleDetail[] {
  return [
    {
      id: ids.association,
      sample_sha256: SHA.sample,
      origin: "seed",
      qualification,
      related_sample_sha256: [SHA.synthesis],
      version: qualification === "candidate" ? 1 : 2,
      changes:
        qualification === "candidate"
          ? []
          : [
              {
                from_qualification: "candidate",
                to_qualification: qualification,
                actor_kind: "human",
                actor_id: "dev-analyst",
                reason: "Décision G3 de test.",
                changed_at: "2026-11-22T10:10:00Z",
              },
            ],
    },
  ];
}

function bootstrapPlan(): BootstrapPlanV1 {
  return {
    schema_version: "vt-bootstrap-plan.v1",
    generated_at: "2026-11-22T09:10:00Z",
    investigation_id: ids.investigation,
    state: "awaiting_g2",
    envelope_version: null,
    envelope_approval_decision_id: null,
    disclosure_tlp: "amber",
    budget: { vt_units_remaining: 18, downloads_remaining: 2 },
    operations: [
      {
        operation_key: "op-1",
        ioc_id: "ioc-seed-1",
        tool_name: "vt.file_report",
        schema_version: "1",
        arguments: {},
        status: "pending",
        reason: null,
        reserved_vt_units: 1,
        reserved_downloads: 0,
        requires_batch_evidence: false,
        route_status: "configured",
        limitations: [],
      },
    ],
    coverage: [],
    limits: {
      max_operations: 20,
      operations_planned: 1,
      operations_truncated: false,
      max_relationships_per_object: 10,
    },
    limitations: [],
  };
}

function snapshot(): BootstrapSnapshotV1 {
  return {
    schema_version: "vt-bootstrap-snapshot.v1",
    snapshot_id: ids.snapshot,
    investigation_id: ids.investigation,
    pinned_synthesis_sha256: SHA.synthesis,
    workspace_id: "ws-m24-01",
    generated_at: "2026-11-22T09:20:00Z",
    plan_state: "awaiting_g2",
    envelope_version: null,
    inventory_sha256: SHA.content,
    plan_sha256: SHA.plan,
    iocs: [
      {
        ioc_id: "ioc-seed-1",
        kind: "sample",
        value: SHA.sample,
        hash_type: "sha256",
        origin: "seed",
        status: "partial",
        reason: "partial",
        operations: [
          {
            operation_key: "op-1",
            ioc_id: "ioc-seed-1",
            tool_name: "vt.file_report",
            status: "partial",
            reason: "partial",
            invocation_id: ids.bridgeInvocation,
            idempotent_replay: false,
            observation_refs: [ids.observation],
            budget_consumed: { vt_units: 1 },
            pages: 2,
            observed_count: 14,
            pagination_complete: false,
            next_cursor: "cursor-2",
            provider_routes: ["vt.files"],
            detail_code: null,
            error_code: null,
            limitations: ["Champ « signature » absent du rapport."],
          },
        ],
        download: {
          status: "downloaded",
          association_id: ids.association,
          sample_id: ids.sample,
          sample_sha256: SHA.sample,
          qualification: "candidate",
          hash_verified_quarantine: true,
          reason: null,
        },
        association_ids: [ids.association],
        quarantined_sample_ids: [ids.sample],
      },
    ],
    coverage: [
      {
        kind: "route_unverified",
        target: "vt.behaviour",
        detail: "Route de comportement non attestée.",
        blocking: false,
      },
    ],
    limits: {
      max_iocs: 50,
      iocs_included: 1,
      inventory_truncated: false,
      max_operations: 20,
      operations_recorded: 1,
      operations_truncated: false,
      stopped_reason: null,
    },
    limitations: ["Couverture partielle des relations."],
  };
}

function envelopeProposal(approved: boolean): BootstrapEnvelopeProposalV1 {
  return {
    schema_version: "vt-bootstrap-envelope-proposal.v1",
    generated_at: "2026-11-22T09:15:00Z",
    investigation_id: ids.investigation,
    proposal_sha256: SHA.proposal,
    approved: false,
    already_approved: approved,
    envelope_version: approved ? 1 : null,
    plan_state: "awaiting_g2",
    allowed_providers: ["vt"],
    acquire_hits_allowed: true,
    inventory: {
      seeds: 1,
      discovered: 1,
      samples: 1,
      ipv4: 0,
      domains: 1,
      urls: 0,
      truncated: false,
    },
    planned_operations: 1,
    covered_operations: 1,
    covered_ioc_ids: ["ioc-seed-1"],
    vt_reads: {
      allowed: true,
      provider: "vt",
      operations_planned: 1,
      operations_covered: 1,
      units_planned: 1,
      units_proposed: 1,
      iocs_planned: 1,
      iocs_covered: 1,
      per_tool: [{ tool_name: "vt.file_report", planned: 1, covered: 1 }],
      summary: "Lecture VT bornée à l’IOC graine.",
      limitations: [],
    },
    sample_acquisition: {
      allowed: true,
      acquire_hits_allowed: true,
      sample_count_planned: 1,
      sample_count_covered: 1,
      downloads_limit: 1,
      per_sample_max_bytes: 1_048_576,
      total_max_bytes: 1_048_576,
      route_status: "configured",
      summary: "Acquisition bornée au volume proposé.",
      limitations: [],
    },
    costs: {
      limits: [{ dimension: "vt_units", limit: 1, source: "inventory" }],
      caps: {
        vt_units: 10,
        downloads: 2,
        per_sample_bytes: 1_048_576,
        tool_seconds: 120,
        context_bytes: 100_000,
        context_tokens: 20_000,
      },
      summary: "Plafonds dérivés des réglages.",
    },
    external_model: {
      allowed: false,
      providers: [],
      policy_source: "article_policy",
      summary: "Aucun modèle externe autorisé.",
      limitations: [],
    },
    disclosure: {
      tlp: "amber",
      do_not_submit: true,
      external_llm_allowed: false,
      scope: "article_cited_iocs",
      scope_statement: "IOC cités dans l’article uniquement.",
      summary: "Divulgation bornée aux IOC cités.",
      limitations: [],
    },
    summary: ["Enveloppe initiale bornée."],
    limitations: [],
  };
}

interface WorkshopEnvelopeView extends InvestigationEnvelopeView {
  policy_activated_by: string | null;
  authority_kind: string | null;
  inherited_policy_id: string | null;
  inherited_policy_version: number | null;
  inherited_policy_event_id: string | null;
  inherited_subject_id: string | null;
  policy_status: string | null;
  currently_authorized: boolean;
}

function lifecycleEnvelope(
  approved: boolean,
  version: number,
  revoked: boolean,
): WorkshopEnvelopeView {
  const active = approved && !revoked;
  return {
    investigation_id: ids.investigation,
    approved,
    version,
    allowed_providers: active ? ["vt"] : [],
    available_providers: ["vt", "shodan", "bridge"],
    acquire_hits_allowed: active,
    max_acquire_volume: active ? BUDGET.downloads.limit : 0,
    budget: revoked
      ? {
          ...BUDGET,
          downloads: { limit: 0, consumed: 0, reserved: 0, remaining: 0 },
        }
      : BUDGET,
    disclosure: {
      tlp: "amber",
      do_not_submit: true,
      external_llm_allowed: false,
    },
    approval_decision_id: approved ? ids.approvalDecision : null,
    approval_actor_id: approved ? "dev-analyst" : null,
    approved_at: approved ? "2026-11-22T09:30:00Z" : null,
    policy_activated_by: null,
    authority_kind: approved ? "human_g2" : null,
    inherited_policy_id: null,
    inherited_policy_version: null,
    inherited_policy_event_id: null,
    inherited_subject_id: null,
    policy_status: null,
    currently_authorized: active,
  };
}

function envelopeApproval(): BootstrapEnvelopeApprovalV1 {
  return {
    schema_version: "vt-bootstrap-envelope-approval.v1",
    approved: true,
    investigation_id: ids.investigation,
    proposal: envelopeProposal(true),
    envelope_version: 1,
    approval_decision_id: ids.approvalDecision,
    approval_actor_id: "dev-analyst",
    approved_at: "2026-11-22T09:30:00Z",
    allowed_providers: ["vt"],
    acquire_hits_allowed: true,
    budget_limits: { vt_units: 1, downloads: 1 },
    disclosure_tlp: "amber",
    disclosure_do_not_submit: true,
    disclosure_external_llm_allowed: false,
  };
}

const PLAN_LEADS = [
  {
    title: "Analyser le dropper",
    objective: "Identifier le packer du binaire.",
    cti_justification: "Le claim 1 décrit un chargeur non documenté.",
    claim_refs: ["claim-1"],
    start_evidence_refs: [ids.observation],
    sample_refs: [SHA.sample],
    tools: ["floss", "ghidra"],
    preconditions: ["Échantillon en quarantaine"],
    interest: "Découvrir le protocole C2.",
    conclusion_criteria: "Chaîne d’infection établie.",
    limits: "Pas d’exécution sur l’hôte.",
    depends_on: [],
    lead_id: "lead-a",
  },
  {
    title: "Étendre la collecte",
    objective: "",
    cti_justification: "",
    claim_refs: [],
    start_evidence_refs: [],
    sample_refs: [],
    tools: [],
    preconditions: [],
    interest: "",
    conclusion_criteria: "",
    limits: "",
    depends_on: ["lead-a"],
  },
];

function planDetail(): PlanVersionDetailV1 {
  return {
    plan_version_id: ids.plan,
    investigation_id: ids.investigation,
    version: 1,
    schema_version: "investigation-plan.v1",
    content_sha256: SHA.plan,
    parse_status: "parsed",
    source_invocation_id: ids.bridgeInvocation,
    source_observation_id: ids.observation,
    supersedes_id: null,
    created_at: "2026-11-22T09:40:00Z",
    lead_count: PLAN_LEADS.length,
    current: true,
    content: {
      schema_version: "investigation-plan.v1",
      investigation_id: ids.investigation,
      version: 1,
      leads: PLAN_LEADS,
    },
  };
}

function planList(): PlanListV1 {
  const detail = planDetail();
  return { current_version: 1, plans: [detail] };
}

function planAccept(tasks: TaskView[]): PlanAcceptResponseV1 {
  return {
    investigation_id: ids.investigation,
    plan_version_id: ids.plan,
    plan_version: 1,
    decision_id: ids.planDecision,
    tasks: tasks.map((task) => ({
      task_id: task.id,
      task_key: task.task_key,
      title: task.title,
      status: task.status,
      created: true,
    })),
    idempotent_replay: false,
  };
}

function makeTask(params: {
  id: string;
  key: string;
  title: string;
  question: string;
  objective: string;
  status: "ready" | "proposed";
}): TaskView {
  return {
    id: params.id,
    investigation_id: ids.investigation,
    task_key: params.key,
    plan_version_id: ids.plan,
    parent_task_id: null,
    depth: 0,
    title: params.title,
    question: params.question,
    objective: params.objective,
    priority: "high",
    claim_refs: ["claim-1"],
    evidence_refs: [ids.observation],
    sample_refs: [SHA.sample],
    required_capabilities: ["local_analysis"],
    allowed_workspace_paths: ["results/"],
    execution_policy: {},
    expected_outputs: ["note"],
    status: params.status,
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
    event_sequence: 1,
    created_by: { actor_kind: "system", actor_id: "plan_accept:decision" },
    created_at: "2026-11-22T09:50:00Z",
    updated_at: "2026-11-22T09:50:00Z",
  };
}

function acceptedTasks(): TaskView[] {
  return [
    makeTask({
      id: ids.taskReady,
      key: "lead-a",
      title: "Vérifier le domaine C2",
      question: "Le domaine est-il un C2 ?",
      objective: "Confirmer le rôle du domaine dans la chaîne.",
      status: "ready",
    }),
    makeTask({
      id: ids.taskProposed,
      key: "lead-b",
      title: "Pivoter sur l’infrastructure",
      question: "L’infrastructure est-elle partagée ?",
      objective: "Relier le domaine aux autres échantillons.",
      status: "proposed",
    }),
  ];
}

function taskPage(tasks: TaskView[]): TaskPageView {
  return { items: tasks, total: tasks.length };
}

/** One page covering the canonical evidence and the four B7 working objects. */
function timelinePage(): TimelinePageV1 {
  return {
    items: [
      {
        item_type: "observation",
        object_id: ids.observation,
        occurred_at: "2026-11-22T10:00:00Z",
        task_id: null,
        actor_kind: "source",
        actor_id: "virustotal",
        summary: "vt_file_report:sample",
        evidence: true,
        refs: { observation_id: ids.observation, sha256: SHA.sample },
        linked_event_ids: [],
        linked_observation_ids: [],
      },
      {
        item_type: "handoff",
        object_id: ids.handoff,
        occurred_at: "2026-11-22T10:02:00Z",
        task_id: ids.taskReady,
        actor_kind: "human",
        actor_id: "analyst:e2e",
        summary: "handoff:v1:human->operator",
        evidence: false,
        refs: {
          handoff_id: ids.handoff,
          task_id: ids.taskReady,
          version: "1",
          sha256: SHA.note,
        },
        linked_event_ids: [],
        linked_observation_ids: [],
      },
      {
        item_type: "contribution",
        object_id: "contribution-1",
        occurred_at: "2026-11-22T10:03:00Z",
        task_id: ids.taskReady,
        actor_kind: "operator",
        actor_id: "codex",
        summary: "contribution:finding:imported",
        evidence: false,
        refs: {
          contribution_id: "contribution-1",
          workspace_id: "ws-m24-01",
          workspace_revision_id: "revision-2",
          file_relpath: "notes/agents/task-1/finding.md",
          file_sha256: SHA.note,
          finding_id: ids.finding,
        },
        linked_event_ids: [],
        linked_observation_ids: [],
      },
      {
        item_type: "agent_session",
        object_id: ids.session,
        occurred_at: "2026-11-22T10:04:00Z",
        task_id: ids.taskReady,
        actor_kind: "system",
        actor_id: "codex_cli",
        summary: "agent_session:codex_cli:running",
        evidence: false,
        refs: {
          session_id: ids.session,
          task_id: ids.taskReady,
          adapter_kind: "codex_cli",
          state: "running",
          state_version: "2",
          handoff_id: ids.handoff,
        },
        linked_event_ids: [],
        linked_observation_ids: [],
      },
      {
        item_type: "instruction",
        object_id: ids.instruction,
        occurred_at: "2026-11-22T10:05:00Z",
        task_id: ids.taskReady,
        actor_kind: "human",
        actor_id: "analyst:e2e",
        summary: "instruction:queued",
        evidence: false,
        refs: {
          instruction_id: ids.instruction,
          session_id: ids.session,
          text_sha256: SHA.proposed,
          delivery_attempts: "0",
        },
        linked_event_ids: [],
        linked_observation_ids: [],
      },
    ],
    next_cursor: null,
    has_more: false,
  };
}

function capabilitiesPayload(
  adapterKind: OperatorAdapterKind,
  executable: string,
  unconfined: boolean,
) {
  return {
    adapter_kind: adapterKind,
    executable,
    available: true,
    version: adapterKind === "codex_cli" ? "codex-cli 0.163.0" : "2.1.284",
    capability_sha256: "a".repeat(64),
    probed_at: "2026-11-22T10:05:00Z",
    supports_resume: true,
    supports_structured_output: true,
    supports_non_interactive: true,
    modes: ["non_interactive"],
    permission_modes: [],
    confinement: unconfined
      ? {
          profile: "unattested",
          bwrap_ok: true,
          netns: false,
          network_mode: null,
          loopback_isolated: false,
        }
      : {
          profile: "bwrap-loopback",
          bwrap_ok: true,
          netns: true,
          network_mode: "isolated_egress",
          loopback_isolated: true,
        },
  };
}

function openedSession(
  task: TaskView,
  adapterKind: OperatorAdapterKind,
  handoffId: string,
): AgentSessionView {
  return {
    id: ids.session,
    investigation_id: ids.investigation,
    task_id: task.id,
    adapter_kind: adapterKind,
    state: "running",
    state_version: 2,
    host_session_ref: "host-session-e2e",
    handoff_id: handoffId,
    capability_sha256: "a".repeat(64),
    probed_at: "2026-11-22T10:05:00Z",
    task_lease_id: "cdcdcdcd-cdcd-4cdc-8cdc-cdcdcdcdcdcd",
    task_lease_fence: 1,
    checkpoint_ref: null,
    checkpoint_sha256: null,
    reconciliation: null,
    reconciliation_sha256: null,
    last_activity_at: "2026-11-22T10:06:00Z",
    opened_at: "2026-11-22T10:05:00Z",
    closed_at: null,
    close_reason: null,
    created_at: "2026-11-22T10:05:00Z",
    updated_at: "2026-11-22T10:06:00Z",
  };
}

function zeroBudget(): Record<BudgetDimension, number> {
  return {
    bridge_turns: 0,
    vt_units: 0,
    shodan_credits: 0,
    downloads: 0,
    tool_seconds: 0,
    context_tokens: 0,
    context_bytes: 0,
  };
}

/**
 * B6 projection for the operator panel. The mock journal holds no invocation
 * rows, so operations/instructions are legitimately empty; the envelope,
 * handoff and derived broker block mirror the surrounding fixtures.
 */
function sessionSummary(
  session: AgentSessionView,
  envelopeApproved: boolean,
): AgentSessionSummaryView {
  const envelope = envelopeApproved
    ? {
        present: true,
        version: 1,
        approved_at: "2026-11-22T09:30:00Z",
        limits: {
          ...zeroBudget(),
          bridge_turns: null,
          vt_units: 1,
          downloads: 1,
        },
        consumed: zeroBudget(),
        reserved: zeroBudget(),
        remaining: {
          ...zeroBudget(),
          bridge_turns: null,
          vt_units: 1,
          shodan_credits: null,
          downloads: 1,
          tool_seconds: null,
          context_tokens: null,
          context_bytes: null,
        },
      }
    : {
        present: false,
        version: null,
        approved_at: null,
        limits: null,
        consumed: null,
        reserved: null,
        remaining: null,
      };
  return {
    session_id: session.id,
    operations: { items: [], total: 0, limit: 100, truncated: false },
    budget: {
      session: {
        invocation_count: 0,
        consumed: zeroBudget(),
        reserved: zeroBudget(),
      },
      envelope,
    },
    broker: {
      derived_from: "tool_invocations_journal",
      last_signed_call: null,
      signed_call_count: 0,
      needs_review_count: 0,
      reconciliation_required: false,
    },
    handoff:
      session.handoff_id === null
        ? null
        : {
            handoff_id: session.handoff_id,
            version: 1,
            sha256: SHA.content,
            created_at: "2026-11-22T09:45:00Z",
            from_actor: { kind: "human", actor_id: "dev-analyst" },
            to_actor: {
              kind: "operator",
              actor_id: `operator:${session.adapter_kind}`,
            },
            freshness: { checked: true, state: "fresh", diffs: [] },
          },
    instructions: { items: [], total: 0, limit: 20, truncated: false },
  };
}

function consultations(): ConsultationLedgerV1 {
  return { investigation_id: ids.investigation, consultations: [] };
}

function contributions(): WorkContributionView[] {
  return [
    {
      id: "contribution-1",
      investigation_id: ids.investigation,
      workspace_id: "ws-m24-01",
      workspace_revision_id: "revision-2",
      file_relpath: "notes/agents/task-1/finding.md",
      file_sha256: SHA.note,
      category: "finding",
      schema_version: "work-contribution.v1",
      import_status: "imported",
      origin: "host",
      origin_actor_id: null,
      task_id: ids.taskReady,
      lease_fence: 1,
      content_blob_id: "blob-1",
      content_sha256: SHA.note,
      observation_id: null,
      finding_id: ids.finding,
      sample_association_id: ids.association,
      validation_error_code: null,
      citable_as_evidence: true,
      idempotency_key: "idem-contribution-1",
      observed_at: "2026-11-22T10:00:00Z",
      created_at: "2026-11-22T10:01:00Z",
    },
  ];
}

function analystNote(params: {
  id: string;
  anchor: string;
  text: string;
  bridgeBlockType: AnalystNoteV1["bridge_block_type"];
}): AnalystNoteV1 {
  return {
    id: params.id,
    anchor: params.anchor,
    text: params.text,
    state: "observed",
    relation: "confirms",
    finding_refs: [ids.finding],
    evidence_refs: [ids.observation],
    bridge_authored: true,
    bridge_block_type: params.bridgeBlockType,
    bridge_draft_ref: {
      invocation_id: ids.bridgeInvocation,
      text_sha256: SHA.content,
    },
    bridge_modified_by_operator: false,
  };
}

function opinionVersion(): OpinionVersionV1 {
  const blocks = [
    {
      type: "key_findings" as const,
      text: "Une variante distincte est étayée.",
      finding_refs: [ids.finding],
      evidence_refs: [ids.observation],
      qualification: "fact" as const,
      certainty: 0.9,
    },
  ];
  const provenance = {
    investigation_id: ids.investigation,
    synthesis_artifact_id: ids.synthesis,
    synthesis_sha256: SHA.synthesis,
    base_publication_artifact_id: ids.synthesis,
    base_publication_sha256: SHA.proposal,
  };
  return {
    schema: "analyst-opinion.v1",
    id: ids.opinion,
    version: 3,
    investigation_id: ids.investigation,
    synthesis_artifact_id: ids.synthesis,
    synthesis_sha256: SHA.synthesis,
    base_publication_artifact_id: ids.synthesis,
    base_publication_sha256: SHA.proposal,
    blocks,
    unresolved_points: [],
    author_id: "system:opinion-writer",
    reviewer_id: null,
    status: "draft",
    created_at: "2026-11-22T09:00:00Z",
    content_sha256: SHA.synthesis,
    renderable_blocks: blocks,
    provenance,
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
    observations: [
      analystNote({
        id: ids.noteLead,
        anchor: "lead:0001",
        text: "Le chapô observe un service de commande.",
        bridgeBlockType: "key_findings",
      }),
      analystNote({
        id: ids.noteSection,
        anchor: "section:2:paragraph:0007",
        text: "La section documente une persistance.",
        bridgeBlockType: "network_and_infrastructure",
      }),
    ],
  };
}

function opinionPayload(): OpinionReadPayloadV1 {
  const draft = opinionVersion();
  return {
    draft,
    history: [draft],
    citation_violations: [],
    renderable_blocks: draft.renderable_blocks,
    provenance: draft.provenance,
  };
}

/** Canonical anchor space the note picker reads; base matches the opinion pin. */
function articleAnchorCatalog(): ArticleAnchorCatalogViewV1 {
  return {
    base_publication_artifact_id: ids.synthesis,
    base_publication_version: 3,
    base_publication_sha256: SHA.proposal,
    source: "pinned_opinion_base",
    anchors: [
      {
        anchor: "lead:0001",
        text: "Le chapô observe un service de commande.",
        text_sha256: SHA.content,
        section_index: null,
        paragraph_index: 0,
      },
      {
        anchor: "section:2:paragraph:0007",
        text: "La section documente une persistance.",
        text_sha256: SHA.note,
        section_index: 2,
        paragraph_index: 6,
      },
      {
        anchor: "lead:0002",
        text: "Un paragraphe dupliqué pour prouver l’occurrence.",
        text_sha256: SHA.proposed,
        section_index: null,
        paragraph_index: 1,
      },
      {
        anchor: "lead:0003",
        text: "Un paragraphe dupliqué pour prouver l’occurrence.",
        text_sha256: SHA.proposed,
        section_index: null,
        paragraph_index: 2,
      },
    ],
    elements: [],
    truncated: false,
  };
}

function noteProposal(stale: boolean): NoteProposalViewV1 {
  return {
    id: ids.proposal,
    investigation_id: ids.investigation,
    opinion_id: ids.opinion,
    task_id: null,
    anchor: "lead:0001",
    target_block_type: "lead",
    bridge_block_type: "key_findings",
    consultation_type: "draft_text",
    base_note_id: ids.noteLead,
    note_state: "observed",
    note_relation: "confirms",
    cited_finding_refs: [ids.finding],
    cited_observation_refs: [ids.observation],
    proposed_text: "Texte Bridge proposé pour le chapô.",
    proposed_text_sha256: SHA.proposed,
    bridge_invocation_id: ids.bridgeInvocation,
    bridge_output_sha256: SHA.content,
    bridge_response_observation_id: ids.observation,
    tool_invocation_id: ids.bridgeInvocation,
    base_opinion_version: 3,
    base_opinion_sha256: SHA.synthesis,
    base_publication_artifact_id: ids.synthesis,
    base_publication_version: 3,
    base_publication_sha256: SHA.proposal,
    anchor_before_sha256: SHA.content,
    status: stale ? "stale" : "proposed",
    status_version: stale ? 5 : 4,
    accepted_opinion_version: null,
    analyst_decision_id: null,
    idempotency_key: "idem-proposal-1",
    materialised: true,
    created_at: "2026-11-22T09:55:00Z",
    updated_at: "2026-11-22T09:55:00Z",
  };
}

function proposalDiff(stale: boolean): NoteProposalDiffViewV1 {
  const proposal = noteProposal(stale);
  return {
    proposal,
    current_opinion_version: 3,
    current_opinion_sha256: SHA.synthesis,
    current_note_id: ids.noteLead,
    current_note_text: "Note actuelle du chapô.",
    proposed_text: proposal.proposed_text,
    text_changed: true,
    anchor: proposal.anchor,
    target_block_type: proposal.target_block_type,
    anchor_excerpt: "Extrait du chapô.",
    cited_finding_refs: proposal.cited_finding_refs,
    cited_observation_refs: proposal.cited_observation_refs,
    limits: ["Limite signalée par Bridge."],
    stale,
    accept_ready: !stale,
  };
}

function proposalRequestResponse(
  stale: boolean,
): NoteProposalRequestResponseV1 {
  return {
    proposal: noteProposal(stale),
    invocation_id: ids.bridgeInvocation,
    invocation_status: "succeeded",
    idempotent_replay: false,
    materialised: true,
  };
}

function proposalAcceptance(stale: boolean): NoteAcceptanceViewV1 {
  const proposal = noteProposal(stale);
  return {
    proposal: {
      ...proposal,
      status: "accepted",
      status_version: proposal.status_version + 1,
      accepted_opinion_version: 4,
      analyst_decision_id: ids.decision,
    },
    opinion_id: ids.opinion,
    opinion_version: 4,
    opinion_sha256: SHA.opinionNext,
    decision_id: ids.decision,
    note_id: ids.noteLead,
    idempotent_replay: false,
  };
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {};
}

function conflictChoice(
  value: unknown,
): "keep_current" | "keep_proposed" | "merged_text" {
  return value === "keep_proposed" || value === "merged_text"
    ? value
    : "keep_current";
}

export function createWorkshopApi(options: WorkshopApiOptions = {}) {
  const agentStatus = options.agentStatus ?? "available";
  const staleProposal = options.staleProposal === true;
  const existingInvestigation = options.existingInvestigation === true;
  const transferOutcome = options.transferOutcome ?? "stale";
  const capabilitiesState = options.capabilitiesState ?? "attested";
  const openSessionOutcome = options.openSessionOutcome ?? "success";
  const requests: RecordedRequest[] = [];
  const unhandled: string[] = [];
  let started = existingInvestigation;
  let workspaceBound = (options.workspaceState ?? "bound") === "bound";
  let envelopeApproved = options.envelopeApproved === true;
  let envelopeVersion = envelopeApproved ? 1 : 0;
  let envelopeRevoked = false;
  let sampleQualification: SampleQualification = "candidate";
  let tasks: TaskView[] = [];
  let sessions: AgentSessionView[] = [];
  let conflictResolved = false;

  const inv = (suffix: string) =>
    `/api/investigations/${ids.investigation}${suffix}`;

  async function handle(route: Route) {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    let body: unknown = null;
    if (request.postData()) {
      try {
        body = request.postDataJSON() as unknown;
      } catch {
        body = request.postData();
      }
    }
    requests.push({
      method,
      path,
      body,
      idempotencyKey: request.headers()["idempotency-key"] ?? null,
    });

    if (path === "/api/workspace-agent/status") {
      const payload: WorkspaceAgentStatusResponse = {
        status: agentStatus,
        instance_id: agentStatus === "available" ? "host-agent-m24" : null,
      };
      return fulfill(route, payload);
    }
    if (path === `/api/subjects/${ids.subject}`) {
      return fulfill(route, {
        id: ids.subject,
        edition_id: ids.edition,
        title: "Sujet de test CTF",
        slug: "sujet-test-ctf",
        tlp: "AMBER",
        version: 1,
        created_at: "2026-11-22T08:00:00Z",
        updated_at: "2026-11-22T08:00:00Z",
      });
    }
    if (path === `/api/subjects/${ids.subject}/content`) {
      return fail(route, 404, "subject_content_not_found");
    }
    if (
      path === `/api/subjects/${ids.subject}/production/artifacts/synthesis`
    ) {
      return fulfill(route, {
        artifact_id: ids.synthesis,
        production_run_id: ids.run,
        stage: "synthesis",
        version: 4,
        input_hash: SHA.content,
        canonical_sha256: SHA.synthesis,
        status: "verified",
        metadata: {},
        canonical_content: { schema_version: 2 },
        caption: "Synthèse vérifiée",
        provenance: "production",
      });
    }
    if (path === `/api/subjects/${ids.subject}/investigations`) {
      if (method === "POST") {
        started = true;
        return fulfill(route, {
          investigation: investigation(),
          job_id: ids.job,
          idempotent_replay: false,
        });
      }
      return fulfill(
        route,
        started
          ? [
              {
                id: ids.investigation,
                subject_id: ids.subject,
                production_run_id: ids.run,
                synthesis_artifact_id: ids.synthesis,
                synthesis_sha256: SHA.synthesis,
                status: "running",
                version: 1,
                stop_reason: null,
                updated_at: "2026-11-22T10:00:00Z",
              },
            ]
          : [],
      );
    }
    if (path === inv("") && method === "GET") {
      return fulfill(route, investigation());
    }
    if (path === inv("/activity") && method === "GET") {
      return fulfill(route, activity());
    }
    if (path === inv("/samples") && method === "GET") {
      return fulfill(route, sampleDetails(sampleQualification));
    }
    if (
      path.startsWith(inv("/samples/")) &&
      path.endsWith("/qualify") &&
      method === "POST"
    ) {
      const payload = asRecord(body);
      const qualification = payload.qualification;
      if (typeof qualification === "string") {
        sampleQualification = qualification as SampleQualification;
      }
      return fulfill(route, {
        association: sampleDetails(sampleQualification)[0],
        idempotent_replay: false,
      });
    }

    if (path === inv("/workspace") && method === "GET") {
      if (agentStatus !== "available") {
        return fail(
          route,
          503,
          "workspace_local_unavailable",
          "L’agent hôte ne répond pas : aucun workspace local n’est monté.",
        );
      }
      return fulfill(
        route,
        workspaceStatus(workspaceBound, conflictResolved, tasks.length),
      );
    }
    if (path === inv("/workspace/ensure") && method === "POST") {
      if (agentStatus !== "available") {
        return fail(
          route,
          503,
          "workspace_local_unavailable",
          "L’agent hôte ne répond pas : création impossible.",
        );
      }
      workspaceBound = true;
      return fulfill(route, ensureResult());
    }
    if (path === inv("/workspace/files") && method === "GET") {
      return fulfill(route, workspaceFiles(workspaceBound));
    }
    if (path === inv("/workspace/files/content") && method === "GET") {
      if (!workspaceBound) {
        return fail(route, 503, "workspace_local_unavailable");
      }
      return fulfill(route, {
        investigation_id: ids.investigation,
        workspace_id: "ws-m24-01",
        path: NOTE_PATH,
        revision_id: "revision-2",
        sha256: SHA.note,
        size: 120,
        metadata_only: false,
        text: "# Hypothèse\n\nLe domaine pourrait être un leurre.",
        content_base64: null,
      });
    }
    if (path === inv("/workspace/conflicts") && method === "GET") {
      return fulfill(
        route,
        workspaceConflicts(workspaceBound, conflictResolved),
      );
    }
    if (
      path.startsWith(inv("/workspace/conflicts/")) &&
      path.endsWith("/resolve") &&
      method === "POST"
    ) {
      conflictResolved = true;
      return fulfill(
        route,
        conflictResolution(conflictChoice(asRecord(body).choice)),
      );
    }
    if (
      path.startsWith(inv("/workspace/notes/")) &&
      path.endsWith("/view") &&
      method === "GET"
    ) {
      if (!workspaceBound) {
        return fail(route, 409, "workspace_not_bound");
      }
      return fulfill(route, noteView());
    }
    if (
      path.startsWith(inv("/workspace/notes/")) &&
      path.endsWith("/ask-bridge") &&
      method === "POST"
    ) {
      const review: NoteBridgeReviewResponseV1 = {
        investigation_id: ids.investigation,
        note_revision_id: "revision-2",
        note_sha256: SHA.note,
        consultation_type: "interpretation",
        invocation_id: ids.bridgeInvocation,
        invocation_status: "pending",
        pending: true,
        idempotent_replay: false,
        advisory_only: true,
        is_evidence: false,
      };
      return fulfill(route, review);
    }

    if (path === inv("/envelope") && method === "GET") {
      return fulfill(
        route,
        lifecycleEnvelope(envelopeApproved, envelopeVersion, envelopeRevoked),
      );
    }
    if (path === inv("/envelope/extend") && method === "POST") {
      envelopeApproved = true;
      envelopeVersion += 1;
      return fulfill(
        route,
        lifecycleEnvelope(envelopeApproved, envelopeVersion, envelopeRevoked),
      );
    }
    if (path === inv("/envelope/revoke") && method === "POST") {
      envelopeVersion += 1;
      envelopeRevoked = true;
      return fulfill(
        route,
        lifecycleEnvelope(envelopeApproved, envelopeVersion, envelopeRevoked),
      );
    }

    if (path === inv("/bootstrap/inventory") && method === "GET") {
      return fulfill(route, inventory(sampleQualification));
    }
    if (path === inv("/bootstrap/plan") && method === "GET") {
      return fulfill(route, bootstrapPlan());
    }
    if (path === inv("/bootstrap/snapshot") && method === "GET") {
      return fulfill(route, snapshot());
    }
    if (path === inv("/bootstrap/run") && method === "POST") {
      return fulfill(route, snapshot());
    }
    if (path === inv("/bootstrap/envelope-proposal") && method === "GET") {
      return fulfill(route, envelopeProposal(envelopeApproved));
    }
    if (path === inv("/bootstrap/envelope/approve") && method === "POST") {
      envelopeApproved = true;
      envelopeVersion = Math.max(envelopeVersion, 1);
      return fulfill(route, envelopeApproval());
    }

    if (path === inv("/plans") && method === "GET") {
      return fulfill(route, planList());
    }
    if (path === inv(`/plans/${ids.plan}`) && method === "GET") {
      return fulfill(route, planDetail());
    }
    if (path === inv(`/plans/${ids.plan}/accept`) && method === "POST") {
      tasks = acceptedTasks();
      return fulfill(route, planAccept(tasks));
    }
    if (path === inv("/consultations") && method === "GET") {
      return fulfill(route, consultations());
    }

    if (path === inv("/tasks") && method === "GET") {
      return fulfill(route, taskPage(tasks));
    }
    if (path === inv("/timeline") && method === "GET") {
      return fulfill(route, timelinePage());
    }
    if (
      path === "/api/investigations/agent-sessions/capabilities" &&
      method === "GET"
    ) {
      if (capabilitiesState === "host_unavailable") {
        return fail(
          route,
          503,
          "workspace_local_unavailable",
          "L’agent hôte ne répond pas : le sondage des capacités est impossible.",
        );
      }
      const adapterKind = (url.searchParams.get("adapter_kind") ??
        "codex_cli") as OperatorAdapterKind;
      const executable = url.searchParams.get("executable") ?? "codex";
      return fulfill(
        route,
        capabilitiesPayload(
          adapterKind,
          executable,
          capabilitiesState === "unconfined",
        ),
      );
    }
    if (path === inv("/agent-sessions") && method === "GET") {
      return fulfill(route, sessions);
    }
    if (path === inv("/agent-sessions") && method === "POST") {
      if (openSessionOutcome === "external_llm_refused") {
        return fail(
          route,
          403,
          "agent_session_external_llm_not_allowed",
          "La politique G2/TLP de cette enquête n’autorise pas une session externe.",
        );
      }
      const post = asRecord(body);
      const taskId = typeof post.task_id === "string" ? post.task_id : "";
      const adapterKind = (
        typeof post.adapter_kind === "string" ? post.adapter_kind : "codex_cli"
      ) as OperatorAdapterKind;
      const handoffId =
        typeof post.handoff_id === "string" ? post.handoff_id : ids.decision;
      const task = tasks.find((item) => item.id === taskId);
      if (task === undefined) {
        return fail(route, 404, "task_not_found");
      }
      const session = openedSession(task, adapterKind, handoffId);
      sessions = [...sessions, session];
      tasks = tasks.map((item) =>
        item.id === task.id
          ? {
              ...item,
              status: "running",
              status_version: item.status_version + 1,
              handoff_ref: handoffId,
              updated_at: "2026-11-22T10:06:00Z",
            }
          : item,
      );
      return fulfill(route, { session });
    }
    if (
      path.startsWith(inv("/agent-sessions/")) &&
      path.endsWith("/summary") &&
      method === "GET"
    ) {
      const sessionId = path.slice(
        inv("/agent-sessions/").length,
        -"/summary".length,
      );
      const session = sessions.find((item) => item.id === sessionId);
      if (session === undefined) {
        return fail(route, 404, "agent_session_not_found");
      }
      return fulfill(route, sessionSummary(session, envelopeApproved));
    }
    if (
      path.startsWith(inv("/tasks/")) &&
      path.endsWith("/events") &&
      method === "GET"
    ) {
      return fulfill(route, { items: [], next_cursor: null, has_more: false });
    }
    if (
      path.startsWith(inv("/tasks/")) &&
      path.endsWith("/transfer") &&
      method === "POST"
    ) {
      if (transferOutcome === "stale") {
        // The browser sends the exact task status_version; this scenario
        // exercises the typed stale-CAS refusal.
        return fail(
          route,
          409,
          "stale_task_version",
          "La tâche a changé depuis son chargement.",
        );
      }
      const transferTaskId = path.slice(
        inv("/tasks/").length,
        -"/transfer".length,
      );
      const task = tasks.find((item) => item.id === transferTaskId);
      const post = asRecord(body);
      const adapterKind =
        typeof post.to_adapter_kind === "string"
          ? post.to_adapter_kind
          : "codex_cli";
      const humanDestination = post.to_actor_kind === "human";
      const destinationKind = humanDestination ? "human" : "operator";
      const destinationActorId = humanDestination
        ? "human:dev-analyst"
        : `operator:${adapterKind}`;
      tasks = tasks.map((item) =>
        item.id === transferTaskId
          ? {
              ...item,
              status: "assigned",
              status_version: item.status_version + 1,
              assigned_actor_kind: destinationKind,
              assigned_actor_id: destinationActorId,
              handoff_ref: ids.decision,
              updated_at: "2026-11-22T10:10:00Z",
            }
          : item,
      );
      return fulfill(route, {
        task: {
          id: transferTaskId,
          investigation_id: ids.investigation,
          task_key: task?.task_key ?? "lead-a",
          status: "assigned",
          status_version: 2,
          lease_fence: 0,
          assigned_actor_kind: destinationKind,
          assigned_actor_id: destinationActorId,
          result_verification_status: "unverified",
          updated_at: "2026-11-22T10:10:00Z",
        },
        destination: {
          actor_kind: destinationKind,
          actor_id: destinationActorId,
        },
        sync: null,
        handoff: {
          schema_version: "investigation-handoff.v1",
          handoff_id: ids.decision,
          investigation_id: ids.investigation,
          task_id: transferTaskId,
          version: 1,
          created_at: "2026-11-22T10:10:00Z",
          from_actor: { kind: "human", actor_id: "dev-analyst" },
          to_actor: {
            kind: destinationKind,
            actor_id: destinationActorId,
          },
          task: {
            status: "assigned",
            status_version: 1,
            question: task?.question ?? "",
            objective: task?.objective ?? "",
            expected_outputs: task?.expected_outputs ?? [],
            assigned_actor_kind: null,
            assigned_actor_id: null,
          },
          plan: null,
          work: {
            hypotheses: [],
            attempts: [],
            established_results: [],
            partial_results: [],
            negative_results: [],
            limits: [],
          },
          refs: {
            observations: [],
            findings: [],
            decisions: [],
            consultations: [],
          },
          files: [],
          next_action: "Poursuivre la tâche transférée.",
          freshness_refs: [],
        },
        previous_handoff_id: null,
        previous_handoff_version: null,
        previous_freshness: null,
        session: null,
        replayed: false,
      });
    }
    if (
      path.startsWith(inv("/tasks/")) &&
      path.endsWith("/commands") &&
      method === "POST"
    ) {
      const task = tasks[0];
      if (task === undefined) return fail(route, 404, "task_not_found");
      return fulfill(route, { task, event: null, replayed: false });
    }
    if (path.startsWith(inv("/tasks/")) && method === "GET") {
      const taskId = path.slice(inv("/tasks/").length);
      const task = tasks.find((item) => item.id === taskId);
      return task === undefined
        ? fail(route, 404, "task_not_found")
        : fulfill(route, task);
    }

    if (path === inv("/contributions") && method === "GET") {
      return fulfill(route, contributions());
    }

    if (path === inv("/opinion") && method === "GET") {
      return fulfill(route, opinionPayload());
    }
    if (path === inv("/article-anchors") && method === "GET") {
      return fulfill(route, articleAnchorCatalog());
    }
    if (
      path === `/api/opinions/${ids.opinion}/preview.pdf` &&
      method === "GET"
    ) {
      return route.fulfill({
        status: 200,
        contentType: "application/pdf",
        body: MINIMAL_PDF,
      });
    }
    if (path === inv("/note-proposals")) {
      if (method === "POST") {
        return fulfill(route, proposalRequestResponse(staleProposal));
      }
      return fulfill(route, [noteProposal(staleProposal)]);
    }
    if (path === inv(`/note-proposals/${ids.proposal}`) && method === "GET") {
      return fulfill(route, proposalDiff(staleProposal));
    }
    if (
      path === inv(`/note-proposals/${ids.proposal}/accept`) &&
      method === "POST"
    ) {
      return fulfill(route, proposalAcceptance(staleProposal));
    }
    if (
      path === inv(`/note-proposals/${ids.proposal}/reject`) &&
      method === "POST"
    ) {
      return fulfill(route, {
        proposal: { ...noteProposal(staleProposal), status: "rejected" },
      });
    }

    unhandled.push(`${method} ${path}`);
    return fail(route, 404, "unmocked_route");
  }

  return {
    ids,
    requests,
    unhandled,
    install: (page: Page) =>
      page.route((url) => url.pathname.startsWith("/api/"), handle),
  };
}

export type WorkshopApi = ReturnType<typeof createWorkshopApi>;

function fulfill(route: Route, payload: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(payload),
  });
}

function fail(route: Route, status: number, code: string, message?: string) {
  return fulfill(route, { detail: { code, message: message ?? code } }, status);
}
