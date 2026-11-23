import { createHash } from "node:crypto";
import { readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import {
  expect,
  test,
  type APIRequestContext,
  type APIResponse,
  type Page,
} from "@playwright/test";

/**
 * M24 investigation-workshop real-backend journey, steps 1–11.
 *
 * These specs run against the launcher in
 * `backend/tests/workflow_emulator/m24_browser_server.py`: a throwaway
 * PostgreSQL database migrated to head, the real FastAPI application, the real
 * host workspace sidecar with fake H1 CLI profiles, a fake VirusTotal and an
 * auto-answering Bridge. No API is intercepted here: the browser drives the
 * real routes, and the launcher publishes canonical state, PostgreSQL
 * diagnostics and host workspace facts under `/api/m24-browser/state`.
 *
 * Steps 12–24 (workspace note, instruction delivery/ack, host-signed results,
 * task consultations, note proposals, anchors, corrections, Typst PDF) are
 * finalised by a later lot once B5a/B5b/F2/F3 are merged; this file
 * deliberately stops after step 11.
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

interface TaskDiagnostic {
  id: string;
  task_key: string;
  title: string;
  status: string;
  status_version: number;
  priority: string;
  assigned_actor_kind: string | null;
  assigned_actor_id: string | null;
  lease_fence: number;
  lease_session_id: string | null;
  handoff_ref: string | null;
}

interface HandoffDiagnostic {
  handoff_id: string;
  task_id: string;
  version: number;
}

interface SessionDiagnostic {
  id: string;
  task_id: string;
  adapter_kind: string;
  state: string;
  host_session_ref: string | null;
  handoff_id: string | null;
  task_lease_fence: number | null;
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
}

interface SummaryView {
  operations: unknown;
  budget: unknown;
  broker: unknown;
  handoff: unknown;
  instructions: unknown;
}

const LOADER_TITLE = "Verifier le chargeur synthetique";
const INFRA_TITLE = "Verifier l'infrastructure synthetique";

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

function sha256File(path: string): string {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

test.beforeEach(() => {
  requireRealBackendLite();
});

test("parcours atelier M24 étapes 1–11 : article, G2, bootstrap, plan, tâche, session", async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
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
  const loaderCard = page.locator("article.m24-task-card").filter({
    has: page.getByRole("heading", { name: LOADER_TITLE, exact: true }),
  });
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
  // its journal-derived summary, and the real POD drain control is reachable
  // (steps 12–24, including instruction delivery, live in the next lot).
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
});
