import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  NoteAgentContinuationResponseV1,
  NoteRevisionViewV1,
  NoteViewResponseV1,
  WorkspaceConflictListV1,
  WorkspaceConflictResolutionV1,
  WorkspaceContentViewV1,
  WorkspaceFileListV1,
  WorkspaceFileViewV1,
  WorkspaceNoteWriteResultV1,
  WorkspaceStatusV1,
  WorkspaceSyncReportV1,
} from "../api";
import {
  bodyOf,
  createTestQueryClient,
  errorResponse,
  headersOf,
  installFetch,
  jsonResponse,
  type RecordedRequest,
} from "../api/testSupport";
import { WorkspaceNotesView } from "./WorkspaceNotesView";

const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);
const SHA_C = "c".repeat(64);
const SHA_D = "d".repeat(64);
const originalClipboard = Object.getOwnPropertyDescriptor(
  navigator,
  "clipboard",
);

const binding = {
  workspace_id: "ws-1",
  investigation_id: "inv-1",
  subject_id: "subject-1",
  delivery_code: "IR2609",
  article_slug: "bluemoon-cn",
  synthesis_sha256: SHA_A,
  created_at: "2026-11-20T09:00:00Z",
};

const availableStatus = {
  investigation_id: "inv-1",
  workspace_state: "bound",
  workspace_available: true,
  availability_code: "available",
  availability_reason: null,
  binding,
  manifest: {
    schema: "autowork-workspace-manifest.v1",
    workspace_id: "ws-1",
    delivery_code: "IR2609",
    article_slug: "bluemoon-cn",
    subject_id: "subject-1",
    investigation_id: "inv-1",
    synthesis_sha256: SHA_A,
    created_at: "2026-11-20T09:00:00Z",
    created_by: "autowork-workspace-agent",
    layout_version: 1,
    identity_sha256: SHA_D,
  },
  generation: 3,
  cursor: 42,
  counts: {
    files: 2,
    human_notes: 1,
    unresolved_conflicts: 1,
    policy_violations: 0,
    plans: 1,
    tasks: 1,
    findings: 1,
  },
  lastly_observed_at: "2026-11-20T12:00:00Z",
} satisfies WorkspaceStatusV1;

const unavailableStatus = {
  ...availableStatus,
  workspace_state: "unbound",
  workspace_available: false,
  availability_code: "unavailable",
  availability_reason: "L’agent hôte est arrêté.",
  binding: null,
  manifest: null,
  generation: null,
  cursor: null,
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
} satisfies WorkspaceStatusV1;

const noteFile = {
  path: "notes/humaines/hypothese.md",
  revision_id: "revision-2",
  sha256: SHA_B,
  size: 120,
  mode: "0640",
  policy_class: "human_editable",
  origin: "filesystem",
  import_status: "applied",
  task_id: null,
  observed_at: "2026-11-20T11:00:00Z",
  conflict: false,
  history: [
    {
      revision_id: "revision-2",
      sha256: SHA_B,
      size: 120,
      observed_at: "2026-11-20T11:00:00Z",
      origin: "filesystem",
      task_id: null,
      supersedes_revision_id: "revision-1",
    },
    {
      revision_id: "revision-1",
      sha256: SHA_D,
      size: 100,
      observed_at: "2026-11-20T10:00:00Z",
      origin: "ui",
      task_id: "task-1",
      supersedes_revision_id: null,
    },
  ],
} satisfies WorkspaceFileViewV1;

const planFile = {
  path: "plans/initial-v0001.md",
  revision_id: "revision-3",
  sha256: SHA_C,
  size: 2048,
  mode: "0440",
  policy_class: "generated_immutable",
  origin: "system",
  import_status: "applied",
  task_id: null,
  observed_at: "2026-11-20T09:30:00Z",
  conflict: false,
  history: [],
} satisfies WorkspaceFileViewV1;

const fileList = {
  investigation_id: "inv-1",
  workspace_id: "ws-1",
  workspace_available: true,
  files: [noteFile, planFile],
  next_page_token: null,
} satisfies WorkspaceFileListV1;

const unavailableFileList = {
  ...fileList,
  workspace_available: false,
  files: [],
} satisfies WorkspaceFileListV1;

const noteContent = {
  investigation_id: "inv-1",
  workspace_id: "ws-1",
  path: noteFile.path,
  revision_id: noteFile.revision_id,
  sha256: noteFile.sha256,
  size: noteFile.size,
  metadata_only: false,
  text: "# Hypothèse\n\nLe domaine pourrait être un leurre.",
  content_base64: null,
} satisfies WorkspaceContentViewV1;

const planContent = {
  ...noteContent,
  path: planFile.path,
  revision_id: planFile.revision_id,
  sha256: planFile.sha256,
  size: planFile.size,
  text: "# Plan initial\n\nÉtapes de travail.",
} satisfies WorkspaceContentViewV1;

const conflict = {
  conflict_id: "conflict-1",
  path: noteFile.path,
  conflict_path: "notes/humaines/hypothese.conflict-abc12345.md",
  current_sha256: SHA_B,
  proposed_sha256: SHA_C,
  origin: "ui",
  task_id: null,
  observed_at: "2026-11-20T12:00:00Z",
  current: {
    path: noteFile.path,
    sha256: SHA_B,
    size: 120,
    text: "Version locale conservée.",
    binary: false,
  },
  proposed: {
    path: conflictPath(noteFile.path),
    sha256: SHA_C,
    size: 130,
    text: "Version proposée conservée.",
    binary: false,
  },
  diff: "- Version locale conservée.\n+ Version proposée conservée.",
} satisfies WorkspaceConflictListV1["conflicts"][number];

function conflictPath(path: string): string {
  const slash = path.lastIndexOf("/");
  return `${path.slice(0, slash)}/hypothese.conflict-abc12345.md`;
}

const conflictList = {
  investigation_id: "inv-1",
  workspace_id: "ws-1",
  workspace_available: true,
  conflicts: [conflict],
} satisfies WorkspaceConflictListV1;

const unavailableConflictList = {
  ...conflictList,
  workspace_available: false,
  conflicts: [],
} satisfies WorkspaceConflictListV1;

const syncReport = {
  investigation_id: "inv-1",
  workspace_id: "ws-1",
  generation: 3,
  cursor: 43,
  resync: false,
  truncated: false,
  files_observed: 2,
  events_received: 1,
  events_inserted: 1,
  events_existing: 0,
  revisions_inserted: 1,
  new_human_notes: [],
  changed_human_notes: [noteFile.path],
  human_notes_truncated: false,
  conflicts_observed: 0,
  unresolved_conflicts: 1,
  policy_violations: 0,
  lastly_observed_at: "2026-11-20T12:01:00Z",
} satisfies WorkspaceSyncReportV1;

const noteRevision = {
  revision_id: "revision-2",
  path: noteFile.path,
  sha256: SHA_B,
  size: 120,
  observed_at: "2026-11-20T11:00:00Z",
  origin: "filesystem",
  supersedes_revision_id: "revision-1",
} satisfies NoteRevisionViewV1;

const noteView = {
  investigation_id: "inv-1",
  workspace_id: "ws-1",
  path: noteFile.path,
  latest: noteRevision,
  versions: [
    noteRevision,
    {
      ...noteRevision,
      revision_id: "revision-1",
      sha256: SHA_D,
      size: 100,
      observed_at: "2026-11-20T10:00:00Z",
      origin: "ui",
      supersedes_revision_id: null,
    },
  ],
  annotations: [],
  linked_task_ids: ["task-1"],
  linked_iocs: [{ ioc_type: "domain", value: "evil.example" }],
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
} satisfies NoteViewResponseV1;

const noteViewWithoutTask = {
  ...noteView,
  linked_task_ids: [],
} satisfies NoteViewResponseV1;

const taskFixture = {
  id: "task-1",
  investigation_id: "inv-1",
  task_key: "lead-a",
  plan_version_id: null,
  parent_task_id: null,
  depth: 0,
  title: "Vérifier le domaine",
  question: "Le domaine est-il un C2 ?",
  objective: "Confirmer le rôle du domaine.",
  priority: "high",
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
  event_sequence: 1,
  created_by: { actor_kind: "system", actor_id: "plan_accept" },
  created_at: "2026-11-20T09:00:00Z",
  updated_at: "2026-11-20T09:00:00Z",
} as const;

const activityFixture = {
  schema_version: "investigation-activity.v1",
  investigation: {
    id: "inv-1",
    subject_id: "subject-1",
    production_run_id: "run-1",
    synthesis_artifact_id: "synthesis-1",
    synthesis_sha256: SHA_A,
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
    turn_index: 2,
    stop_reason: null,
    hypothesis_count: 1,
    open_hypothesis_count: 0,
  },
  last_activity_at: "2026-11-20T12:00:00Z",
  last_activity_source: "investigation",
  active_backend_job_count: 0,
  budget: null,
  timeline: [],
  next_cursor: null,
  has_more: false,
  blockers: [],
  counts: { inconclusive_findings: 0 },
  generated_at: "2026-11-20T12:01:00Z",
} as const;

const sampleFixture = {
  id: "assoc-1",
  sample_sha256: SHA_B,
  origin: "vt_pivot",
  qualification: "candidate",
  related_sample_sha256: [],
  version: 1,
  changes: [],
} as const;

const activeSessionFixture = {
  id: "session-1",
  investigation_id: "inv-1",
  task_id: "task-1",
  adapter_kind: "codex_cli",
  state: "running",
  state_version: 1,
  host_session_ref: "host-1",
  handoff_id: null,
  capability_sha256: SHA_A,
  probed_at: "2026-11-20T09:00:00Z",
  task_lease_id: null,
  task_lease_fence: 0,
  checkpoint_ref: null,
  checkpoint_sha256: null,
  reconciliation: null,
  reconciliation_sha256: null,
  last_activity_at: "2026-11-20T12:00:00Z",
  opened_at: "2026-11-20T09:00:00Z",
  closed_at: null,
  close_reason: null,
  created_at: "2026-11-20T09:00:00Z",
  updated_at: "2026-11-20T12:00:00Z",
} as const;

const bridgeReviewFixture = {
  investigation_id: "inv-1",
  note_revision_id: "revision-2",
  note_sha256: SHA_B,
  consultation_type: "interpretation",
  invocation_id: "invocation-1",
  invocation_status: "pending",
  pending: true,
  idempotent_replay: false,
  advisory_only: true,
  is_evidence: false,
} as const;

const agentContinuation = {
  investigation_id: "inv-1",
  note_revision_id: noteFile.revision_id,
  note_sha256: SHA_B,
  note_path: noteFile.path,
  task_id: "task-1",
  session_id: "session-1",
  instruction_id: "instruction-1",
  instruction_state: "queued",
  instruction_sha256: SHA_C,
} satisfies NoteAgentContinuationResponseV1;

const writeResult = {
  investigation_id: "inv-1",
  workspace_id: "ws-1",
  path: "notes/humaines/ma-note.md",
  revision: {
    revision_id: "revision-4",
    sha256: SHA_A,
    size: 31,
    observed_at: "2026-11-20T12:05:00Z",
    origin: "ui",
    task_id: null,
    supersedes_revision_id: null,
  },
  created: true,
  already_present: false,
} satisfies WorkspaceNoteWriteResultV1;

const resolution = {
  investigation_id: "inv-1",
  workspace_id: "ws-1",
  conflict_id: "conflict-1",
  path: noteFile.path,
  choice: "keep_proposed",
  decision_id: "decision-9",
  result_revision: {
    revision_id: "revision-5",
    sha256: SHA_C,
    size: 130,
    observed_at: "2026-11-20T12:06:00Z",
    origin: "ui",
    task_id: null,
    supersedes_revision_id: "revision-2",
  },
  current: conflict.current,
  proposed: conflict.proposed,
  diff: conflict.diff,
  idempotent_replay: false,
} satisfies WorkspaceConflictResolutionV1;

interface HandlerOptions {
  status?: WorkspaceStatusV1;
  agentStatus?: "available" | "unavailable" | "unconfigured";
  note?: NoteViewResponseV1;
  sessions?: "active" | "none";
}

function makeHandler(options: HandlerOptions = {}) {
  const status = options.status ?? availableStatus;
  const agentStatus = options.agentStatus ?? "available";
  const sessions = options.sessions ?? "active";
  const files = status.workspace_available ? fileList : unavailableFileList;
  const conflicts = status.workspace_available
    ? conflictList
    : unavailableConflictList;
  return (request: RecordedRequest): Response => {
    const { url } = request;
    const method = request.init?.method ?? "GET";
    const parsedUrl = new URL(url, "http://localhost");
    if (url === "/api/workspace-agent/status") {
      return jsonResponse({ status: agentStatus, instance_id: "instance-1" });
    }
    if (url.includes("/activity")) return jsonResponse(activityFixture);
    if (url.endsWith("/tasks")) {
      return jsonResponse({ items: [taskFixture], total: 1 });
    }
    if (url.endsWith("/samples")) return jsonResponse([sampleFixture]);
    if (url.endsWith("/agent-sessions")) {
      return jsonResponse(sessions === "active" ? [activeSessionFixture] : []);
    }
    if (url.includes("/ask-bridge")) return jsonResponse(bridgeReviewFixture);
    if (url.includes("/ask-agent")) return jsonResponse(agentContinuation);
    if (url.endsWith("/workspace")) return jsonResponse(status);
    if (url.includes("/workspace/files/content")) {
      return jsonResponse(
        parsedUrl.searchParams.get("path") === planFile.path
          ? planContent
          : noteContent,
      );
    }
    if (url.endsWith("/workspace/files") && method === "PUT") {
      return jsonResponse(writeResult);
    }
    if (url.includes("/workspace/files")) {
      const prefix = parsedUrl.searchParams.get("prefix");
      return jsonResponse({
        ...files,
        files:
          prefix === null
            ? files.files
            : files.files.filter((file) => file.path.startsWith(prefix)),
      });
    }
    if (url.includes("/workspace/conflicts/") && url.endsWith("/resolve")) {
      return jsonResponse(resolution);
    }
    if (url.endsWith("/workspace/conflicts")) return jsonResponse(conflicts);
    if (url.includes("/workspace/notes/") && url.endsWith("/view")) {
      return jsonResponse(options.note ?? noteView);
    }
    if (url.endsWith("/workspace/sync")) return jsonResponse(syncReport);
    return errorResponse(404, "not_found");
  };
}

function renderView(handler: (request: RecordedRequest) => Response) {
  const recorder = installFetch(handler);
  const client = createTestQueryClient();
  render(
    <QueryClientProvider client={client}>
      <WorkspaceNotesView investigationId="inv-1" />
    </QueryClientProvider>,
  );
  return recorder;
}

afterEach(() => {
  vi.unstubAllGlobals();
  if (originalClipboard === undefined) {
    Reflect.deleteProperty(navigator, "clipboard");
  } else {
    Object.defineProperty(navigator, "clipboard", originalClipboard);
  }
});

describe("WorkspaceNotesView", () => {
  it("bloque les écritures quand le workspace local est indisponible", async () => {
    renderView(
      makeHandler({ status: unavailableStatus, agentStatus: "unavailable" }),
    );

    expect(
      (await screen.findAllByText("Workspace local indisponible")).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByText(/aucune action n’est mise en file d’attente hors ligne/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Synchroniser maintenant" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Enregistrer la note" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Créer le workspace" }),
    ).not.toBeInTheDocument();
  });

  it("formule le rapport de synchronisation avec les bons pluriels", async () => {
    const user = userEvent.setup();
    renderView(makeHandler());

    await user.click(await screen.findByRole("button", { name: "Actualiser" }));

    expect(
      await screen.findByText(
        /Synchronisation terminée\. 2 fichiers observés, 1 événement ajouté, 1 révision, 0 conflit observé, 1 conflit non résolu, 0 violation de politique\./,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/fichier\(s\)|nouvel\/nouveaux/)).toBeNull();
    // Generation and cursor stay available behind the technical details.
    expect(screen.getByText("Détails techniques")).toBeInTheDocument();
    expect(screen.getByText(/Génération 3 · curseur 43\./)).toBeInTheDocument();
  });

  it("envoie la résolution de conflit avec les deux SHA-256 attendus", async () => {
    const recorder = renderView(makeHandler());
    const user = userEvent.setup();

    await user.click(await screen.findByText("Conflits — 1 non résolu"));

    const choice = await screen.findByLabelText("Résolution");
    await user.selectOptions(choice, "keep_proposed");
    await user.type(
      screen.getByLabelText("Raison (facultatif)"),
      "La proposition contient le complément.",
    );
    await user.click(
      screen.getByRole("button", { name: "Résoudre le conflit" }),
    );

    await waitFor(() => {
      expect(
        recorder.calls.some((call) =>
          call.url.endsWith("/workspace/conflicts/conflict-1/resolve"),
        ),
      ).toBe(true);
    });
    const resolveCall = recorder.calls.find((call) =>
      call.url.endsWith("/workspace/conflicts/conflict-1/resolve"),
    );
    expect(resolveCall?.init?.method).toBe("POST");
    expect(bodyOf(resolveCall?.init)).toEqual({
      choice: "keep_proposed",
      expected_current_sha256: SHA_B,
      expected_proposed_sha256: SHA_C,
      reason: "La proposition contient le complément.",
    });
    expect(headersOf(resolveCall?.init)["Idempotency-Key"]).toMatch(
      /^[A-Za-z0-9._:-]{8,128}$/,
    );
  });

  it("ouvre une note au clavier et étiquette l’hypothèse humaine non vérifiée", async () => {
    const recorder = renderView(makeHandler());
    const user = userEvent.setup();
    const button = await screen.findByRole("button", {
      name: /hypothese\.md/,
    });
    button.focus();
    expect(button).toHaveFocus();
    await user.keyboard("{Enter}");

    expect(
      await screen.findByText("Hypothèse humaine — non vérifiée"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Note de travail — non probante"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "Transmettre à Codex / Claude",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Associer à une tâche" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Demander à Bridge" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Proposer une observation" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Note humaine — Hypothèse" }),
    ).toBeVisible();
    expect(
      screen.getByText(/Le domaine pourrait être un leurre/),
    ).toBeVisible();
    const versions = screen
      .getByText("Versions & provenance")
      .closest("details");
    expect(versions).not.toHaveAttribute("open");
    await user.click(
      screen.getByRole("button", { name: "Transmettre à Codex / Claude" }),
    );
    expect(await screen.findByText(/Session active/)).toBeInTheDocument();
    expect(
      screen.queryByText(/le client API figé n’expose pas la version d’état/),
    ).not.toBeInTheDocument();
    expect(
      recorder.calls.some(
        (call) =>
          call.url.includes("/workspace/files/content") &&
          call.url.includes("hypothese.md"),
      ),
    ).toBe(true);
  });

  it("envoie la relecture Bridge avec la version CAS de l’état de travail", async () => {
    const recorder = renderView(makeHandler());
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", {
        name: /hypothese\.md/,
      }),
    );
    await user.click(screen.getByRole("button", { name: "Demander à Bridge" }));
    await user.type(
      await screen.findByLabelText("Question à Bridge sur cette note"),
      "La note est-elle cohérente avec l’observation ?",
    );
    await user.click(
      screen.getByRole("button", { name: "Demander à Bridge de relire" }),
    );

    await waitFor(() => {
      expect(
        recorder.calls.some((call) => call.url.includes("/ask-bridge")),
      ).toBe(true);
    });
    const call = recorder.calls.find((item) =>
      item.url.includes("/ask-bridge"),
    );
    expect(bodyOf(call?.init)).toEqual({
      consultation_type: "interpretation",
      question: "La note est-elle cohérente avec l’observation ?",
      expected_working_state_version: 5,
    });
    expect(headersOf(call?.init)["Idempotency-Key"]).toMatch(
      /^[A-Za-z0-9._:-]{8,128}$/,
    );
    expect(await screen.findByText(/Relecture Bridge demandée/)).toBeVisible();
  });

  it("conditionne l’instruction agent à une session active", async () => {
    renderView(makeHandler({ sessions: "none" }));
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", {
        name: /hypothese\.md/,
      }),
    );
    await user.click(
      screen.getByRole("button", { name: "Transmettre à Codex / Claude" }),
    );

    expect(
      await screen.findByText(
        /Action indisponible : aucune session active pour cette tâche/,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Transmettre l’instruction" }),
    ).toBeDisabled();
  });

  it("masque l’action agent quand aucune tâche n’est liée à la note", async () => {
    renderView(makeHandler({ note: noteViewWithoutTask }));
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", {
        name: /hypothese\.md/,
      }),
    );
    await user.click(
      screen.getByRole("button", { name: "Transmettre à Codex / Claude" }),
    );

    expect(
      await screen.findByText(
        "Action indisponible : liez d’abord la note à une tâche ; une instruction de session doit citer une tâche de cette enquête.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: "Transmettre l’instruction",
      }),
    ).not.toBeInTheDocument();
  });

  it("présente le navigateur master/detail, un nom lisible et le chemin copiable", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    renderView(makeHandler());

    expect(
      await screen.findByRole("region", { name: "Dossiers et fichiers" }),
    ).toBeVisible();
    expect(
      screen.getByRole("region", { name: "Contenu sélectionné" }),
    ).toBeVisible();
    const fileButton = await screen.findByRole("button", {
      name: /hypothese\.md/,
    });
    expect(fileButton).not.toHaveTextContent(noteFile.path);
    expect(fileButton).toHaveAttribute("title", noteFile.path);
    await user.click(fileButton);

    expect(
      await screen.findByRole("heading", { name: "Note humaine — Hypothèse" }),
    ).toBeVisible();
    expect(
      screen.getByText(/Le domaine pourrait être un leurre/),
    ).toBeVisible();
    await user.click(screen.getByText("Chemin exact"));
    await user.click(
      screen.getByRole("button", { name: "Copier le chemin exact" }),
    );
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(noteFile.path));
  });

  it("affiche le contenu d’un fichier non humain sans actions de note", async () => {
    const user = userEvent.setup();
    renderView(makeHandler());
    await user.click(
      await screen.findByRole("button", { name: "Tous les fichiers" }),
    );
    await user.click(
      await screen.findByRole("button", { name: /initial-v0001\.md/ }),
    );

    expect(await screen.findByText(/Étapes de travail\./)).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "initial-v0001.md" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Éditer" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Transmettre à Codex / Claude" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("Versions & provenance").closest("details"),
    ).not.toHaveAttribute("open");
  });

  it("enregistre une note éditée avec le SHA courant comme CAS", async () => {
    const recorder = renderView(makeHandler());
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: /hypothese\.md/ }),
    );
    await user.click(await screen.findByRole("button", { name: "Éditer" }));
    const editor = await screen.findByLabelText("Contenu Markdown");
    await user.clear(editor);
    await user.type(editor, "# Hypothèse révisée\n\nTexte corrigé.");
    await user.click(screen.getByRole("button", { name: "Enregistrer" }));

    await waitFor(() => {
      expect(recorder.calls.some((call) => call.init?.method === "PUT")).toBe(
        true,
      );
    });
    const put = recorder.calls.find((call) => call.init?.method === "PUT");
    expect(bodyOf(put?.init)).toEqual({
      path: noteFile.path,
      expected_sha256: SHA_B,
      text: "# Hypothèse révisée\n\nTexte corrigé.",
    });
  });

  it("transmet une instruction depuis la note par la route existante UX-09", async () => {
    const recorder = renderView(makeHandler());
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: /hypothese\.md/ }),
    );
    await user.click(
      screen.getByRole("button", { name: "Transmettre à Codex / Claude" }),
    );
    await user.type(
      await screen.findByLabelText("Question à l’agent"),
      "Vérifie cette piste.",
    );
    await user.click(
      screen.getByRole("button", { name: "Transmettre l’instruction" }),
    );

    await waitFor(() => {
      expect(
        recorder.calls.some((call) => call.url.includes("/ask-agent")),
      ).toBe(true);
    });
    const call = recorder.calls.find((item) => item.url.includes("/ask-agent"));
    expect(call?.init?.method).toBe("POST");
    expect(bodyOf(call?.init)).toEqual({
      task_id: "task-1",
      session_id: "session-1",
      question: "Vérifie cette piste.",
    });
    expect(headersOf(call?.init)["Idempotency-Key"]).toMatch(
      /^[A-Za-z0-9._:-]{8,128}$/,
    );
  });

  it("écrit une note humaine Markdown avec une clé d’idempotence", async () => {
    const recorder = renderView(makeHandler());
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Nouvelle note" }),
    );
    await screen.findByLabelText("Chemin de la note");
    await user.type(screen.getByLabelText("Chemin de la note"), "ma-note.md");
    await user.type(
      screen.getByLabelText("Contenu Markdown"),
      "# Hypothèse\n\nÀ vérifier.",
    );
    await user.click(
      screen.getByRole("button", { name: "Enregistrer la note" }),
    );

    await waitFor(() => {
      expect(recorder.calls.some((call) => call.init?.method === "PUT")).toBe(
        true,
      );
    });
    const put = recorder.calls.find((call) => call.init?.method === "PUT");
    expect(put?.url).toBe("/api/investigations/inv-1/workspace/files");
    expect(bodyOf(put?.init)).toEqual({
      path: "notes/humaines/ma-note.md",
      expected_sha256: "new",
      text: "# Hypothèse\n\nÀ vérifier.",
    });
    expect(headersOf(put?.init)["Idempotency-Key"]).toMatch(
      /^[A-Za-z0-9._:-]{8,128}$/,
    );
  });
});
