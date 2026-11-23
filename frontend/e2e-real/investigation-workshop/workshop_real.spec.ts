import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

import {
  expect,
  test,
  type APIRequestContext,
  type APIResponse,
  type Page,
} from "@playwright/test";

/**
 * M24 investigation-workshop real-backend journey, steps 1–24.
 *
 * These specs run against the launcher in
 * `backend/tests/workflow_emulator/m24_browser_server.py`: a throwaway
 * PostgreSQL database migrated to head, the real FastAPI application, the real
 * host workspace sidecar with fake H1 CLI profiles, a fake VirusTotal and an
 * auto-answering Bridge. No API is intercepted here: the browser drives the
 * real routes, and the launcher publishes canonical state, PostgreSQL
 * diagnostics and host workspace facts under `/api/m24-browser/state`.
 *
 * Host effects — the D7 signed tool calls and the instruction acknowledgement —
 * are played by the launcher's `POST /__m24_browser/signed-session-action`
 * control route. That route derives the synthetic per-session secret inside
 * the backend process and signs the exact D7 message there; the secret never
 * reaches this spec, the browser, a trace or a log.
 *
 * Steps 12–24 (workspace note, instruction delivery/ack, host-signed result,
 * Claude transfer, task consultation, canonical anchors, note proposals,
 * corrections, stale refusal, real Typst PDF) and the G2 extend/revoke
 * lifecycle run after the F7a steps 1–11.
 */

interface BrowserMaterial {
  hashes: string[];
  downloadable_hashes: string[];
  unavailable_hashes: string[];
  domains: string[];
  ips: string[];
}

interface StateFile {
  fixture: string;
  database_name: string;
  api_port: number;
  subject_id: string;
  subject_slug: string;
  edition_id: string;
  production_run_id: string;
  synthesis_artifact_id: string;
  synthesis_sha256: string;
  publication_artifact_id: string;
  publication_sha256: string;
  workspace_root: string;
  material: BrowserMaterial;
}

interface Fixtures {
  bridge_submissions: number;
  bridge_consultation_types: string[];
  vt_calls: Record<string, number>;
  vt_download_calls: number;
  vt_downloaded_hashes: string[];
}

interface InstructionDiagnostic {
  instruction_id: string;
  state: string;
  text_sha256: string;
  queued_at: string;
  delivered_at: string | null;
  acknowledged_at: string | null;
  delivery_attempts: number;
  delivery_mode: string | null;
  last_delivery_error: string | null;
  cancelled_at: string | null;
  cancel_reason: string | null;
}

interface HostRunDiagnostic {
  state: string;
  runs: number;
  exit: { code: number | null; signal: number | null } | null;
}

interface SessionDiagnostic {
  id: string;
  task_id: string;
  adapter_kind: string;
  state: string;
  host_session_ref: string | null;
  handoff_id: string | null;
  task_lease_fence: number | null;
  host: HostRunDiagnostic | null;
  instructions: InstructionDiagnostic[];
}

interface TaskDiagnostic {
  id: string;
  task_key: string;
  title: string;
  status: string;
  status_version: number;
  priority: string;
  assigned_actor_kind: string | null;
  assigned_actor_id: string | null;
  result_verification_status: string;
  result_refs: Array<Record<string, unknown>>;
  lease_fence: number;
  lease_session_id: string | null;
  handoff_ref: string | null;
}

interface HandoffDiagnostic {
  handoff_id: string;
  task_id: string;
  version: number;
}

interface NoteProposalDiagnostic {
  proposal_id: string;
  anchor: string;
  status: string;
  status_version: number;
  proposed_text: string | null;
  proposed_text_sha256: string | null;
  bridge_block_type: string;
  target_block_type: string;
  base_opinion_version: number | null;
  base_opinion_sha256: string | null;
  base_publication_artifact_id: string | null;
  base_publication_sha256: string | null;
  content_blocks: Array<Record<string, unknown>>;
  materialised: boolean;
  task_id: string | null;
}

interface OpinionNoteDiagnostic {
  note_id: string;
  anchor: string;
  text: string;
  content_blocks: Array<Record<string, unknown>>;
}

interface OpinionDiagnostic {
  opinion_id: string;
  version: number;
  content_sha256: string;
  base_publication_sha256: string;
  observations: OpinionNoteDiagnostic[];
}

interface ContributionDiagnostic {
  contribution_id: string;
  category: string;
  import_status: string;
  file_relpath: string;
  file_sha256: string;
  observation_id: string | null;
  finding_id: string | null;
}

interface ToolInvocationDiagnostic {
  invocation_id: string;
  tool_name: string;
  status: string;
  task_id: string | null;
  agent_session_id: string | null;
  context_task_id: string | null;
  actor_kind: string | null;
  actor_id: string | null;
}

interface WorkspaceDiagnostic {
  workspace_id: string;
  delivery_code: string;
  article_slug: string;
  workspace_root: string;
  manifest: Record<string, unknown> | null;
  base_samples: string[];
}

interface InvestigationDiagnostic {
  id: string;
  prose_authority: string;
  synthesis_artifact_id: string;
  execution_mode: string | null;
  status: string;
  version: number | null;
  stop_reason: string | null;
  envelope_version: number | null;
  envelope_allowed_providers: string[] | null;
  envelope_external_llm_allowed: boolean | null;
  plan_count: number;
  current_plan_version: number | null;
  task_count: number;
  handoff_count: number;
  session_count: number;
  decision_count: number;
  decision_types: string[];
  tasks: TaskDiagnostic[];
  handoffs: HandoffDiagnostic[];
  sessions: SessionDiagnostic[];
  note_proposals: NoteProposalDiagnostic[];
  opinion: OpinionDiagnostic | null;
  contributions: ContributionDiagnostic[];
  tool_invocations: ToolInvocationDiagnostic[];
  workspace: WorkspaceDiagnostic | null;
}

interface BrowserDiagnostics extends StateFile {
  fixtures: Fixtures;
  investigations: InvestigationDiagnostic[];
}

interface ContentResponse {
  artifact_id: string;
  artifact_version: number;
  artifact_input_hash: string;
  status: string;
  publication_kind: "article" | "brief" | null;
  canonical_content: { title: string };
}

interface ArtifactResponse {
  artifact_id: string;
  production_run_id: string;
  status: string;
  canonical_sha256: string | null;
}

interface InventoryResponse {
  seeds: Array<{ kind: string; value: string }>;
  discovered: Array<{ kind: string; value: string }>;
}

interface SnapshotIoc {
  value: string;
  download: {
    status: string;
    hash_verified_quarantine: boolean;
  } | null;
}

interface SnapshotResponse {
  snapshot_id: string;
  plan_state: string;
  iocs: SnapshotIoc[];
}

interface PlanListResponse {
  current_version: number | null;
  plans: Array<{
    plan_version_id: string;
    version: number;
    parse_status: string;
    current: boolean;
  }>;
}

interface PlanDetailResponse {
  plan_version_id: string;
  content_sha256: string;
  content: { leads: Array<{ title: string }> };
}

interface TaskView {
  id: string;
  task_key: string;
  title: string;
  status: string;
  status_version: number;
  result_verification_status: string;
  result_refs: Array<Record<string, unknown>>;
  handoff_ref: string | null;
  assigned_actor_kind: string | null;
  assigned_actor_id: string | null;
}

interface TaskPage {
  items: TaskView[];
  total: number;
}

interface SessionView {
  id: string;
  task_id: string;
  adapter_kind: string;
  state: string;
  handoff_id: string | null;
}

interface SummaryView {
  operations: unknown;
  budget: unknown;
  broker: unknown;
  handoff: unknown;
  instructions: {
    items: Array<{
      instruction_id: string;
      state: string;
      text: string;
      text_sha256: string;
      delivered_at: string | null;
      acknowledged_at: string | null;
      delivery_mode: string | null;
    }>;
    total: number;
  };
}

interface InvestigationObservationApi {
  id: string;
  kind: string;
  sha256: string;
  source: string;
  observed_at: string;
  object_type: string;
  object_id: string;
}

interface InvestigationApiView {
  id: string;
  observations: InvestigationObservationApi[];
  findings: Array<{ id: string }>;
}

interface ArticleAnchorApi {
  anchor: string;
  text: string;
  text_sha256: string;
}

interface ArticleAnchorCatalogApi {
  base_publication_artifact_id: string;
  base_publication_version: number;
  base_publication_sha256: string;
  source: string;
  anchors: ArticleAnchorApi[];
  elements: Array<{ kind: string; key: string; anchor: string | null }>;
  truncated: boolean;
}

interface ProposalDiffApi {
  proposal: {
    id: string;
    anchor: string;
    status: string;
    status_version: number;
    proposed_text_sha256: string | null;
    base_opinion_version: number | null;
    base_opinion_sha256: string | null;
    base_publication_sha256: string | null;
    materialised: boolean;
  };
  proposed_text: string | null;
  current_note_text: string | null;
  content_blocks_valid: boolean;
  stale: boolean;
  accept_ready: boolean;
  resolved_content_blocks: Array<{
    block_type: string;
    title?: string;
    rows?: Array<{ observation_id: string; object_id: string }>;
  }>;
}

interface OpinionReadApi {
  draft: {
    id: string;
    version: number;
    content_sha256: string;
    base_publication_sha256: string;
    observations: Array<{ id: string; anchor: string; text: string }>;
  } | null;
}

interface ConsultationLedgerApi {
  consultations: Array<{
    invocation_id: string;
    consultation_type: string;
    context_task_id: string | null;
    invocation_status: string;
    parse_status: string;
    reconciliation_state: string;
    created_at: string;
  }>;
}

interface EnvelopeApiView {
  approved: boolean;
  version: number;
  currently_authorized: boolean;
  allowed_providers: string[];
}

interface WorkspaceFileApi {
  path: string;
  revision_id: string;
  sha256: string;
  origin: string;
  import_status: string;
  policy_class: string | null;
}

interface WorkspaceFileListApi {
  workspace_available: boolean;
  files: WorkspaceFileApi[];
}

interface WorkspaceNoteViewApi {
  path: string;
  latest: { sha256: string };
  is_evidence: boolean;
}

interface SignedSessionActionResult {
  status_code: number;
  body: unknown;
}

const LOADER_TITLE = "Verifier le chargeur synthetique";
const INFRA_TITLE = "Verifier l'infrastructure synthetique";

const NOTE_TEXT =
  "NOTE-HUMAINE-PARCOURS: observation locale synthetique ecrite dans le workspace.";
const INSTRUCTION_TEXT =
  "INSTRUCTION-PARCOURS: verifie le second echantillon puis rapporte les octets.";
const CONSULTATION_QUESTION =
  "Critique technique synthetique: les octets observes soutiennent-ils la piste chargeur ?";
const NOTE_A_ANCHOR = "lead:0001";
const NOTE_B_ANCHOR = "section:1:paragraph:0002";
const NOTE_B_OCCURRENCE_ANCHOR = "section:2:paragraph:0001";

const REPO_ROOT = resolve(process.cwd(), "..");
const CAPTURES_DIR = join(REPO_ROOT, "docs", "m24", "captures-m24-1");
const SCREENSHOT_SIZES = [
  [1280, 800],
  [1440, 900],
  [1920, 1080],
] as const;

function isRealBackendLite(): boolean {
  return process.env.M24_REAL_LITE_ATTESTED_FAKE_PROVIDER === "1";
}

function requireRealBackendLite(): void {
  test.skip(
    !isRealBackendLite(),
    "Set M24_REAL_LITE_ATTESTED_FAKE_PROVIDER=1 to select this suite.",
  );
  if (process.env.OPERATOR_API_TOKEN === undefined) {
    throw new Error(
      "OPERATOR_API_TOKEN is required: the Vite proxy injects the synthetic operator token into /api requests.",
    );
  }
}

function readStateFile(): StateFile {
  const path = process.env.WORKFLOW_BROWSER_STATE;
  if (path === undefined || path === "") {
    throw new Error(
      "WORKFLOW_BROWSER_STATE is required: run this suite through `make test-investigation-browser`.",
    );
  }
  const raw = readFileSync(path, "utf-8");
  const token = process.env.OPERATOR_API_TOKEN;
  if (token !== undefined) {
    expect(
      raw.includes(token),
      "the launcher state file must never contain the operator token",
    ).toBe(false);
  }
  const parsed: unknown = JSON.parse(raw);
  if (typeof parsed !== "object" || parsed === null) {
    throw new Error("the launcher state file is not an object");
  }
  return parsed as StateFile;
}

async function getJson<T>(page: Page, path: string): Promise<T> {
  const response = await page.request.get(path);
  expect(
    response.status(),
    `GET ${path} must answer 2xx (${await response.text()})`,
  ).toBe(200);
  return (await response.json()) as T;
}

async function diagnostics(page: Page): Promise<BrowserDiagnostics> {
  return getJson<BrowserDiagnostics>(page, "/api/m24-browser/state");
}

async function firstInvestigation(
  page: Page,
): Promise<InvestigationDiagnostic> {
  await expect
    .poll(async () => (await diagnostics(page)).investigations.length, {
      message: "the workshop start must persist one investigation",
    })
    .toBeGreaterThan(0);
  const payload = await diagnostics(page);
  const investigation = payload.investigations[0];
  if (investigation === undefined) {
    throw new Error("the launcher reported no investigation");
  }
  return investigation;
}

async function controlPost(
  request: APIRequestContext,
  state: StateFile,
  path: string,
): Promise<APIResponse> {
  return request.post(`http://127.0.0.1:${state.api_port}${path}`);
}

async function signedSessionAction(
  request: APIRequestContext,
  state: StateFile,
  body: Record<string, unknown>,
): Promise<SignedSessionActionResult> {
  const response = await request.post(
    `http://127.0.0.1:${state.api_port}/__m24_browser/signed-session-action`,
    { data: body },
  );
  expect(
    response.status(),
    `the signed session control route failed (${await response.text()})`,
  ).toBe(200);
  return (await response.json()) as SignedSessionActionResult;
}

function sha256File(path: string): string {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

/**
 * Wait for the diagnostics to list a proposal matching `predicate`.
 *
 * The review panel keeps the previous proposal mounted while a new request is
 * pending or refused, so a heading assertion can pass on stale content.  A
 * timeout surfaces the request panel's typed error instead of a bare timeout.
 */
async function waitForProposal(
  page: Page,
  predicate: (proposal: NoteProposalDiagnostic) => boolean,
  message: string,
): Promise<void> {
  try {
    await expect
      .poll(
        async () =>
          (await firstInvestigation(page)).note_proposals.filter(predicate)
            .length,
        { message, timeout: 30_000 },
      )
      .toBeGreaterThan(0);
  } catch (error) {
    const alerts = await page
      .locator(
        'section[aria-labelledby="opinion-bridge-request"] [role="alert"]',
      )
      .allTextContents();
    throw new Error(
      `${message}; request panel alerts=${JSON.stringify(alerts)}; ${String(error)}`,
    );
  }
}

function sha256Text(value: string): string {
  return createHash("sha256").update(value, "utf-8").digest("hex");
}

function taskCard(page: Page, title: string) {
  return page
    .locator("article.m24-task-card")
    .filter({ has: page.getByRole("heading", { name: title, exact: true }) });
}

async function capture(page: Page, name: string): Promise<void> {
  mkdirSync(CAPTURES_DIR, { recursive: true });
  for (const [width, height] of SCREENSHOT_SIZES) {
    await page.setViewportSize({ width, height });
    await page.waitForTimeout(150);
    await page.screenshot({
      path: join(CAPTURES_DIR, `${name}-${width}x${height}.png`),
      fullPage: false,
    });
  }
}

function pdftotext(path: string): string {
  return execFileSync("pdftotext", ["-layout", path, "-"], {
    encoding: "utf-8",
  });
}

function normalizePdfText(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

function occurrences(text: string, needle: string): number[] {
  const positions: number[] = [];
  let position = text.indexOf(needle);
  while (position >= 0) {
    positions.push(position);
    position = text.indexOf(needle, position + needle.length);
  }
  return positions;
}

test.beforeEach(() => {
  requireRealBackendLite();
});

test("parcours atelier M24 étapes 1–24 : workspace, sessions, Note, PDF, G2", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(600_000);
  const state = readStateFile();
  expect(state.fixture).toBe("m24-investigation-browser");
  expect(state.material.hashes).toHaveLength(5);
  expect(state.material.downloadable_hashes).toHaveLength(3);

  // Every browser request must stay on loopback and must never carry the
  // synthetic operator token: only the Vite proxy adds it upstream.
  const offLoopbackRequests: string[] = [];
  const browserAuthorizationHeaders: string[] = [];
  page.on("request", (browserRequest) => {
    const url = new URL(browserRequest.url());
    if (url.protocol !== "http:" && url.protocol !== "https:") return;
    if (url.hostname !== "127.0.0.1" && url.hostname !== "localhost") {
      offLoopbackRequests.push(browserRequest.url());
    }
    const authorization = browserRequest.headers()["authorization"];
    if (authorization !== undefined) {
      browserAuthorizationHeaders.push(authorization);
    }
  });

  // Step 1 — open the seeded subject's ARTICLE and prove the exact synthesis
  // and publication identities rendered by the real routes.
  await page.goto(`/subjects/${state.subject_id}`);
  const content = await getJson<ContentResponse>(
    page,
    `/api/subjects/${state.subject_id}/content`,
  );
  expect(content.status).toBe("verified");
  expect(content.publication_kind).toBe("article");
  expect(content.artifact_input_hash).toBe(state.publication_sha256);
  const synthesis = await getJson<ArtifactResponse>(
    page,
    `/api/subjects/${state.subject_id}/production/artifacts/synthesis`,
  );
  expect(synthesis.status).toBe("verified");
  expect(synthesis.canonical_sha256).toBe(state.synthesis_sha256);
  expect(synthesis.production_run_id).toBe(state.production_run_id);
  await expect(
    page.getByRole("heading", { name: content.canonical_content.title }),
  ).toBeVisible();
  await expect(
    page.getByText(`Version ${content.artifact_version}`),
  ).toBeVisible();

  // Step 2 — start the workshop from the subject tab (operator_led,
  // bridge_only) and reach the real cockpit.
  await page
    .getByRole("button", { name: "Enquête / Avis de l’analyste" })
    .click();
  const startWorkshop = page.getByRole("button", {
    name: /Lancer l.atelier d.investigation/,
  });
  await expect(startWorkshop).toBeEnabled();
  await startWorkshop.click();
  await expect(
    page.getByRole("region", { name: "Cockpit d’enquête" }),
  ).toBeVisible();
  const investigation = await firstInvestigation(page);
  expect(investigation.prose_authority).toBe("bridge_only");
  expect(investigation.execution_mode).toBe("operator_led");
  expect(investigation.synthesis_artifact_id).toBe(state.synthesis_artifact_id);

  // Step 3 — overview: host agent available and the real workspace bound,
  // with the manifest the sidecar wrote on disk.
  await expect(
    page.getByRole("region", { name: "Vue d’ensemble de l’enquête" }),
  ).toBeVisible();
  const agentStatus = await getJson<{ status: string }>(
    page,
    "/api/workspace-agent/status",
  );
  expect(agentStatus.status).toBe("available");
  const workspaceStatus = await getJson<{
    workspace_state: string;
    workspace_available: boolean;
    binding: { workspace_id: string } | null;
  }>(page, `/api/investigations/${investigation.id}/workspace`);
  expect(workspaceStatus.workspace_state).toBe("bound");
  expect(workspaceStatus.workspace_available).toBe(true);
  expect(workspaceStatus.binding).not.toBeNull();
  const workspace = investigation.workspace;
  expect(
    workspace,
    "the launcher must expose the bound workspace",
  ).not.toBeNull();
  if (workspace === null) throw new Error("workspace diagnostics missing");
  expect(workspace.workspace_id).toBe(workspaceStatus.binding?.workspace_id);
  expect(workspace.manifest).not.toBeNull();
  expect(workspace.manifest?.["investigation_id"]).toBe(investigation.id);
  expect(workspace.manifest?.["synthesis_sha256"]).toBe(state.synthesis_sha256);
  expect(workspace.manifest?.["subject_id"]).toBe(state.subject_id);
  const manifestPath = join(
    workspace.workspace_root,
    ".autowork",
    "manifest.json",
  );
  expect(statSync(manifestPath).isFile()).toBe(true);
  expect(JSON.parse(readFileSync(manifestPath, "utf-8"))).toMatchObject({
    investigation_id: investigation.id,
    synthesis_sha256: state.synthesis_sha256,
  });
  // Every host file the launcher lists must still hash to its own name.
  for (const digest of workspace.base_samples) {
    expect(
      sha256File(join(workspace.workspace_root, "base_samples", digest)),
    ).toBe(digest);
  }
  await capture(page, "cockpit-overview");

  // Step 4 — IOC inventory: the seeds are the five synthetic hashes, two
  // domains and two documentation IPs; nothing was discovered yet.
  const inventory = await getJson<InventoryResponse>(
    page,
    `/api/investigations/${investigation.id}/bootstrap/inventory`,
  );
  const seedValues = inventory.seeds.map((seed) => seed.value).sort();
  expect(seedValues).toEqual(
    [
      ...state.material.hashes,
      ...state.material.domains,
      ...state.material.ips,
    ].sort(),
  );
  expect(inventory.discovered).toEqual([]);
  await expect(
    page.getByRole("heading", { name: "Inventaire IOC", exact: true }),
  ).toBeVisible();
  await expect(page.getByText(state.material.hashes[0] ?? "")).toBeVisible();

  // Step 5 — approve the initial G2 envelope (reason + confirmation + pinned
  // proposal fingerprint) through the real human decision route.
  await page
    .getByLabel(/Motif de la décision/)
    .fill("Approbation humaine G2 du parcours navigateur synthetique");
  await page
    .getByRole("checkbox", { name: /Je confirme approuver l’enveloppe G2/ })
    .check();
  await page.getByRole("button", { name: /Approuver l’enveloppe/ }).click();
  await expect(
    page.getByText(/Enveloppe G2 approuvée — version/),
  ).toBeVisible();
  const proposal = await getJson<{ already_approved: boolean }>(
    page,
    `/api/investigations/${investigation.id}/bootstrap/envelope-proposal`,
  );
  expect(proposal.already_approved).toBe(true);
  const afterApproval = await firstInvestigation(page);
  expect(afterApproval.envelope_version).toBe(1);
  expect(afterApproval.decision_types).toContain("pivot_approve");

  // Step 6 — run the bootstrap batch (fake VT reads and downloads) and check
  // the persisted snapshot, quarantine hashes and fixture counters.
  await page.getByRole("tab", { name: "Échantillons et résultats" }).click();
  await page.getByRole("button", { name: "Lancer la collecte VT" }).click();
  await expect(
    page.getByRole("button", { name: "Relancer la collecte VT" }),
  ).toBeVisible();
  const snapshot = await getJson<SnapshotResponse>(
    page,
    `/api/investigations/${investigation.id}/bootstrap/snapshot`,
  );
  expect(snapshot.plan_state).toBe("ready");
  expect(snapshot.iocs).toHaveLength(9);
  const downloaded = snapshot.iocs.filter(
    (ioc) =>
      ioc.download !== null &&
      (ioc.download.status === "downloaded" ||
        ioc.download.status === "reused"),
  );
  const failed = snapshot.iocs.filter(
    (ioc) => ioc.download !== null && ioc.download.status === "failed",
  );
  expect(downloaded).toHaveLength(3);
  expect(failed).toHaveLength(2);
  for (const ioc of downloaded) {
    expect(ioc.download?.hash_verified_quarantine).toBe(true);
  }
  const afterBootstrap = await diagnostics(page);
  expect(
    afterBootstrap.fixtures.vt_calls["file_report"] ?? 0,
  ).toBeGreaterThanOrEqual(5);
  expect(
    afterBootstrap.fixtures.vt_calls["object_report"] ?? 0,
  ).toBeGreaterThanOrEqual(4);
  // The fake adapter records every download attempt: the five seeds were all
  // requested, while the snapshot above proves only the three available ones
  // produced quarantine-verified bytes.
  expect([...afterBootstrap.fixtures.vt_downloaded_hashes].sort()).toEqual(
    [...state.material.hashes].sort(),
  );
  expect(afterBootstrap.fixtures.vt_download_calls).toBeGreaterThanOrEqual(5);
  for (const digest of state.material.downloadable_hashes) {
    expect(afterBootstrap.fixtures.vt_downloaded_hashes).toContain(digest);
  }

  // Step 7 — ask Bridge for a plan from the cockpit; the launcher's fake
  // Bridge answers deterministically with the canonical two-lead markdown.
  await page.getByRole("tab", { name: "Plan et tâches" }).click();
  await page
    .getByLabel("Question facultative pour Bridge")
    .fill("Etablis le plan synthetique du parcours navigateur.");
  await page.getByRole("button", { name: "Demander un plan à Bridge" }).click();
  await expect(page.getByText(/Version 1 du plan enregistrée/)).toBeVisible();
  const plans = await getJson<PlanListResponse>(
    page,
    `/api/investigations/${investigation.id}/plans`,
  );
  expect(plans.current_version).toBe(1);
  expect(plans.plans).toHaveLength(1);
  expect(plans.plans[0]?.parse_status).toBe("parsed");
  const planId = plans.plans[0]?.plan_version_id;
  if (planId === undefined) throw new Error("the plan version is missing");
  const plan = await getJson<PlanDetailResponse>(
    page,
    `/api/investigations/${investigation.id}/plans/${planId}`,
  );
  expect(plan.content.leads.map((lead) => lead.title)).toEqual([
    LOADER_TITLE,
    INFRA_TITLE,
  ]);
  await expect(
    page.getByRole("heading", { name: `Piste 1 — ${LOADER_TITLE}` }),
  ).toBeVisible();
  const afterPlan = await diagnostics(page);
  expect(afterPlan.fixtures.bridge_submissions).toBe(1);
  expect(afterPlan.fixtures.bridge_consultation_types).toContain(
    "plan_investigation",
  );
  expect(afterPlan.investigations[0]?.plan_count).toBe(1);

  // Step 8 — accept the plan version (human CAS decision) and materialise the
  // tasks exactly once.
  await page
    .getByRole("checkbox", {
      name: /Je confirme l’acceptation de cette version/,
    })
    .check();
  await page
    .getByRole("button", { name: "Accepter le plan et créer les tâches" })
    .click();
  await expect(
    page.getByRole("heading", { name: LOADER_TITLE, exact: true }),
  ).toBeVisible();
  const tasks = await getJson<TaskPage>(
    page,
    `/api/investigations/${investigation.id}/tasks`,
  );
  expect(tasks.total).toBe(2);
  expect(tasks.items.map((task) => task.title).sort()).toEqual(
    [LOADER_TITLE, INFRA_TITLE].sort(),
  );
  const afterAccept = await diagnostics(page);
  expect(afterAccept.investigations[0]?.task_count).toBe(2);
  expect(afterAccept.investigations[0]?.decision_types).toContain(
    "plan_accept",
  );

  // Step 9 — assign the loader task to a Codex operator: this is handoff v1,
  // no agent starts yet.
  const loader = tasks.items.find((task) => task.title === LOADER_TITLE);
  if (loader === undefined) throw new Error("the loader task is missing");
  const loaderCard = taskCard(page, LOADER_TITLE);
  await loaderCard.getByRole("button", { name: "Attribuer à Codex" }).click();
  await expect(
    loaderCard.getByText(/Passation v1 créée pour Codex CLI/),
  ).toBeVisible();
  const assignedTasks = await getJson<TaskPage>(
    page,
    `/api/investigations/${investigation.id}/tasks`,
  );
  const assignedLoader = assignedTasks.items.find(
    (task) => task.id === loader.id,
  );
  expect(assignedLoader?.status).toBe("assigned");
  expect(assignedLoader?.assigned_actor_kind).toBe("operator");
  expect(assignedLoader?.assigned_actor_id).toBe("operator:codex_cli");
  const afterTransfer = await firstInvestigation(page);
  expect(afterTransfer.handoff_count).toBe(1);
  expect(afterTransfer.handoffs[0]?.version).toBe(1);
  expect(afterTransfer.handoffs[0]?.task_id).toBe(loader.id);

  // Step 10 — the explicit session opening uses the real capability probe and
  // the real sidecar; the fake CLI stays running under the acquired lease.
  const openSession = loaderCard.getByRole("button", {
    name: "Ouvrir la session Codex CLI",
  });
  await expect(openSession).toBeVisible();
  await openSession.click();
  await expect(
    loaderCard.getByText(/session ouverte ; la tâche passe en cours/),
  ).toBeVisible();
  const sessions = await getJson<SessionView[]>(
    page,
    `/api/investigations/${investigation.id}/agent-sessions`,
  );
  expect(sessions).toHaveLength(1);
  const session = sessions[0];
  if (session === undefined) throw new Error("the opened session is missing");
  expect(session.adapter_kind).toBe("codex_cli");
  expect(session.state).toBe("running");
  expect(session.task_id).toBe(loader.id);
  const afterSession = await firstInvestigation(page);
  const runningTask = afterSession.tasks.find((task) => task.id === loader.id);
  expect(runningTask?.status).toBe("running");
  expect(runningTask?.lease_fence).toBe(1);
  expect(runningTask?.lease_session_id).toBe(session.id);
  expect(afterSession.sessions[0]?.id).toBe(session.id);
  expect(afterSession.sessions[0]?.task_lease_fence).toBe(1);

  // Step 11 — progress is visible: the operator panel lists the session and
  // its journal-derived summary, and the real POD drain control is reachable.
  await expect(
    page.getByRole("heading", { name: "Opérateurs actifs" }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "Codex CLI" })).toBeVisible();
  const summary = await getJson<SummaryView>(
    page,
    `/api/investigations/${investigation.id}/agent-sessions/${session.id}/summary`,
  );
  for (const key of [
    "operations",
    "budget",
    "broker",
    "handoff",
    "instructions",
  ] as const) {
    expect(summary[key], `the session summary exposes ${key}`).toBeDefined();
  }
  const drainResponse = await controlPost(
    request,
    state,
    "/__m24_browser/drain-instructions",
  );
  expect(drainResponse.status()).toBe(200);
  expect((await drainResponse.json()) as { drained: boolean }).toMatchObject({
    drained: true,
  });

  // Step 12 — write a human note from the task card. The write goes through
  // the host workspace route, which writes the file and reports its revision;
  // the workspace index only learns about it through the sync in step 13.
  await loaderCard.getByRole("button", { name: "Ajouter une note" }).click();
  await loaderCard.getByLabel("Note (Markdown, non-preuve)").fill(NOTE_TEXT);
  await loaderCard.getByRole("button", { name: "Enregistrer la note" }).click();
  const noteMessage = loaderCard.getByText(/Note enregistrée dans /);
  await expect(noteMessage).toBeVisible();
  const noteMessageText = ((await noteMessage.textContent()) ?? "").trim();
  const notePath = noteMessageText.match(/^Note enregistrée dans (.+)\.$/)?.[1];
  if (notePath === undefined) {
    throw new Error(`the note path is unreadable: ${noteMessageText}`);
  }
  const diskNotePath = join(workspace.workspace_root, notePath);
  expect(statSync(diskNotePath).isFile()).toBe(true);
  expect(readFileSync(diskNotePath, "utf-8")).toContain(NOTE_TEXT);
  const noteSha = sha256File(diskNotePath);
  await capture(page, "task-card");

  // Step 13 — synchronise the workspace: the host watcher has its stability
  // window, then the real sync route ingests the revision and the API lists
  // the note. The note view states it is not evidence.
  await page.getByRole("tab", { name: "Workspace et notes" }).click();
  await page.waitForTimeout(1_200);
  await page.getByRole("button", { name: "Synchroniser maintenant" }).click();
  await expect(page.getByText(/Synchronisation terminée/)).toBeVisible();
  await expect
    .poll(
      async () =>
        (
          await getJson<WorkspaceFileListApi>(
            page,
            `/api/investigations/${investigation.id}/workspace/files?prefix=notes/humaines`,
          )
        ).files.some((file) => file.path === notePath),
      { message: "the synced note appears through the API", timeout: 20_000 },
    )
    .toBe(true);
  const filesAfterNote = await getJson<WorkspaceFileListApi>(
    page,
    `/api/investigations/${investigation.id}/workspace/files?prefix=notes/humaines`,
  );
  expect(filesAfterNote.workspace_available).toBe(true);
  const noteFile = filesAfterNote.files.find((file) => file.path === notePath);
  expect(noteFile, "the synced note must be listed").toBeDefined();
  expect(noteFile?.sha256).toBe(noteSha);
  expect(["ui", "filesystem"]).toContain(noteFile?.origin);
  await page.getByRole("button", { name: "Notes humaines" }).click();
  const noteButton = page
    .locator("button.workspace-notes__file-button")
    .filter({ hasText: notePath });
  await expect(noteButton).toBeVisible();
  await noteButton.click();
  await expect(
    page.getByRole("heading", { name: notePath }).first(),
  ).toBeVisible();
  await expect(
    page.locator("pre.workspace-notes__content").filter({ hasText: NOTE_TEXT }),
  ).toBeVisible();
  const noteView = await getJson<WorkspaceNoteViewApi>(
    page,
    `/api/investigations/${investigation.id}/workspace/notes/${notePath}/view`,
  );
  expect(noteView.path).toBe(notePath);
  expect(noteView.is_evidence).toBe(false);
  expect(noteView.latest.sha256).toBe(noteSha);
  await capture(page, "workspace-notes");

  // Step 14 — enqueue one human instruction from the operators panel. The
  // agent is busy, so the persisted row stays queued (no volatile queue).
  await page.getByRole("tab", { name: "Plan et tâches" }).click();
  const operatorRow = page
    .locator("li.m24-operator")
    .filter({ hasText: "Codex CLI" })
    .filter({ hasText: LOADER_TITLE });
  await expect(operatorRow).toBeVisible();
  await operatorRow
    .getByLabel(/Instruction en cours de session/)
    .fill(INSTRUCTION_TEXT);
  await operatorRow
    .getByRole("button", { name: /Envoyer l.instruction/ })
    .click();
  await expect(
    operatorRow.getByText(/Instruction mise en file persistante/),
  ).toBeVisible();
  const instructionSha = sha256Text(INSTRUCTION_TEXT);
  await expect
    .poll(
      async () =>
        (await firstInvestigation(page)).sessions
          .find((item) => item.id === session.id)
          ?.instructions.map((row) => `${row.state}:${row.text_sha256}`) ?? [],
      { message: "the instruction must be durably queued", timeout: 15_000 },
    )
    .toEqual([`queued:${instructionSha}`]);

  // Step 15 — effective delivery: the host's clean run end is the receive
  // point; one real drain tick delivers the instruction, and the operator's
  // signed acknowledgement moves it to `acknowledged`.
  await expect
    .poll(
      async () =>
        (await firstInvestigation(page)).sessions.find(
          (item) => item.id === session.id,
        )?.host?.state ?? null,
      {
        message: "the host run must reach the instruction receive point",
        timeout: 30_000,
      },
    )
    .toBe("awaiting_instruction");
  await expect
    .poll(
      async () => {
        const response = await controlPost(
          request,
          state,
          "/__m24_browser/drain-instructions",
        );
        expect(response.status()).toBe(200);
        return ((await response.json()) as { delivered: number }).delivered;
      },
      {
        message: "one drain tick must deliver the queued instruction",
        timeout: 30_000,
      },
    )
    .toBe(1);
  await expect
    .poll(
      async () => {
        const current = await firstInvestigation(page);
        return (
          current.sessions
            .find((item) => item.id === session.id)
            ?.instructions.find((row) => row.text_sha256 === instructionSha)
            ?.state ?? null
        );
      },
      { message: "the instruction must be delivered", timeout: 30_000 },
    )
    .toBe("delivered");
  const deliveredInstruction = (await firstInvestigation(page)).sessions
    .find((item) => item.id === session.id)
    ?.instructions.find((item) => item.text_sha256 === instructionSha);
  if (deliveredInstruction === undefined) {
    throw new Error("the delivered instruction vanished");
  }
  expect(deliveredInstruction.state).toBe("delivered");
  expect(deliveredInstruction.delivered_at).not.toBeNull();
  expect(deliveredInstruction.delivery_mode).toBe("next_run");
  const ack = await signedSessionAction(request, state, {
    kind: "instruction",
    action: "ack",
    session_id: session.id,
    investigation_id: investigation.id,
    instruction_ids: [deliveredInstruction.instruction_id],
  });
  expect(ack.status_code, JSON.stringify(ack.body)).toBe(200);
  const ackRows = ack.body as Array<{
    state: string;
    acknowledged_at: string | null;
  }>;
  expect(ackRows).toHaveLength(1);
  expect(ackRows[0]?.state).toBe("acknowledged");
  expect(ackRows[0]?.acknowledged_at).not.toBeNull();
  const afterAck = await firstInvestigation(page);
  const ackedRow = afterAck.sessions
    .find((item) => item.id === session.id)
    ?.instructions.find((item) => item.text_sha256 === instructionSha);
  expect(ackedRow?.state).toBe("acknowledged");
  expect(ackedRow?.acknowledged_at).not.toBeNull();
  const summaryAfterAck = await getJson<SummaryView>(
    page,
    `/api/investigations/${investigation.id}/agent-sessions/${session.id}/summary`,
  );
  expect(summaryAfterAck.instructions.items[0]?.state).toBe("acknowledged");
  expect(summaryAfterAck.instructions.items[0]?.text).toBe(INSTRUCTION_TEXT);
  // The human sees the journal-derived state after a fresh mount of the panel.
  await page.getByRole("tab", { name: "Vue d’ensemble" }).click();
  await page.getByRole("tab", { name: "Plan et tâches" }).click();
  const refreshedOperatorRow = page
    .locator("li.m24-operator")
    .filter({ hasText: "Codex CLI" })
    .filter({ hasText: LOADER_TITLE });
  await expect(
    refreshedOperatorRow.getByText("Acquittée", { exact: true }),
  ).toBeVisible();
  await expect(refreshedOperatorRow.getByText(/acquittée/)).toBeVisible();
  await capture(page, "operators-panel");

  // Step 16 — the host submits its result through the signed D7 tool route;
  // the human then accepts the result, which is the only canonical write.
  const beforeSubmit = await firstInvestigation(page);
  const loaderBeforeSubmit = beforeSubmit.tasks.find(
    (task) => task.id === loader.id,
  );
  if (loaderBeforeSubmit === undefined) {
    throw new Error("the loader task disappeared before submission");
  }
  const submitResult = await signedSessionAction(request, state, {
    kind: "tool_call",
    session_id: session.id,
    investigation_id: investigation.id,
    tool_name: "task.submit_result",
    schema_version: "1",
    arguments: {
      expected_status_version: loaderBeforeSubmit.status_version,
      lease_fence: loaderBeforeSubmit.lease_fence,
      result_refs: [
        {
          kind: "synthetic_result",
          path: "results/loader.md",
          sha256: sha256Text("m24-browser-loader-result"),
        },
      ],
    },
    idempotency_key: "m24-browser-codex-submit-result",
  });
  expect(submitResult.status_code, JSON.stringify(submitResult.body)).toBe(200);
  const submitPayload = submitResult.body as {
    status: string;
    output: { task: { status: string; result_verification_status: string } };
  };
  expect(submitPayload.status).toBe("awaiting_review");
  expect(submitPayload.output.task.status).toBe("awaiting_review");
  expect(submitPayload.output.task.result_verification_status).toBe(
    "pending_review",
  );
  const submittedDiagnostic = await firstInvestigation(page);
  const loaderSubmitted = submittedDiagnostic.tasks.find(
    (task) => task.id === loader.id,
  );
  expect(loaderSubmitted?.status).toBe("awaiting_review");
  expect(loaderSubmitted?.result_verification_status).toBe("pending_review");
  expect(loaderSubmitted?.result_refs).toHaveLength(1);
  await page.getByRole("tab", { name: "Vue d’ensemble" }).click();
  await page.getByRole("tab", { name: "Plan et tâches" }).click();
  const submittedLoaderCard = taskCard(page, LOADER_TITLE);
  await submittedLoaderCard
    .getByRole("button", { name: "Accepter le résultat" })
    .click();
  await submittedLoaderCard
    .getByLabel(/Motif d’acceptation \(obligatoire\)/)
    .fill("Acceptation humaine du resultat synthetique verifie");
  await submittedLoaderCard.getByRole("button", { name: "Confirmer" }).click();
  await expect(
    submittedLoaderCard.getByText(/Résultat accepté ; la tâche est terminée/),
  ).toBeVisible();
  const afterAcceptResult = await firstInvestigation(page);
  const completedLoader = afterAcceptResult.tasks.find(
    (task) => task.id === loader.id,
  );
  expect(completedLoader?.status).toBe("completed");
  expect(completedLoader?.result_verification_status).toBe("accepted");
  expect(afterAcceptResult.decision_types).toContain("task_result_accept");

  // Step 17 — transfer the other task to Claude Code: Codex assignment is
  // handoff v1, the transfer is handoff v2, and the explicit probe action
  // opens the fake Claude session under the acquired lease.
  const infraTasks = await getJson<TaskPage>(
    page,
    `/api/investigations/${investigation.id}/tasks`,
  );
  const infra = infraTasks.items.find((task) => task.title === INFRA_TITLE);
  if (infra === undefined)
    throw new Error("the infrastructure task is missing");
  const infraCard = taskCard(page, INFRA_TITLE);
  await infraCard.getByRole("button", { name: "Attribuer à Codex" }).click();
  await expect(
    infraCard.getByText(/Passation v1 créée pour Codex CLI/),
  ).toBeVisible();
  await infraCard.getByRole("button", { name: "Transférer" }).click();
  await infraCard.getByRole("radio", { name: "Claude Code" }).check();
  await infraCard
    .getByLabel(/Instruction pour la session destination/)
    .fill("Poursuis la verification technique sous Claude Code.");
  await infraCard
    .getByRole("button", { name: "Confirmer le transfert" })
    .click();
  await expect(
    infraCard.getByText(/Passation v2 créée pour Claude Code/),
  ).toBeVisible();
  const openClaude = infraCard.getByRole("button", {
    name: "Ouvrir la session Claude Code",
  });
  await expect(openClaude).toBeVisible({ timeout: 30_000 });
  await openClaude.click();
  await expect(
    infraCard.getByText(/session ouverte ; la tâche passe en cours/),
  ).toBeVisible();
  const afterClaudeOpen = await firstInvestigation(page);
  const infraHandoffs = afterClaudeOpen.handoffs
    .filter((handoff) => handoff.task_id === infra.id)
    .map((handoff) => handoff.version)
    .sort();
  expect(infraHandoffs).toEqual([1, 2]);
  const claudeSession = afterClaudeOpen.sessions.find(
    (item) => item.adapter_kind === "claude_code",
  );
  if (claudeSession === undefined) {
    throw new Error("the Claude session was not opened");
  }
  expect(claudeSession.task_id).toBe(infra.id);
  expect(claudeSession.state).toBe("running");
  const infraRunning = afterClaudeOpen.tasks.find(
    (task) => task.id === infra.id,
  );
  expect(infraRunning?.status).toBe("running");
  await capture(page, "task-card-claude");

  // Step 18 — ask Bridge from the task card (critique). The consultation is
  // consultative: the displayed state goes from sent to received, and no task
  // status, result or decision changes.
  const beforeConsultation = await firstInvestigation(page);
  const infraBefore = beforeConsultation.tasks.find(
    (task) => task.id === infra.id,
  );
  const decisionCountBefore = beforeConsultation.decision_count;
  await infraCard.getByRole("button", { name: "Demander à Bridge" }).click();
  await infraCard.getByLabel("Type de demande").selectOption("critique");
  await infraCard
    .getByLabel("Question pour Bridge")
    .fill(CONSULTATION_QUESTION);
  await infraCard
    .getByRole("button", { name: "Envoyer la demande à Bridge" })
    .click();
  await expect(
    infraCard.getByText(/Demande en cours auprès de Bridge/),
  ).toBeVisible({ timeout: 10_000 });
  await expect(infraCard.getByText(/Demande : Reçue — Parsée/)).toBeVisible({
    timeout: 30_000,
  });
  const ledger = await getJson<ConsultationLedgerApi>(
    page,
    `/api/investigations/${investigation.id}/consultations?task_id=${infra.id}`,
  );
  const critique = ledger.consultations.find(
    (row) => row.consultation_type === "critique",
  );
  expect(
    critique,
    "the critique consultation must be registered",
  ).toBeDefined();
  expect(critique?.context_task_id).toBe(infra.id);
  expect(critique?.invocation_status).toBe("succeeded");
  expect(critique?.parse_status).toBe("parsed");
  await expect(infraCard.getByText(/Critique technique · Reçue/)).toBeVisible();
  const afterConsultation = await firstInvestigation(page);
  const infraAfter = afterConsultation.tasks.find(
    (task) => task.id === infra.id,
  );
  expect(infraAfter?.status).toBe(infraBefore?.status);
  expect(infraAfter?.status_version).toBe(infraBefore?.status_version);
  expect(afterConsultation.decision_count).toBe(decisionCountBefore);
  // The PostgreSQL invocation journal carries the human consultation with its
  // task context and a successful status (the route never mutates the task).
  const consultationInvocation = afterConsultation.tool_invocations.find(
    (item) =>
      item.tool_name === "advisor.consult" &&
      item.context_task_id === infra.id &&
      item.actor_kind === "human",
  );
  expect(
    consultationInvocation,
    "the consultation must persist one human invocation line",
  ).toBeDefined();
  expect(consultationInvocation?.status).toBe("succeeded");

  // Step 19 — drafting starts from the Note view with the task pre-filled by
  // the task-card link (`?vue=opinion&task_id=…&bridge_intent=draft_text`).
  await infraCard
    .getByRole("link", { name: "Rédiger une Note avec Bridge" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Note de l’analyste" }),
  ).toBeVisible();
  const url = new URL(page.url());
  expect(url.searchParams.get("vue")).toBe("opinion");
  expect(url.searchParams.get("task_id")).toBe(infra.id);
  expect(url.searchParams.get("bridge_intent")).toBe("draft_text");
  await expect(page.locator("#opinion-note-task")).toHaveValue(infra.id, {
    timeout: 15_000,
  });

  // Step 20 — canonical anchor selection: the base identity is displayed, the
  // duplicate paragraph carries its real occurrence, and the manual entry
  // stays folded as a diagnostic escape hatch.
  const catalog = await getJson<ArticleAnchorCatalogApi>(
    page,
    `/api/investigations/${investigation.id}/article-anchors`,
  );
  expect(catalog.truncated).toBe(false);
  expect(catalog.anchors.map((item) => item.anchor)).toEqual(
    expect.arrayContaining([
      NOTE_A_ANCHOR,
      NOTE_B_ANCHOR,
      NOTE_B_OCCURRENCE_ANCHOR,
    ]),
  );
  const baseLine = page.getByText(/Base de publication :/).first();
  await expect(baseLine).toBeVisible();
  expect(catalog.base_publication_sha256).toBe(state.publication_sha256);
  await expect(
    baseLine.getByText(catalog.base_publication_sha256.slice(0, 12)),
  ).toBeVisible();
  const anchorSelect = page.locator("#opinion-note-anchor");
  await expect(anchorSelect.locator("option")).toHaveCount(
    catalog.anchors.length + 1,
  );
  const optionTexts = await anchorSelect.locator("option").allTextContents();
  const optionValues = await anchorSelect
    .locator("option")
    .evaluateAll((options) =>
      options.map((option) => {
        const element = option as unknown as {
          getAttribute(name: string): string | null;
        };
        return element.getAttribute("value") ?? "";
      }),
    );
  const optionLabel = (value: string) => {
    const index = optionValues.indexOf(value);
    return index < 0 ? "" : (optionTexts[index] ?? "");
  };
  expect(optionLabel(NOTE_B_ANCHOR)).toContain("occurrence 1 sur 2");
  expect(optionLabel(NOTE_B_OCCURRENCE_ANCHOR)).toContain("occurrence 2 sur 2");
  const manualDetails = page.locator("details", {
    hasText: "Diagnostic — saisie manuelle d’ancre",
  });
  await expect(manualDetails).not.toHaveAttribute("open", "");
  await expect(page.locator("#opinion-note-anchor-manual")).toBeHidden();
  await anchorSelect.selectOption(NOTE_A_ANCHOR);
  const leadAnchor = catalog.anchors.find(
    (item) => item.anchor === NOTE_A_ANCHOR,
  );
  if (leadAnchor === undefined) throw new Error("the lead anchor is missing");
  await expect(page.locator("#opinion-note-anchor-detail")).toContainText(
    leadAnchor.text.slice(0, 60),
  );
  await anchorSelect.scrollIntoViewIfNeeded();
  await capture(page, "anchor-picker");

  // Step 21 — ask Bridge for one note at the lead anchor with a verified IOC
  // table reference; the stored text, its SHA-256 and the resolved block are
  // all asserted through the API and the review panel.
  const investigationApi = await getJson<InvestigationApiView>(
    page,
    `/api/investigations/${investigation.id}`,
  );
  const observation = investigationApi.observations.find(
    (item) =>
      item.source.toLowerCase().includes("virustotal") &&
      item.object_type === "file" &&
      state.material.hashes.includes(item.object_id),
  );
  if (observation === undefined) {
    throw new Error(
      "the bootstrap must persist a canonical VT file observation",
    );
  }
  await page
    .getByRole("group", { name: "Observations citées" })
    .getByRole("checkbox", {
      name: new RegExp(observation.sha256.slice(0, 12)),
    })
    .first()
    .check();
  const iocFieldset = page.getByRole("group", {
    name: "Tableau IOC depuis les observations canoniques",
  });
  await iocFieldset
    .getByRole("checkbox", { name: new RegExp(observation.object_id) })
    .first()
    .check();
  await iocFieldset
    .getByRole("button", { name: /Ajouter le tableau IOC \(1\)/ })
    .click();
  await expect(
    page.getByText(/Tableau IOC \(1\).*1 observation\(s\) canonique/),
  ).toBeVisible();
  await page.getByRole("button", { name: "Demander à Bridge" }).click();
  await waitForProposal(
    page,
    (proposal) =>
      proposal.anchor === NOTE_A_ANCHOR &&
      proposal.status === "proposed" &&
      proposal.proposed_text !== null,
    "the first note proposal must be persisted and materialised",
  );
  const firstProposal = (await firstInvestigation(page)).note_proposals.find(
    (proposal) => proposal.anchor === NOTE_A_ANCHOR,
  );
  if (firstProposal === undefined) {
    throw new Error("the first note proposal was not persisted");
  }
  expect(firstProposal.status).toBe("proposed");
  expect(firstProposal.materialised).toBe(true);
  expect(firstProposal.task_id).toBe(infra.id);
  expect(firstProposal.proposed_text).not.toBeNull();
  const firstText = firstProposal.proposed_text ?? "";
  expect(firstText.split("\n\n")).toHaveLength(2);
  expect(firstText).toContain("premier fait verifie sur les octets");
  expect(firstText).toContain(
    "la continuite du saut de paragraphe est conservee telle quelle",
  );
  const firstToken = firstText.match(/pour ([A-Za-z0-9]{1,8}) :/)?.[1];
  expect(firstToken, "the draft text carries its short token").toBeDefined();
  expect(firstProposal.proposed_text_sha256).toBe(sha256Text(firstText));
  expect(firstProposal.content_blocks).toHaveLength(1);
  expect(firstProposal.content_blocks[0]?.["block_type"]).toBe("ioc_table");
  const firstDiff = await getJson<ProposalDiffApi>(
    page,
    `/api/investigations/${investigation.id}/note-proposals/${firstProposal.proposal_id}`,
  );
  expect(firstDiff.proposed_text).toBe(firstText);
  expect(firstDiff.accept_ready).toBe(true);
  expect(firstDiff.content_blocks_valid).toBe(true);
  expect(firstDiff.current_note_text).toBeNull();
  const resolvedTable = firstDiff.resolved_content_blocks.find(
    (block) => block.block_type === "ioc_table",
  );
  expect(resolvedTable?.title).toBe("Tableau IOC (1)");
  expect(resolvedTable?.rows?.[0]?.object_id).toBe(observation.object_id);
  await expect(
    page.getByRole("heading", {
      name: "Aperçu lecture seule des blocs vérifiés",
    }),
  ).toBeVisible();
  await expect(
    page.locator("table caption").filter({ hasText: "Tableau IOC (1)" }),
  ).toBeVisible();
  await expect(
    page.getByText(firstProposal.proposed_text_sha256 ?? ""),
  ).toBeVisible();
  await page
    .getByRole("checkbox", {
      name: /J’ai examiné le texte stocké/,
    })
    .check();
  await capture(page, "note-review");
  await page
    .getByRole("button", { name: new RegExp("Accepter la proposition v\\d+") })
    .click();
  await expect(page.getByText(/Note acceptée/)).toBeVisible();
  const afterFirstAccept = await firstInvestigation(page);
  expect(afterFirstAccept.opinion?.version).toBe(1);
  expect(
    afterFirstAccept.opinion?.observations.find(
      (note) => note.anchor === NOTE_A_ANCHOR,
    )?.text,
  ).toBe(firstText);
  await expect(
    page.getByRole("heading", {
      level: 4,
      name: new RegExp(`Ancre ${NOTE_A_ANCHOR}`),
    }),
  ).toBeVisible();

  // Step 22 — correction: a second targeted Bridge request replaces the note.
  // The new proposal shows the stored text and the paragraph diff, and it is
  // pinned to the current opinion version.
  await page.getByRole("radio", { name: "Corriger" }).check();
  await page.getByRole("button", { name: "Demander à Bridge" }).click();
  await waitForProposal(
    page,
    (proposal) =>
      proposal.anchor === NOTE_A_ANCHOR &&
      proposal.status === "proposed" &&
      proposal.proposal_id !== firstProposal.proposal_id,
    "the correction proposal must be persisted",
  );
  const correctionProposal = (await firstInvestigation(page)).note_proposals
    .filter(
      (proposal) =>
        proposal.anchor === NOTE_A_ANCHOR &&
        proposal.status === "proposed" &&
        proposal.proposal_id !== firstProposal.proposal_id,
    )
    .at(-1);
  if (correctionProposal === undefined) {
    throw new Error("the correction proposal was not persisted");
  }
  const revisedText = correctionProposal.proposed_text ?? "";
  expect(revisedText).not.toBe(firstText);
  expect(revisedText).toContain("corrigee");
  expect(correctionProposal.base_opinion_version).toBe(1);
  expect(correctionProposal.proposed_text_sha256).toBe(sha256Text(revisedText));
  const correctionDiff = await getJson<ProposalDiffApi>(
    page,
    `/api/investigations/${investigation.id}/note-proposals/${correctionProposal.proposal_id}`,
  );
  expect(correctionDiff.current_note_text).toBe(firstText);
  expect(correctionDiff.proposed_text).toBe(revisedText);
  await expect(
    page.getByRole("region", { name: "Comparaison éditoriale" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Différences" }).click();
  await expect(
    page
      .getByRole("group", { name: "Différences du paragraphe" })
      .locator("ins")
      .first(),
  ).toBeVisible();

  // A second note at the duplicate-text anchor is created while the head is
  // still v1: it will be stale after the correction is accepted. The IOC-table
  // reference belongs to note A only, so it is removed before this request.
  await anchorSelect.selectOption(NOTE_B_ANCHOR);
  await page.getByRole("button", { name: "Retirer cette référence" }).click();
  await expect(
    page.getByRole("heading", { name: "Références de contenu (0)" }),
  ).toBeVisible();
  await page.getByRole("radio", { name: "Rédiger" }).check();
  await page.getByRole("button", { name: "Demander à Bridge" }).click();
  await waitForProposal(
    page,
    (proposal) =>
      proposal.anchor === NOTE_B_ANCHOR &&
      proposal.status === "proposed" &&
      proposal.proposed_text !== null,
    "the second-anchor proposal must be persisted",
  );
  const staleCandidate = (await firstInvestigation(page)).note_proposals
    .filter(
      (proposal) =>
        proposal.anchor === NOTE_B_ANCHOR && proposal.status === "proposed",
    )
    .at(-1);
  if (staleCandidate === undefined) {
    throw new Error("the second-anchor proposal was not persisted");
  }
  expect(staleCandidate.base_opinion_version).toBe(1);

  // Step 23 — accept the correction by version/hash (CAS). The earlier
  // second-anchor proposal pinned to v1 is then stale: the UI disables it and
  // the server refuses the same acceptance with 409 stale_note_proposal.
  const selectProposal = async (shortSha: string) => {
    const proposalList = page.locator(
      'section[aria-labelledby="opinion-proposals-heading"]',
    );
    await proposalList
      .locator("li")
      .filter({ hasText: shortSha })
      .getByRole("button", { name: "Examiner la proposition" })
      .click();
  };
  await selectProposal(
    (correctionProposal.proposed_text_sha256 ?? "").slice(0, 12),
  );
  await expect(
    page.getByText(correctionProposal.proposed_text_sha256 ?? ""),
  ).toBeVisible();
  await page
    .getByRole("checkbox", {
      name: /J’ai examiné le texte stocké/,
    })
    .check();
  await page
    .getByRole("button", { name: new RegExp("Accepter la proposition v\\d+") })
    .click();
  await expect(page.getByText(/Note acceptée/)).toBeVisible();
  const afterCorrection = await firstInvestigation(page);
  expect(afterCorrection.opinion?.version).toBe(2);
  const correctedNote = afterCorrection.opinion?.observations.find(
    (note) => note.anchor === NOTE_A_ANCHOR,
  );
  expect(correctedNote?.text).toBe(revisedText);
  await selectProposal(
    (staleCandidate.proposed_text_sha256 ?? "").slice(0, 12),
  );
  await expect(page.getByText(/Proposition périmée/)).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Accepter la proposition v\d+/ }),
  ).toBeDisabled();
  const staleAcceptance = await page.request.post(
    `/api/investigations/${investigation.id}/note-proposals/${staleCandidate.proposal_id}/accept`,
    {
      headers: { "Idempotency-Key": "m24-browser-stale-acceptance" },
      data: {
        expected_status_version: staleCandidate.status_version,
        expected_base_version: staleCandidate.base_opinion_version,
        expected_base_sha256: staleCandidate.base_opinion_sha256,
        reason: "Tentative d'acceptation d'une proposition perimee",
      },
    },
  );
  expect(staleAcceptance.status()).toBe(409);
  const staleBody = (await staleAcceptance.json()) as {
    detail?: { code?: string };
  };
  expect(staleBody.detail?.code).toBe("stale_note_proposal");
  // The note list has refreshed to opinion v2, so the re-pin request pins it.
  await expect(
    page
      .locator('section[aria-labelledby="opinion-notes-list-heading"]')
      .getByText(revisedText.split("\n\n")[0] ?? ""),
  ).toBeVisible();

  // Re-pin the second note on the accepted v2 and accept it as v3.
  await anchorSelect.selectOption(NOTE_B_ANCHOR);
  await page.getByRole("button", { name: "Demander à Bridge" }).click();
  await waitForProposal(
    page,
    (proposal) =>
      proposal.anchor === NOTE_B_ANCHOR &&
      proposal.status === "proposed" &&
      proposal.base_opinion_version === 2,
    "the re-pinned proposal must be persisted",
  );
  const repinned = (await firstInvestigation(page)).note_proposals
    .filter(
      (proposal) =>
        proposal.anchor === NOTE_B_ANCHOR &&
        proposal.status === "proposed" &&
        proposal.base_opinion_version === 2,
    )
    .at(-1);
  if (repinned === undefined) {
    throw new Error("the re-pinned proposal was not persisted");
  }
  expect(repinned.base_opinion_version).toBe(2);
  await page
    .getByRole("checkbox", {
      name: /J’ai examiné le texte stocké/,
    })
    .check();
  await page
    .getByRole("button", { name: new RegExp("Accepter la proposition v\\d+") })
    .click();
  await expect(page.getByText(/Note acceptée/)).toBeVisible();
  const afterSecondNote = await firstInvestigation(page);
  expect(afterSecondNote.opinion?.version).toBe(3);
  const notes = afterSecondNote.opinion?.observations ?? [];
  expect(notes.map((note) => note.anchor).sort()).toEqual(
    [NOTE_A_ANCHOR, NOTE_B_ANCHOR].sort(),
  );
  expect(notes.find((note) => note.anchor === NOTE_A_ANCHOR)?.text).toBe(
    revisedText,
  );
  const secondText =
    notes.find((note) => note.anchor === NOTE_B_ANCHOR)?.text ?? "";
  expect(secondText.length).toBeGreaterThan(0);
  expect(secondText).not.toBe(revisedText);

  // Step 24 — the real Typst preview renders both notes at their anchors with
  // a two-paragraph body each, the verified IOC table and its provenance line.
  const opinionRead = await getJson<OpinionReadApi>(
    page,
    `/api/investigations/${investigation.id}/opinion`,
  );
  const draft = opinionRead.draft;
  if (draft === null) throw new Error("the accepted opinion is missing");
  expect(draft.version).toBe(3);
  await expect(
    page
      .getByRole("link", {
        name: /Ouvrir l’aperçu PDF réel de l’avis v3/,
      })
      .first(),
  ).toBeVisible();
  const previewUrl =
    `/api/opinions/${encodeURIComponent(draft.id)}/preview.pdf?` +
    new URLSearchParams({
      version: String(draft.version),
      opinion_sha256: draft.content_sha256,
      base_publication_sha256: draft.base_publication_sha256,
    }).toString();
  const pdfResponse = await page.request.get(previewUrl);
  expect(pdfResponse.status(), await pdfResponse.text()).toBe(200);
  expect(pdfResponse.headers()["content-type"]).toContain("application/pdf");
  expect(pdfResponse.headers()["x-pdf-sha256"]).toMatch(/^[0-9a-f]{64}$/);
  const lineage = JSON.parse(
    pdfResponse.headers()["x-pdf-lineage"] ?? "{}",
  ) as {
    nature?: string;
    opinion?: { sha256?: string };
    compiler?: { version?: string };
  };
  expect(lineage.nature).toBe("opinion_draft_preview");
  expect(lineage.opinion?.sha256).toBe(draft.content_sha256);
  expect(lineage.compiler?.version).toBe("0.15.1");
  const pdfPath = testInfo.outputPath("opinion-v3.pdf");
  mkdirSync(dirname(pdfPath), { recursive: true });
  writeFileSync(pdfPath, await pdfResponse.body());
  const pdfText = normalizePdfText(pdftotext(pdfPath));
  // Each note keeps its two stored paragraphs (one `\n\n` boundary), and each
  // carries its own short token: the derived content is printed in order.
  const tokenA = revisedText.match(/pour ([A-Za-z0-9]{1,8}) :/)?.[1];
  const tokenB = secondText.match(/pour ([A-Za-z0-9]{1,8}) :/)?.[1];
  expect(tokenA, "the corrected note keeps its token").toBeDefined();
  expect(tokenB, "the second note keeps its token").toBeDefined();
  expect(tokenA).not.toBe(tokenB);
  expect(occurrences(pdfText, tokenA as string)).toHaveLength(2);
  expect(occurrences(pdfText, tokenB as string)).toHaveLength(2);
  expect(
    occurrences(pdfText, "premier fait verifie sur les octets"),
  ).toHaveLength(2);
  expect(
    occurrences(
      pdfText,
      "la continuite du saut de paragraphe est conservee telle quelle",
    ),
  ).toHaveLength(2);
  expect(
    occurrences(pdfText, "Le texte a ete corrige apres revue humaine"),
  ).toHaveLength(1);
  // The corrected text replaced the first version at its anchor.
  expect(pdfText).not.toContain(firstToken as string);
  // Placement: note A at the lead, note B after the first duplicated tail
  // paragraph and before its second occurrence.
  const analysisText = normalizePdfText(
    "ANALYSIS-PARAGRAPH: le second etage synthetique est decrit dans ce paragraphe.",
  );
  const tailText = normalizePdfText(
    "TAIL-PARAGRAPH: portee synthetique finale de l'article.",
  );
  const noteAPosition = pdfText.indexOf(tokenA as string);
  const noteBPosition = pdfText.indexOf(tokenB as string);
  const analysisPosition = pdfText.indexOf(analysisText);
  const tailPositions = occurrences(pdfText, tailText);
  expect(noteAPosition).toBeGreaterThan(0);
  expect(noteBPosition).toBeGreaterThan(noteAPosition);
  expect(analysisPosition).toBeGreaterThan(0);
  expect(tailPositions).toHaveLength(2);
  expect(noteAPosition).toBeLessThan(analysisPosition);
  expect(analysisPosition).toBeLessThan(tailPositions[0]);
  expect(tailPositions[0]).toBeLessThan(noteBPosition);
  expect(noteBPosition).toBeLessThan(tailPositions[1]);
  // The verified IOC table and its provenance line are printed inside note A.
  const tablePosition = pdfText.indexOf("Tableau IOC (1)");
  const provenancePosition = pdfText.indexOf(
    "Observations canoniques de cette enquête : 1",
  );
  expect(tablePosition).toBeGreaterThan(noteAPosition);
  expect(provenancePosition).toBeGreaterThan(tablePosition);
  expect(provenancePosition).toBeLessThan(analysisPosition);
  expect(occurrences(pdfText, "Tableau IOC (1)")).toHaveLength(1);
  expect(
    occurrences(pdfText, "Observations canoniques de cette enquête : 1"),
  ).toHaveLength(1);
  expect(pdfText).toContain("empreintes vérifiées");
  await page
    .locator('iframe[title="Aperçu PDF réel de l’avis"]')
    .first()
    .scrollIntoViewIfNeeded();
  await capture(page, "pdf-preview");
  mkdirSync(CAPTURES_DIR, { recursive: true });
  execFileSync(
    "pdftoppm",
    [
      "-png",
      "-r",
      "72",
      "-f",
      "1",
      "-l",
      "1",
      pdfPath,
      join(CAPTURES_DIR, "pdf-page"),
    ],
    { encoding: "utf-8" },
  );

  // G2 lifecycle — extend then revoke the approved envelope at the end of the
  // journey, so no later Bridge or provider action depends on the rights. The
  // envelope version advances with every budget-changing invocation, so the
  // CAS expectations are relative to the current version, never absolute.
  await page.getByRole("tab", { name: "Vue d’ensemble" }).click();
  await expect(
    page.getByRole("heading", { name: "Cycle de vie de l’enveloppe G2" }),
  ).toBeVisible();
  await expect(page.getByText(/Enveloppe G2 active — version/)).toBeVisible();
  const beforeExtension = await getJson<EnvelopeApiView>(
    page,
    `/api/investigations/${investigation.id}/envelope`,
  );
  const extendedVersion = beforeExtension.version + 1;
  await page.getByRole("button", { name: "Étendre l’enveloppe" }).click();
  await page
    .getByLabel("Motif de l’extension")
    .fill("Extension humaine synthetique du parcours navigateur");
  await page
    .getByRole("checkbox", {
      name: /Je confirme cette extension de l’enveloppe version/,
    })
    .check();
  await page.getByRole("button", { name: "Confirmer l’extension" }).click();
  try {
    await expect
      .poll(
        async () =>
          (
            await getJson<EnvelopeApiView>(
              page,
              `/api/investigations/${investigation.id}/envelope`,
            )
          ).version,
        {
          message: `the envelope extension must commit version ${extendedVersion}`,
          timeout: 15_000,
        },
      )
      .toBe(extendedVersion);
  } catch (error) {
    const lifecycle = await page
      .locator('section[aria-labelledby="overview-envelope-lifecycle-heading"]')
      .innerText();
    throw new Error(
      `the envelope extension did not commit; lifecycle=${lifecycle.slice(0, 2000)}; ${String(error)}`,
    );
  }
  await expect(
    page.getByText(
      new RegExp(`Enveloppe étendue — version ${extendedVersion}`),
    ),
  ).toBeVisible();
  const extendedEnvelope = await getJson<EnvelopeApiView>(
    page,
    `/api/investigations/${investigation.id}/envelope`,
  );
  expect(extendedEnvelope.version).toBe(extendedVersion);
  expect(extendedEnvelope.currently_authorized).toBe(true);
  await expect(
    page.getByText(
      new RegExp(`Enveloppe G2 active — version ${extendedVersion}`),
    ),
  ).toBeVisible();
  await page.getByRole("button", { name: "Révoquer l’enveloppe" }).click();
  await page
    .getByLabel("Motif obligatoire de la révocation")
    .fill("Revocation humaine synthetique de fin de parcours");
  await page
    .getByRole("checkbox", {
      name: /Je confirme la révocation de l’enveloppe version/,
    })
    .check();
  await page.getByRole("button", { name: "Confirmer la révocation" }).click();
  await expect(
    page.getByText(/Enveloppe révoquée — droits externes coupés/),
  ).toBeVisible();
  const revokedEnvelope = await getJson<EnvelopeApiView>(
    page,
    `/api/investigations/${investigation.id}/envelope`,
  );
  expect(revokedEnvelope.currently_authorized).toBe(false);
  await expect(
    page.getByRole("button", { name: /Approuver l’enveloppe/ }),
  ).toHaveCount(0);
  const afterRevoke = await firstInvestigation(page);
  // Revocation writes the zero-external rights envelope one version further.
  expect(afterRevoke.envelope_version).toBe(extendedVersion + 1);
  expect(afterRevoke.envelope_allowed_providers).toEqual([]);
  expect(afterRevoke.decision_types).toContain("pivot_reject");
  await capture(page, "overview-revoked");

  // The whole journey happened on loopback, through the real routes only.
  expect(
    offLoopbackRequests,
    "the browser must never contact a non-loopback host",
  ).toEqual([]);
  expect(
    browserAuthorizationHeaders,
    "the browser never holds the operator token",
  ).toEqual([]);
  const finalState = await diagnostics(page);
  const rawState = JSON.stringify(finalState);
  const token = process.env.OPERATOR_API_TOKEN;
  if (token !== undefined) {
    expect(rawState).not.toContain(token);
  }
  expect(rawState).not.toContain("session_secret");
  expect(rawState).not.toContain("nonce");
});
