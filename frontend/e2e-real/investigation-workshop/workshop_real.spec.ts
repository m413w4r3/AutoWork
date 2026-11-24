import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";

import {
  expect,
  test,
  type APIRequestContext,
  type APIResponse,
  type Locator,
  type Page,
} from "@playwright/test";

/**
 * M24 investigation-workshop real-backend journey, steps 1–32.
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
 * The journey covers overview, workspace, the searchable task list, fake VT,
 * fake Bridge/Codex/Claude, instruction delivery, canonical article anchors,
 * M23/IOC/code references, real Typst output and G2 revocation.
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
  shodan_calls: Array<[string, unknown[]]>;
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
  base_publication_version: number | null;
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
  operations: Array<{
    status: string;
    error_code: string | null;
  }>;
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
  anchors: Array<
    ArticleAnchorApi & {
      section_index: number | null;
      paragraph_index: number | null;
    }
  >;
  sections: Array<{ section_index: number; heading: string }>;
  elements: Array<{
    kind: string;
    key: string;
    anchor: string | null;
    title: string | null;
    caption: string | null;
    element_sha256: string;
    proposable: boolean;
    refusal_reason: string | null;
  }>;
  truncated: boolean;
}

interface InvestigationActivityApi {
  working_state: { version: number } | null;
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
    kind?: string;
    element_key?: string;
    element_sha256?: string;
    anchor?: string;
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
  "NOTE-HUMAINE-PARCOURS: première observation locale synthétique.";
const NOTE_EDITED_TEXT =
  "NOTE-HUMAINE-PARCOURS: version éditée dans le workspace avant transmission à Codex.";
const NOTE_PATH = "notes/humaines/m24-e4-note.md";
const INSTRUCTION_TEXT =
  "INSTRUCTION-PARCOURS: relis la note locale éditée, vérifie le second échantillon puis rapporte les octets.";
const CONSULTATION_QUESTION =
  "Critique technique synthetique: les octets observes soutiennent-ils la piste chargeur ?";
const NOTE_A_ANCHOR = "section:1:paragraph:0002";
const NOTE_B_ANCHOR = "section:1:paragraph:0003";
const ARTICLE_TABLE_KEY = "d2-article-a-table";
const ARTICLE_FIGURE_KEY = "d2-article-b-figure";

const REPO_ROOT = resolve(process.cwd(), "..");
const CAPTURES_DIR =
  process.env.M24_REAL_CAPTURE_DIR ??
  join(REPO_ROOT, "docs", "m24", "captures-m24-2");
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

async function rejectArticleElementReference(
  page: Page,
  investigationId: string,
  input: {
    anchor: string;
    kind: "table" | "figure";
    element_key: string;
    element_sha256: string;
    idempotencyKey: string;
  },
): Promise<void> {
  const activity = await getJson<InvestigationActivityApi>(
    page,
    `/api/investigations/${investigationId}/activity?limit=1`,
  );
  const workingStateVersion = activity.working_state?.version;
  if (workingStateVersion === undefined) {
    throw new Error("the investigation working-state version is unavailable");
  }
  const response = await page.request.post(
    `/api/investigations/${investigationId}/note-proposals`,
    {
      headers: { "Idempotency-Key": input.idempotencyKey },
      data: {
        anchor: input.anchor,
        target_block_type: "section",
        bridge_block_type: "key_findings",
        content_blocks: [
          {
            block_type: "article_element",
            kind: input.kind,
            element_key: input.element_key,
            element_sha256: input.element_sha256,
            anchor: input.anchor,
          },
        ],
        expected_working_state_version: workingStateVersion,
      },
    },
  );
  expect(response.status()).toBeGreaterThanOrEqual(400);
  const body = (await response.json()) as {
    detail?: { code?: string; message?: string };
  };
  expect(body.detail?.code).toBe("note_block_ref_stale");
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

async function capture(
  page: Page,
  name: string,
  target?: Locator,
): Promise<void> {
  mkdirSync(CAPTURES_DIR, { recursive: true });
  for (const [width, height] of SCREENSHOT_SIZES) {
    await page.setViewportSize({ width, height });
    if (target !== undefined) {
      await expect(target).toBeVisible();
      await target.scrollIntoViewIfNeeded();
    }
    await page.waitForTimeout(100);
    await page.screenshot({
      path: join(CAPTURES_DIR, `${name}-${width}x${height}.png`),
      fullPage: false,
    });
  }
}

async function refreshWorkspace(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Actualiser" }).click();
  await expect(page.getByRole("button", { name: "Actualiser" })).toBeEnabled();
  await expect(page.getByText(/Actualisation en cours/)).toHaveCount(0);
  await expect(page.getByText(/Synchronisation terminée/)).toBeVisible();
}

async function assertCockpitResponsive(page: Page): Promise<void> {
  const cockpit = page.getByRole("region", { name: "Cockpit d’enquête" });
  await page.setViewportSize({ width: 1280, height: 800 });
  const viewport = await page.locator("html").evaluate((element) => {
    const root = element as unknown as {
      clientWidth: number;
      scrollWidth: number;
    };
    return { clientWidth: root.clientWidth, scrollWidth: root.scrollWidth };
  });
  expect(
    viewport.scrollWidth,
    "the investigation page must not overflow horizontally at 1280 px",
  ).toBeLessThanOrEqual(viewport.clientWidth);

  await page.setViewportSize({ width: 1920, height: 1080 });
  await expect(cockpit).toBeVisible();
  const cockpitWidth = await cockpit.evaluate((element) => {
    const measurable = element as unknown as {
      getBoundingClientRect: () => { width: number };
    };
    return measurable.getBoundingClientRect().width;
  });
  expect(
    cockpitWidth,
    "the investigation cockpit must use at least 1600 px at 1920 px",
  ).toBeGreaterThanOrEqual(1600);
  await page.setViewportSize({ width: 1440, height: 900 });
}

async function expectNoClippedContent(
  page: Page,
  rootSelector: string,
): Promise<void> {
  const root = page.locator(rootSelector);
  const originalViewport = page.viewportSize();
  await expect(root).toBeVisible();

  for (const [width, height] of [
    [1280, 800],
    [1440, 900],
  ] as const) {
    await page.setViewportSize({ width, height });
    const measurement = await root.evaluate((panel) => {
      interface ComputedStyleView {
        display: string;
        visibility: string;
        opacity: string;
        position: string;
        clip: string;
        clipPath: string;
        overflowX: string;
        overflowY: string;
      }
      interface MeasurableElement {
        ownerDocument: {
          defaultView: {
            getComputedStyle: (element: MeasurableElement) => ComputedStyleView;
          };
        };
        tagName: string;
        className: unknown;
        textContent: string | null;
        scrollWidth: number;
        clientWidth: number;
        querySelectorAll: (selector: string) => ArrayLike<unknown>;
        getBoundingClientRect: () => {
          right: number;
          width: number;
          height: number;
        };
        getClientRects: () => { length: number };
        closest: (selector: string) => object | null;
      }
      const panelElement = panel as unknown as MeasurableElement;
      const panelRect = panelElement.getBoundingClientRect();
      const descendants = Array.from(
        panelElement.querySelectorAll("*"),
      ) as MeasurableElement[];
      const isHidden = (
        element: MeasurableElement,
        style: ComputedStyleView,
      ) => {
        const rect = element.getBoundingClientRect();
        const outOfFlow =
          style.position === "absolute" || style.position === "fixed";
        const visuallyMasked =
          outOfFlow &&
          ((style.clip !== "auto" && style.clip !== "none") ||
            (style.clipPath !== "none" && style.clipPath !== "") ||
            ((style.overflowX === "hidden" || style.overflowX === "clip") &&
              (style.overflowY === "hidden" || style.overflowY === "clip") &&
              rect.width <= 1 &&
              rect.height <= 1));
        return (
          style.display === "none" ||
          style.visibility === "hidden" ||
          style.visibility === "collapse" ||
          style.opacity === "0" ||
          element.closest('[aria-hidden="true"]') !== null ||
          element.getClientRects().length === 0 ||
          visuallyMasked
        );
      };
      const clipped = descendants.flatMap((element) => {
        const style =
          panelElement.ownerDocument.defaultView.getComputedStyle(element);
        if (
          element.tagName === "TEXTAREA" ||
          (element.tagName === "PRE" &&
            (style.overflowX === "auto" ||
              style.overflowX === "scroll" ||
              style.overflowY === "auto" ||
              style.overflowY === "scroll")) ||
          isHidden(element, style)
        ) {
          return [];
        }
        const rect = element.getBoundingClientRect();
        if (rect.right <= panelRect.right + 1) return [];
        return [
          {
            tag: element.tagName.toLowerCase(),
            className: String(element.className),
            text: (element.textContent ?? "")
              .trim()
              .replace(/[A-Za-z0-9_-]{24,}/g, "[valeur technique]")
              .slice(0, 100),
            overflowPx: Number((rect.right - panelRect.right).toFixed(1)),
          },
        ];
      });
      const overflowingDescendants = descendants.flatMap((element) => {
        const style =
          panelElement.ownerDocument.defaultView.getComputedStyle(element);
        if (
          element.tagName === "TEXTAREA" ||
          (element.tagName === "PRE" &&
            (style.overflowX === "auto" ||
              style.overflowX === "scroll" ||
              style.overflowY === "auto" ||
              style.overflowY === "scroll")) ||
          isHidden(element, style) ||
          element.scrollWidth <= element.clientWidth + 1
        ) {
          return [];
        }
        const rect = element.getBoundingClientRect();
        return [
          {
            tag: element.tagName.toLowerCase(),
            className: String(element.className),
            text: (element.textContent ?? "")
              .trim()
              .replace(/[A-Za-z0-9_-]{24,}/g, "[valeur technique]")
              .slice(0, 100),
            contentOverflowPx: element.scrollWidth - element.clientWidth,
            rightBeyondPanelPx: Number(
              (rect.right - panelRect.right).toFixed(1),
            ),
          },
        ];
      });

      return {
        clipped,
        overflowingDescendants: overflowingDescendants.slice(0, 5),
        scrollWidth: panelElement.scrollWidth,
        clientWidth: panelElement.clientWidth,
      };
    });

    expect(
      measurement.clipped,
      `${rootSelector} has visible descendants clipped past its right edge at ${width}px`,
    ).toEqual([]);
    expect(
      measurement.scrollWidth,
      `${rootSelector} must not scroll horizontally at ${width}px; overflowing descendants=${JSON.stringify(measurement.overflowingDescendants)}`,
    ).toBeLessThanOrEqual(measurement.clientWidth);
  }

  if (originalViewport !== null) {
    await page.setViewportSize(originalViewport);
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

test("parcours atelier M24 étapes 1–32 : workspace, tâches, Notes, PDF, G2", async ({
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
  const contributionApiStatuses: number[] = [];
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
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (url.pathname.endsWith("/contributions")) {
      contributionApiStatuses.push(response.status());
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
  const overview = page.getByRole("region", {
    name: "Vue d’ensemble de l’enquête",
  });
  const overviewMetrics = overview.getByRole("region", {
    name: "Indicateurs de l’enquête",
  });
  for (const label of [
    "Décisions requises",
    "Agents en cours",
    "Tâches actives",
    "Résultats nouveaux",
  ]) {
    await expect(
      overviewMetrics.getByText(label, { exact: true }),
    ).toBeVisible();
  }
  await expect(
    overview.getByRole("heading", { name: "À traiter maintenant" }),
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

  // Step 4 — open the real workspace view and prove its local write actions
  // are available before asking a human to approve external access.
  await page.getByRole("tab", { name: "Workspace et notes" }).click();
  const workspaceView = page.locator(".workspace-notes");
  await expect(workspaceView).toBeVisible();
  await expect(
    workspaceView.getByRole("button", { name: "Nouvelle note" }),
  ).toBeEnabled();
  await expect(
    workspaceView.getByText("Fichier local ≠ preuve canonique"),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Vue d’ensemble" }).click();
  await assertCockpitResponsive(page);

  // Supporting inventory check: the seeds are the five synthetic hashes, two
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
  await expect(
    page.getByText("valeur technique masquée").first(),
  ).toBeVisible();

  // Step 5 — approve the initial G2 envelope (reason + confirmation + pinned
  // proposal fingerprint) through the real human decision route.
  await page.locator('a[href="#overview-g2-details"]').click();
  await page
    .getByLabel(/Motif de la décision/)
    .fill("Approbation humaine G2 du parcours navigateur synthetique");
  await page
    .getByRole("checkbox", { name: /Je confirme approuver l’enveloppe G2/ })
    .check();
  await page.getByRole("button", { name: /Approuver l’enveloppe/ }).click();
  await expect(
    page.getByText("Approbation G2 initiale enregistrée"),
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
    page.getByRole("status").filter({ hasText: "Plan accepté" }),
  ).toBeVisible();
  await expect
    .poll(
      async () =>
        (
          await getJson<TaskPage>(
            page,
            `/api/investigations/${investigation.id}/tasks`,
          )
        ).total,
      { message: "plan acceptance must materialise both tasks" },
    )
    .toBe(2);
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

  // Step 9 — add 30 root tasks through the real human API route. The
  // synthetic data exercises the searchable task list without bypassing its
  // normal task contract or creating nested children.
  const syntheticTaskTitles = Array.from(
    { length: 30 },
    (_unused, index) =>
      `UX-06 — tâche synthétique ${String(index + 1).padStart(2, "0")}`,
  );
  for (const [index, title] of syntheticTaskTitles.entries()) {
    const response = await page.request.post(
      `/api/investigations/${investigation.id}/tasks`,
      {
        headers: { "Idempotency-Key": `m24-e4-ux06-task-${index + 1}` },
        data: {
          task_key: `m24-e4-ux06-${String(index + 1).padStart(2, "0")}`,
          title,
          question: `Question synthétique ${index + 1}`,
          objective: `Objectif synthétique ${index + 1}`,
        },
      },
    );
    expect(
      response.status(),
      `the harness API must create synthetic task ${index + 1}`,
    ).toBe(200);
  }
  await page.reload();
  await page
    .getByRole("button", { name: "Enquête / Avis de l’analyste" })
    .click();
  await expect(
    page.getByRole("region", { name: "Cockpit d’enquête" }),
  ).toBeVisible();
  const taskList = page.getByRole("list", { name: "Liste des tâches" });
  const taskWorkspace = page.getByRole("region", {
    name: "Poste de travail des tâches",
  });
  await expect(taskList.getByRole("button")).toHaveCount(32);
  const allTasks = await getJson<TaskPage>(
    page,
    `/api/investigations/${investigation.id}/tasks`,
  );
  expect(allTasks.total).toBe(32);
  const loader = allTasks.items.find((task) => task.title === LOADER_TITLE);
  if (loader === undefined) throw new Error("the loader task is missing");
  const distantTaskTitle = syntheticTaskTitles.at(-1);
  if (distantTaskTitle === undefined) {
    throw new Error("the UX-06 synthetic task list is empty");
  }
  const distantTask = allTasks.items.find(
    (task) => task.title === distantTaskTitle,
  );
  if (distantTask === undefined) {
    throw new Error("the distant UX-06 task was not created");
  }
  const loaderCard = taskCard(page, LOADER_TITLE);
  await taskList
    .getByRole("button", { name: new RegExp(LOADER_TITLE) })
    .click();
  await expect(
    taskList.getByRole("button", { name: new RegExp(LOADER_TITLE) }),
  ).toHaveAttribute("aria-pressed", "true");
  expect(new URL(page.url()).searchParams.get("task")).toBe(loader.id);
  await capture(page, "plan-task-list-30", taskList);
  await assertCockpitResponsive(page);

  // Step 10 — search selects an old, distant row without scrolling through
  // the cards; refresh restores it, and browser Back restores the prior task.
  const distantTaskButton = taskList.getByRole("button", {
    name: new RegExp(distantTaskTitle),
  });
  await expect(distantTaskButton).not.toBeInViewport();
  const initialScrollY = await page.evaluate(
    () => (globalThis as unknown as { scrollY: number }).scrollY,
  );
  await taskWorkspace
    .getByRole("searchbox", { name: "Recherche" })
    .fill(distantTaskTitle);
  await expect(
    taskWorkspace.getByText("1 tâche(s) affichée(s) sur 32."),
  ).toBeVisible();
  await expect(distantTaskButton).toBeVisible();
  expect(
    await page.evaluate(
      () => (globalThis as unknown as { scrollY: number }).scrollY,
    ),
  ).toBe(initialScrollY);
  await distantTaskButton.click();
  await expect(distantTaskButton).toHaveAttribute("aria-pressed", "true");
  expect(new URL(page.url()).searchParams.get("task")).toBe(distantTask.id);
  await page.reload();
  await page
    .getByRole("button", { name: "Enquête / Avis de l’analyste" })
    .click();
  await expect(
    page.getByRole("region", { name: "Cockpit d’enquête" }),
  ).toBeVisible();
  const restoredTaskList = page.getByRole("list", {
    name: "Liste des tâches",
  });
  await expect(
    restoredTaskList.getByRole("button", {
      name: new RegExp(distantTaskTitle),
    }),
  ).toHaveAttribute("aria-pressed", "true");
  expect(new URL(page.url()).searchParams.get("task")).toBe(distantTask.id);
  await page.goBack();
  await expect
    .poll(() => new URL(page.url()).searchParams.get("task"))
    .toBe(loader.id);
  await expect(
    page
      .getByRole("list", { name: "Liste des tâches" })
      .getByRole("button", { name: new RegExp(`^${LOADER_TITLE},`) }),
  ).toHaveAttribute("aria-pressed", "true");

  // Step 11 — assign the loader task to a Codex operator: this is handoff v1,
  // no agent starts yet.
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

  // Step 12 — the explicit session opening uses the real capability probe and
  // the real sidecar; the fake CLI stays running under the acquired lease.
  await loaderCard.getByRole("tab", { name: "Agent et messages" }).click();
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

  // Step 12 — progress is visible: the operator panel lists the session and
  // its journal-derived summary, and the real POD drain control is reachable.
  await expect(
    page.getByRole("heading", { name: "Contexte agent de la tâche" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Codex CLI — En cours" }),
  ).toBeVisible();
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
  await capture(page, "task-detail-codex", loaderCard);
  const drainResponse = await controlPost(
    request,
    state,
    "/__m24_browser/drain-instructions",
  );
  expect(drainResponse.status()).toBe(200);
  expect((await drainResponse.json()) as { drained: boolean }).toMatchObject({
    drained: true,
  });

  // Step 13 — confirm the fake Codex run has returned successfully. Canonical
  // result submission stays after instruction delivery so the active task
  // lease remains valid for that real transition.
  await expect
    .poll(
      async () =>
        (await firstInvestigation(page)).sessions[0]?.host?.exit?.code,
    )
    .toBe(0);

  // Step 14 — create then edit a non-probative human note in the local
  // workspace. The edit uses the workspace UI's revision-aware write action.
  await page.getByRole("tab", { name: "Workspace et notes" }).click();
  await page.getByRole("button", { name: "Nouvelle note" }).click();
  await page.getByLabel("Chemin de la note").fill(NOTE_PATH);
  await page.getByLabel("Contenu Markdown").fill(NOTE_TEXT);
  await page.getByRole("button", { name: "Enregistrer la note" }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "Note enregistrée" }),
  ).toBeVisible();
  const diskNotePath = join(workspace.workspace_root, NOTE_PATH);
  expect(statSync(diskNotePath).isFile()).toBe(true);
  expect(readFileSync(diskNotePath, "utf-8")).toContain(NOTE_TEXT);
  const initialNoteSha = sha256File(diskNotePath);

  await page.waitForTimeout(1_200);
  await refreshWorkspace(page);
  await expect
    .poll(
      async () =>
        (
          await getJson<WorkspaceFileListApi>(
            page,
            `/api/investigations/${investigation.id}/workspace/files?prefix=notes/humaines`,
          )
        ).files.some((file) => file.path === NOTE_PATH),
      { message: "the synced note appears through the API", timeout: 20_000 },
    )
    .toBe(true);
  const filesAfterNote = await getJson<WorkspaceFileListApi>(
    page,
    `/api/investigations/${investigation.id}/workspace/files?prefix=notes/humaines`,
  );
  expect(filesAfterNote.workspace_available).toBe(true);
  const noteFile = filesAfterNote.files.find((file) => file.path === NOTE_PATH);
  expect(noteFile, "the synced note must be listed").toBeDefined();
  expect(noteFile?.sha256).toBe(initialNoteSha);
  expect(["ui", "filesystem"]).toContain(noteFile?.origin);
  const noteButton = workspaceView.getByRole("button", {
    name: /Note humaine/,
  });
  await expect(noteButton).toBeVisible();
  await noteButton.click();
  const noteFilename = NOTE_PATH.split("/").at(-1) ?? NOTE_PATH;
  await expect(page.getByRole("heading", { name: noteFilename })).toBeVisible();
  await expect(
    page.locator("pre.workspace-notes__content").filter({ hasText: NOTE_TEXT }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Éditer" }).click();
  await page.getByLabel("Contenu Markdown").fill(NOTE_EDITED_TEXT);
  await page.getByRole("button", { name: "Enregistrer", exact: true }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "Note enregistrée" }),
  ).toBeVisible();
  expect(readFileSync(diskNotePath, "utf-8")).toContain(NOTE_EDITED_TEXT);
  const noteSha = sha256File(diskNotePath);
  await page.waitForTimeout(1_200);
  await refreshWorkspace(page);
  const filesAfterEdit = await getJson<WorkspaceFileListApi>(
    page,
    `/api/investigations/${investigation.id}/workspace/files?prefix=notes/humaines`,
  );
  expect(
    filesAfterEdit.files.find((file) => file.path === NOTE_PATH)?.sha256,
  ).toBe(noteSha);
  await noteButton.click();
  await expect(
    page
      .locator("pre.workspace-notes__content")
      .filter({ hasText: NOTE_EDITED_TEXT }),
  ).toBeVisible();
  const noteView = await getJson<WorkspaceNoteViewApi>(
    page,
    `/api/investigations/${investigation.id}/workspace/notes/${NOTE_PATH}/view`,
  );
  expect(noteView.path).toBe(NOTE_PATH);
  expect(noteView.is_evidence).toBe(false);
  expect(noteView.latest.sha256).toBe(noteSha);
  await capture(
    page,
    "workspace-note-open",
    page.getByRole("heading", { name: noteFilename }),
  );

  // Step 15 — send the edited workspace note to Codex through a persistent
  // human instruction. While the fake CLI is busy, the row remains queued.
  await page.getByRole("tab", { name: "Plan et tâches" }).click();
  await loaderCard.getByRole("tab", { name: "Agent et messages" }).click();
  const operatorRow = page
    .locator("li.m24-operator")
    .filter({ hasText: "Codex CLI" })
    .filter({ hasText: LOADER_TITLE });
  await expect(operatorRow).toBeVisible();
  await operatorRow.getByLabel(/Instruction à envoyer/).fill(INSTRUCTION_TEXT);
  await operatorRow
    .getByRole("button", { name: /Envoyer l.instruction/ })
    .click();
  await expect(
    operatorRow.getByText(/Instruction en file persistante/),
  ).toBeVisible();
  await expect(
    operatorRow
      .getByRole("region", { name: "Instructions" })
      .getByText("En file", { exact: true }),
  ).toBeVisible();
  await capture(page, "instruction-en-file", operatorRow);
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

  // Step 16 — effective delivery: the host's clean run end is the receive
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
  await page.reload();
  await page
    .getByRole("button", { name: "Enquête / Avis de l’analyste" })
    .click();
  await expect(
    page.getByRole("region", { name: "Cockpit d’enquête" }),
  ).toBeVisible();
  const deliveredLoaderCard = taskCard(page, LOADER_TITLE);
  await deliveredLoaderCard
    .getByRole("tab", { name: "Agent et messages" })
    .click();
  const deliveredOperatorRow = page
    .locator("li.m24-operator")
    .filter({ hasText: "Codex CLI" })
    .filter({ hasText: LOADER_TITLE });
  await expect(
    deliveredOperatorRow
      .getByRole("region", { name: "Instructions" })
      .getByText("Transmise", { exact: true }),
  ).toBeVisible();
  await capture(page, "instruction-transmise", deliveredOperatorRow);
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

  // The fake Codex session publishes one agent-owned note and imports that
  // exact workspace revision through the signed contribution tool. This is
  // the real D7 broker path: the host derives the task, actor and lease fence.
  const agentNote = await signedSessionAction(request, state, {
    kind: "tool_call",
    session_id: session.id,
    investigation_id: investigation.id,
    tool_name: "note.capture",
    schema_version: "1",
    arguments: {
      target: "note",
      relpath: "m24-e4-contribution.md",
      text: "# Finding de travail synthétique\n\nRésultat local du faux Codex, non probant.",
      expected_sha256: "new",
    },
    idempotency_key: "m24-browser-codex-note-capture",
  });
  expect(agentNote.status_code, JSON.stringify(agentNote.body)).toBe(200);
  const agentNotePayload = agentNote.body as {
    status: string;
    output: { path: string; revision_id: string; evidence: boolean };
  };
  expect(agentNotePayload.status).toBe("captured");
  expect(agentNotePayload.output.path).toBe(
    `notes/agents/${loader.id}/m24-e4-contribution.md`,
  );
  expect(agentNotePayload.output.evidence).toBe(false);
  await page.getByRole("tab", { name: "Workspace et notes" }).click();
  await refreshWorkspace(page);
  const capturedFiles = await getJson<WorkspaceFileListApi>(
    page,
    `/api/investigations/${investigation.id}/workspace/files?prefix=notes/agents/${loader.id}`,
  );
  const capturedRevision = capturedFiles.files.find(
    (file) => file.path === agentNotePayload.output.path,
  );
  expect(capturedRevision?.revision_id).toBe(
    agentNotePayload.output.revision_id,
  );
  await page.getByRole("tab", { name: "Plan et tâches" }).click();
  await expect(taskCard(page, LOADER_TITLE)).toBeVisible();

  const importedAgentNote = await signedSessionAction(request, state, {
    kind: "tool_call",
    session_id: session.id,
    investigation_id: investigation.id,
    tool_name: "contribution.import",
    schema_version: "1",
    arguments: {
      workspace_id: workspace.workspace_id,
      workspace_revision_id: agentNotePayload.output.revision_id,
      category: "work_contribution",
    },
    idempotency_key: "m24-browser-codex-contribution-import",
  });
  expect(
    importedAgentNote.status_code,
    JSON.stringify(importedAgentNote.body),
  ).toBe(200);
  const importedAgentNotePayload = importedAgentNote.body as {
    status: string;
    output: { category: string; import_status: string; task_id: string };
  };
  expect(importedAgentNotePayload.status).toBe("imported");
  expect(importedAgentNotePayload.output.category).toBe("work_contribution");
  expect(importedAgentNotePayload.output.import_status).toBe("imported");
  expect(importedAgentNotePayload.output.task_id).toBe(loader.id);

  // The human sees the journal-derived state after a fresh browser load.
  await page.reload();
  await page
    .getByRole("button", { name: "Enquête / Avis de l’analyste" })
    .click();
  await expect(
    page.getByRole("region", { name: "Cockpit d’enquête" }),
  ).toBeVisible();
  const ackedLoaderCard = taskCard(page, LOADER_TITLE);
  await ackedLoaderCard.getByRole("tab", { name: "Agent et messages" }).click();
  const refreshedOperatorRow = page
    .locator("li.m24-operator")
    .filter({ hasText: "Codex CLI" })
    .filter({ hasText: LOADER_TITLE });
  await expect(
    refreshedOperatorRow
      .getByRole("region", { name: "Instructions" })
      .getByText("Acquittée", { exact: true }),
  ).toBeVisible();
  await capture(page, "instruction-acquittee", refreshedOperatorRow);

  // Complete step 13 — submit and human-review the fake Codex result through
  // the signed D7 route after instruction acknowledgement preserves the lease.
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
  await expect(
    overview.getByRole("heading", { name: "À traiter maintenant" }),
  ).toBeVisible();
  await expect(overview.getByText(LOADER_TITLE)).toBeVisible();
  await expect(
    overview.getByRole("heading", { name: "Inventaire IOC", exact: true }),
  ).toBeVisible();
  await expect(
    overview.getByRole("heading", { name: "Snapshot bootstrap", exact: true }),
  ).toBeVisible();
  await capture(
    page,
    "overview-decision-pending",
    overview.getByRole("heading").first(),
  );
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
  expect(afterAcceptResult.contributions.length).toBeGreaterThan(0);

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
  await page.getByRole("button", { name: new RegExp(INFRA_TITLE) }).click();
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
  await infraCard.getByRole("tab", { name: "Agent et messages" }).click();
  const openClaude = infraCard.getByRole("button", {
    name: "Ouvrir la session Claude Code",
  });
  await expect(openClaude).toBeVisible({ timeout: 30_000 });
  await openClaude.click();
  await expect(
    infraCard.getByText(/session ouverte ; la tâche passe en cours/),
  ).toBeVisible();
  const afterClaudeOpen = await firstInvestigation(page);
  // Step 18 — verify the persisted handoff versions and selected Claude
  // session before reading the resulting contribution in the overview.
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
  await capture(page, "task-detail-claude", infraCard);

  // Step 19 — read the accepted task result and the workspace contribution
  // from a fresh overview projection while the Claude task remains active.
  // The contribution was imported through the signed fake-agent route, so a
  // reload forces the browser query cache to read the persisted row.
  const contributionResponsesBeforeReload = contributionApiStatuses.length;
  await page.reload();
  await page
    .getByRole("button", { name: "Enquête / Avis de l’analyste" })
    .click();
  await expect(
    page.getByRole("region", { name: "Cockpit d’enquête" }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("list", { name: "Liste des tâches" })
      .getByRole("button", { name: new RegExp(`^${INFRA_TITLE},`) }),
  ).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("tab", { name: "Vue d’ensemble" }).click();
  await expect
    .poll(() => contributionApiStatuses.length)
    .toBeGreaterThan(contributionResponsesBeforeReload);
  expect(contributionApiStatuses.at(-1)).toBe(200);
  const activeTaskMetric = overview
    .locator(".workshop-overview-metrics__tile")
    .filter({ hasText: "Tâches actives" });
  const activeTaskCount = Number(
    await activeTaskMetric.locator("dd").textContent(),
  );
  expect(activeTaskCount).toBeGreaterThan(0);
  await expect(
    overview.getByRole("heading", { name: "Résultats nouveaux" }),
  ).toBeVisible();
  const resultProjection = overview.locator(
    '[aria-labelledby="overview-new-results-heading"]',
  );
  const persistedContributions = await getJson<
    Array<{ category: string; origin: string; task_id: string | null }>
  >(page, `/api/investigations/${investigation.id}/contributions`);
  expect(
    persistedContributions.some(
      (contribution) =>
        contribution.category === "work_contribution" &&
        contribution.origin === "agent" &&
        contribution.task_id === loader.id,
    ),
  ).toBe(true);
  await expect(resultProjection).toContainText(
    /Contribution de travail|Observation|Finding/,
  );
  await expect(
    overview.getByRole("heading", { name: "Inventaire IOC", exact: true }),
  ).toBeVisible();
  await expect(
    overview.getByRole("heading", { name: "Snapshot bootstrap", exact: true }),
  ).toBeVisible();
  await capture(page, "overview-active", overview.getByRole("heading").first());
  await page.getByRole("tab", { name: "Plan et tâches" }).click();

  // Step 20 — ask Bridge from the task card (critique). The consultation is
  // consultative: the displayed state goes from sent to received, and no task
  // status, result or decision changes.
  const beforeConsultation = await firstInvestigation(page);
  const infraBefore = beforeConsultation.tasks.find(
    (task) => task.id === infra.id,
  );
  const decisionCountBefore = beforeConsultation.decision_count;
  await infraCard.getByRole("tab", { name: "Agent et messages" }).click();
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

  // Step 21 — open the article reader from the task-scoped Note view.
  // The task card link also keeps the selected task in the cockpit URL.
  // the task-card link (`?vue=opinion&task_id=…&bridge_intent=draft_text`).
  await infraCard
    .getByRole("link", { name: "Rédiger une Note avec Bridge" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Note de l’analyste" }),
  ).toBeVisible();
  const url = new URL(page.url());
  expect(url.searchParams.get("vue")).toBe("opinion");
  expect(url.searchParams.get("task")).toBe(infra.id);
  expect(url.searchParams.get("task_id")).toBe(infra.id);
  expect(url.searchParams.get("bridge_intent")).toBe("draft_text");
  await expect(page.locator("#opinion-note-task")).toHaveValue(infra.id, {
    timeout: 15_000,
  });

  // Step 22 — visually select the canonical article's duplicate paragraphs;
  // the manual anchor field stays folded.
  await expect(page.locator(".article-reading:visible")).toHaveCount(1);
  const articleReader = page.locator(".article-reading:visible");
  await expect(articleReader.getByRole("heading", { level: 3 })).toBeVisible();
  // Read the canonical article and select the two identical
  // paragraphs through the reader. The manual anchor field stays folded.
  const catalog = await getJson<ArticleAnchorCatalogApi>(
    page,
    `/api/investigations/${investigation.id}/article-anchors`,
  );
  expect(catalog.truncated).toBe(false);
  expect(catalog.sections.map((section) => section.heading)).toEqual([
    "Vue d'ensemble",
    "Analyse synthetique",
  ]);
  expect(catalog.anchors.map((item) => item.anchor)).toEqual(
    expect.arrayContaining([NOTE_A_ANCHOR, NOTE_B_ANCHOR]),
  );
  const tableElement = catalog.elements.find(
    (item) => item.kind === "table" && item.key === ARTICLE_TABLE_KEY,
  );
  const figureElement = catalog.elements.find(
    (item) => item.kind === "figure" && item.key === ARTICLE_FIGURE_KEY,
  );
  if (tableElement === undefined || figureElement === undefined) {
    throw new Error(
      "the seeded M23 table and figure must be in the article catalog",
    );
  }
  expect(tableElement.anchor).toBe(NOTE_A_ANCHOR);
  expect(tableElement.proposable).toBe(true);
  expect(figureElement.anchor).toBe(NOTE_B_ANCHOR);
  expect(figureElement.proposable).toBe(true);
  await expect(
    page.locator(".article-reading__element").filter({
      hasText: "M23-D2-ARTICLE-FIGURE-UNIQUE",
    }),
  ).toBeVisible();
  const baseLine = articleReader.getByText(
    new RegExp(
      `Base de publication · v${catalog.base_publication_version} · SHA-256`,
    ),
  );
  await expect(baseLine).toBeVisible();
  expect(catalog.base_publication_sha256).toBe(state.publication_sha256);
  expect(await baseLine.textContent()).toContain(
    catalog.base_publication_sha256.slice(0, 12),
  );
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
  expect(optionLabel(NOTE_A_ANCHOR)).toContain("occurrence 1 sur 2");
  expect(optionLabel(NOTE_B_ANCHOR)).toContain("occurrence 2 sur 2");
  const manualDetails = page.locator(".anchor-picker details", {
    hasText: "Diagnostic — saisie manuelle d’ancre",
  });
  await expect(manualDetails).not.toHaveAttribute("open", "");
  await expect(page.locator("#opinion-note-anchor-manual")).toBeHidden();
  const duplicateParagraphButtons = page
    .locator(".article-reading__select")
    .filter({
      hasText: "TAIL-PARAGRAPH: portee synthetique finale de l'article.",
    });
  await expect(duplicateParagraphButtons).toHaveCount(2);
  await duplicateParagraphButtons.nth(0).click();
  await expect(duplicateParagraphButtons.nth(0)).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(anchorSelect).toHaveValue(NOTE_A_ANCHOR);
  await duplicateParagraphButtons.nth(0).press("ArrowDown");
  await expect(duplicateParagraphButtons.nth(1)).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(anchorSelect).toHaveValue(NOTE_B_ANCHOR);
  await duplicateParagraphButtons.nth(1).press("ArrowUp");
  await expect(duplicateParagraphButtons.nth(0)).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(anchorSelect).toHaveValue(NOTE_A_ANCHOR);
  const firstDuplicateAnchor = catalog.anchors.find(
    (item) => item.anchor === NOTE_A_ANCHOR,
  );
  if (firstDuplicateAnchor === undefined) {
    throw new Error("the first duplicate paragraph anchor is missing");
  }
  await expect(page.locator("#opinion-note-anchor-detail")).toContainText(
    firstDuplicateAnchor.text.slice(0, 60),
  );
  await capture(
    page,
    "article-anchor-selected",
    duplicateParagraphButtons.nth(0),
  );

  // Step 23 — prepare a Bridge draft at occurrence 1 with one canonical
  // observation citation. M23/IOC/code blocks are added after the correction.
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
  const addObservationCitation = async () => {
    const qualificationSummary = page.getByText(
      "Qualification et contenu vérifiable",
      { exact: true },
    );
    const qualificationDetails = qualificationSummary.locator("..");
    if ((await qualificationDetails.getAttribute("open")) === null) {
      await qualificationSummary.click();
    }
    await page
      .getByRole("group", { name: /Observations disponibles/ })
      .getByRole("checkbox", {
        name: /Rapport VT fichier/,
      })
      .first()
      .check();
  };

  // The figure at occurrence 2 is refused when attached to occurrence 1.
  await rejectArticleElementReference(page, investigation.id, {
    anchor: NOTE_A_ANCHOR,
    kind: "figure",
    element_key: ARTICLE_FIGURE_KEY,
    element_sha256: figureElement.element_sha256,
    idempotencyKey: "m24-browser-d2-foreign-anchor",
  });
  await addObservationCitation();
  await expectNoClippedContent(page, ".opinion-workspace__panel");
  await page.getByRole("button", { name: "Demander à Bridge" }).click();
  await waitForProposal(
    page,
    (proposal) =>
      proposal.anchor === NOTE_A_ANCHOR &&
      proposal.status === "proposed" &&
      proposal.base_publication_version === 1 &&
      proposal.proposed_text !== null,
    "the old-base note proposal must be persisted and materialised",
  );
  const oldBaseProposal = (await firstInvestigation(page)).note_proposals.find(
    (proposal) =>
      proposal.anchor === NOTE_A_ANCHOR &&
      proposal.base_publication_version === 1 &&
      proposal.status === "proposed",
  );
  if (oldBaseProposal === undefined) {
    throw new Error("the old-base note proposal was not persisted");
  }
  expect(oldBaseProposal.status).toBe("proposed");
  expect(oldBaseProposal.materialised).toBe(true);
  expect(oldBaseProposal.task_id).toBe(infra.id);
  expect(oldBaseProposal.content_blocks).toHaveLength(0);
  const oldBaseText = oldBaseProposal.proposed_text ?? "";
  expect(oldBaseText.length).toBeGreaterThan(0);

  // Change the current canonical base after drafting. Acceptance of the
  // proposal pinned to publication v1 must fail with the explicit stale-base
  // message; the same journey then drafts against the new current base.
  await page
    .getByRole("checkbox", {
      name: /J’ai relu ce texte et ses références/,
    })
    .check();
  const advancePublication = await controlPost(
    request,
    state,
    "/__m24_browser/advance-publication",
  );
  expect(advancePublication.status()).toBe(200);
  const revisedBase = (await advancePublication.json()) as {
    artifact_id: string;
    version: number;
    sha256: string;
  };
  expect(revisedBase.version).toBe(2);
  const staleBaseResponsePromise = page.waitForResponse((response) =>
    /\/note-proposals\/[^/]+\/accept$/.test(response.url()),
  );
  await page.getByRole("button", { name: "Accepter", exact: true }).click();
  const staleBaseResponse = await staleBaseResponsePromise;
  expect(staleBaseResponse.status()).toBe(409);
  const staleBaseBody = (await staleBaseResponse.json()) as {
    detail?: { code?: string; message?: string };
  };
  expect(staleBaseBody.detail?.code).toBe("stale_base");
  expect(staleBaseBody.detail?.message).toMatch(/publication base changed/i);
  const staleBaseAlert = page.getByRole("alert").filter({
    hasText: "Acceptation refusée",
  });
  await expect(staleBaseAlert).toContainText(
    "La base de publication a changé depuis la proposition",
  );

  await page.reload();
  await page
    .getByRole("button", { name: "Enquête / Avis de l’analyste" })
    .click();
  await page.getByRole("tab", { name: "Note de l’analyste" }).click();
  const revisedCatalog = await getJson<ArticleAnchorCatalogApi>(
    page,
    `/api/investigations/${investigation.id}/article-anchors`,
  );
  expect(revisedCatalog.base_publication_version).toBe(revisedBase.version);
  expect(revisedCatalog.base_publication_artifact_id).toBe(
    revisedBase.artifact_id,
  );
  expect(revisedCatalog.base_publication_sha256).toBe(revisedBase.sha256);
  const revisedTableElement = revisedCatalog.elements.find(
    (item) => item.kind === "table" && item.key === ARTICLE_TABLE_KEY,
  );
  if (revisedTableElement === undefined) {
    throw new Error(
      "the revised publication table is missing from the catalog",
    );
  }
  expect(revisedTableElement.element_sha256).not.toBe(
    tableElement.element_sha256,
  );
  // The old element SHA is stale because the revised base changed its title;
  // rejection happens before the fake Bridge receives a request.
  await rejectArticleElementReference(page, investigation.id, {
    anchor: NOTE_A_ANCHOR,
    kind: "table",
    element_key: ARTICLE_TABLE_KEY,
    element_sha256: tableElement.element_sha256,
    idempotencyKey: "m24-browser-d2-stale-element",
  });
  await expect(duplicateParagraphButtons).toHaveCount(2);
  await duplicateParagraphButtons.nth(0).click();
  await expect(anchorSelect).toHaveValue(NOTE_A_ANCHOR);
  await addObservationCitation();
  await page.getByRole("button", { name: "Demander à Bridge" }).click();
  await waitForProposal(
    page,
    (proposal) =>
      proposal.anchor === NOTE_A_ANCHOR &&
      proposal.proposal_id !== oldBaseProposal.proposal_id &&
      proposal.base_publication_version === revisedBase.version &&
      proposal.status === "proposed" &&
      proposal.proposed_text !== null,
    "the current-base note proposal must be persisted and materialised",
  );
  const firstProposal = (await firstInvestigation(page)).note_proposals.find(
    (proposal) =>
      proposal.anchor === NOTE_A_ANCHOR &&
      proposal.proposal_id !== oldBaseProposal.proposal_id &&
      proposal.base_publication_version === revisedBase.version &&
      proposal.status === "proposed",
  );
  if (firstProposal === undefined) {
    throw new Error("the current-base note proposal was not persisted");
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
  expect(firstProposal.content_blocks).toHaveLength(0);
  const firstDiff = await getJson<ProposalDiffApi>(
    page,
    `/api/investigations/${investigation.id}/note-proposals/${firstProposal.proposal_id}`,
  );
  expect(firstDiff.proposed_text).toBe(firstText);
  expect(firstDiff.accept_ready).toBe(true);
  expect(firstDiff.content_blocks_valid).toBe(true);
  expect(firstDiff.current_note_text).toBeNull();
  expect(firstDiff.resolved_content_blocks).toHaveLength(0);
  const proposalTechnicalDetails = page.locator(
    ".opinion-proposal-review__technical",
  );
  await proposalTechnicalDetails.locator("summary").click();
  expect(await proposalTechnicalDetails.innerText()).toContain(
    firstProposal.proposed_text_sha256 ?? "",
  );
  await page
    .getByRole("checkbox", {
      name: /J’ai relu ce texte et ses références/,
    })
    .check();
  await expectNoClippedContent(page, ".opinion-proposal-review");
  await capture(
    page,
    "bridge-proposal",
    page.locator(".opinion-proposal-review"),
  );
  await page.getByRole("button", { name: "Accepter", exact: true }).click();
  await expect(page.getByText(/Note acceptée/)).toBeVisible();
  const afterFirstAccept = await firstInvestigation(page);
  expect(afterFirstAccept.opinion?.version).toBe(1);
  const acceptedFirstNote = afterFirstAccept.opinion?.observations.find(
    (note) => note.anchor === NOTE_A_ANCHOR,
  );
  expect(acceptedFirstNote?.text).toBe(firstText);
  expect(acceptedFirstNote?.content_blocks).toHaveLength(0);
  await page.getByRole("tab", { name: "Notes 1" }).click();
  await expect(
    page.getByRole("region", { name: "Note existante" }),
  ).toContainText(firstText);

  // Step 24 — correction: a targeted Bridge request replaces the note.
  // The new proposal shows the stored text and the paragraph diff, and it is
  // pinned to the current opinion version.
  await page.getByRole("radio", { name: /Réviser \/ corriger/ }).check();
  await page
    .getByRole("textbox", {
      name: /Consigne de correction pour Bridge/,
    })
    .fill("Corrige la note après revue humaine.");
  await page
    .getByRole("button", { name: "Envoyer la demande de correction" })
    .click();
  await waitForProposal(
    page,
    (proposal) =>
      proposal.anchor === NOTE_A_ANCHOR &&
      proposal.status === "proposed" &&
      proposal.proposal_id !== firstProposal.proposal_id &&
      proposal.proposal_id !== oldBaseProposal.proposal_id &&
      proposal.base_opinion_version === 1,
    "the correction proposal must be persisted",
  );
  const correctionProposal = (await firstInvestigation(page)).note_proposals
    .filter(
      (proposal) =>
        proposal.anchor === NOTE_A_ANCHOR &&
        proposal.status === "proposed" &&
        proposal.proposal_id !== firstProposal.proposal_id &&
        proposal.proposal_id !== oldBaseProposal.proposal_id &&
        proposal.base_opinion_version === 1,
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
  const removedParagraphs = page
    .getByRole("group", { name: "Différences du paragraphe" })
    .locator("del");
  const [firstOldParagraph, secondOldParagraph] = firstText.split("\n\n");
  if (firstOldParagraph === undefined || secondOldParagraph === undefined) {
    throw new Error("the prior Note must retain two paragraphs in the diff");
  }
  await expect(removedParagraphs).toHaveCount(2);
  await expect(removedParagraphs.nth(0)).toContainText(firstOldParagraph);
  await expect(removedParagraphs.nth(1)).toContainText(secondOldParagraph);
  await capture(
    page,
    "bridge-correction-diff",
    page.locator(".opinion-proposal-review"),
  );

  // A distinct note at the second identical paragraph is created while the
  // head is still v1; it becomes stale after the correction is accepted. The
  // reader click selects occurrence 2, and the first note's references stay
  // attached only to occurrence 1.
  await page.getByRole("tab", { name: "Notes 1" }).click();
  await duplicateParagraphButtons.nth(1).click();
  await expect(anchorSelect).toHaveValue(NOTE_B_ANCHOR);
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

  // Step 25 — accept the correction by version/hash (CAS). The earlier
  // second-anchor proposal pinned to v1 is then stale: the UI disables it and
  // the server refuses the same acceptance with 409 stale_note_proposal.
  const proposalList = page.locator(
    'section[aria-labelledby="opinion-proposals-heading"]',
  );
  const correctionListItem = proposalList
    .locator("li")
    .filter({ hasText: "Proposée" })
    .filter({ hasText: NOTE_A_ANCHOR })
    .last();
  await expect(correctionListItem).toHaveCount(1);
  await correctionListItem
    .getByRole("button", { name: "Examiner la proposition" })
    .click();
  await expect(
    page.getByRole("region", { name: "Texte proposé" }),
  ).toContainText(revisedText.split("\n\n")[0] ?? "");
  const correctionTechnicalDetails = page.locator(
    ".opinion-proposal-review__technical",
  );
  await correctionTechnicalDetails.locator("summary").click();
  expect(await correctionTechnicalDetails.innerText()).toContain(
    correctionProposal.proposed_text_sha256 ?? "",
  );
  await page
    .getByRole("checkbox", {
      name: /J’ai relu ce texte et ses références/,
    })
    .check();
  await page.getByRole("button", { name: "Accepter", exact: true }).click();
  await expect(page.getByText(/Note acceptée/)).toBeVisible();
  const afterCorrection = await firstInvestigation(page);
  expect(afterCorrection.opinion?.version).toBe(2);
  const correctedNote = afterCorrection.opinion?.observations.find(
    (note) => note.anchor === NOTE_A_ANCHOR,
  );
  expect(correctedNote?.text).toBe(revisedText);
  expect(correctedNote?.content_blocks).toHaveLength(0);
  const staleCandidateListItem = proposalList
    .locator("li")
    .filter({ hasText: "Rédaction" })
    .last();
  await staleCandidateListItem
    .getByRole("button", { name: "Examiner la proposition" })
    .click();
  await expect(page.getByText(/Proposition périmée/)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Accepter", exact: true }),
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
  // The note list has refreshed to opinion v2; now add M23, IOC and canonical
  // code references at occurrence 2 through their visible selectors.
  await page.getByRole("tab", { name: "Notes 1" }).click();
  await page.getByText("Notes existantes (1)", { exact: true }).click();
  await expect(
    page
      .locator('section[aria-labelledby="opinion-notes-list-heading"]')
      .getByText(revisedText.split("\n\n")[0] ?? ""),
  ).toBeVisible();

  // Step 26 — reference the M23 figure rendered at occurrence 2.
  await duplicateParagraphButtons.nth(1).click();
  await expect(anchorSelect).toHaveValue(NOTE_B_ANCHOR);
  await page
    .getByText("Qualification et contenu vérifiable", { exact: true })
    .click();
  const articleElementPicker = page.getByRole("group", {
    name: "Élément à référencer",
  });
  await expectNoClippedContent(page, ".opinion-workspace__panel");
  await articleElementPicker
    .getByRole("radio", { name: /^Figure · / })
    .first()
    .check();
  await capture(page, "m23-element-selected", articleElementPicker);
  await page.getByRole("button", { name: "Référencer cet élément" }).click();
  await expect(
    page.getByRole("heading", { name: "Références de contenu (1)" }),
  ).toBeVisible();

  // Step 27 — add one IOC table from the canonical observation list.
  await addObservationCitation();
  const iocFieldset = page.getByRole("group", {
    name: "Tableau IOC depuis les observations canoniques",
  });
  await iocFieldset
    .getByRole("checkbox", { name: /Rapport VT fichier/ })
    .first()
    .check();
  await expectNoClippedContent(page, ".opinion-workspace__panel");
  await capture(page, "ioc-table-selected", iocFieldset);
  await iocFieldset
    .getByRole("button", { name: /Ajouter le tableau IOC \(1\)/ })
    .click();
  await expect(
    page.getByRole("heading", { name: "Références de contenu (2)" }),
  ).toBeVisible();

  // Step 28 — select and verify canonical bytes in the visual selector. The
  // manual offset, length and SHA diagnostic remains collapsed and untouched.
  const codeSelector = page.getByRole("group", {
    name: "Extrait de code canonique",
  });
  const observationSourcePicker = codeSelector.getByRole("group", {
    name: "Observation source",
  });
  await observationSourcePicker
    .getByRole("radio", {
      name: new RegExp(
        `Rapport VT fichier · file/${observation.object_id.slice(0, 10)}.*Observation 1`,
      ),
    })
    .check();
  const excerptEditor = codeSelector.getByLabel(/Extrait canonique en texte/);
  await expect(excerptEditor).toBeVisible();
  await excerptEditor.press("Control+A");
  await expect(codeSelector.getByRole("status").last()).toContainText(
    /\d+ octets à partir de/,
  );
  await expect(
    codeSelector.locator("details.canonical-code-selector__manual"),
  ).not.toHaveAttribute("open", "");
  await expectNoClippedContent(page, ".opinion-workspace__panel");
  await capture(page, "canonical-code-selector", codeSelector);
  await codeSelector
    .getByRole("button", { name: "Vérifier et référencer la plage" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Références de contenu (3)" }),
  ).toBeVisible();

  // Request a fresh second-anchor note against opinion v2 and accept it as v3.
  await page.getByRole("radio", { name: "Rédiger" }).check();
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
  expect(
    repinned.content_blocks.map((block) => block["block_type"]).sort(),
  ).toEqual(["article_element", "canonical_code", "ioc_table"]);
  const repinnedDiff = await getJson<ProposalDiffApi>(
    page,
    `/api/investigations/${investigation.id}/note-proposals/${repinned.proposal_id}`,
  );
  expect(repinnedDiff.accept_ready).toBe(true);
  expect(repinnedDiff.content_blocks_valid).toBe(true);
  await expectNoClippedContent(page, ".opinion-proposal-review");
  await capture(
    page,
    "bridge-proposal-with-blocks",
    page.locator(".opinion-proposal-review"),
  );
  await page
    .getByRole("checkbox", {
      name: /J’ai relu ce texte et ses références/,
    })
    .check();
  await page.getByRole("button", { name: "Accepter", exact: true }).click();
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
  expect(
    notes.find((note) => note.anchor === NOTE_A_ANCHOR)?.content_blocks,
  ).toHaveLength(0);
  expect(
    notes.find((note) => note.anchor === NOTE_B_ANCHOR)?.content_blocks,
  ).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        block_type: "article_element",
        kind: "figure",
        element_key: ARTICLE_FIGURE_KEY,
        anchor: NOTE_B_ANCHOR,
      }),
      expect.objectContaining({ block_type: "ioc_table" }),
      expect.objectContaining({ block_type: "canonical_code" }),
    ]),
  );

  // Step 29 — generate and inspect a real Typst PDF. Step 30 verifies placement
  // and provenance with pdftotext, then rasterizes the pages with pdftoppm.
  // The PDF renders both notes at their anchors with
  // a two-paragraph body each, the verified IOC table and its provenance line.
  const opinionRead = await getJson<OpinionReadApi>(
    page,
    `/api/investigations/${investigation.id}/opinion`,
  );
  const draft = opinionRead.draft;
  if (draft === null) throw new Error("the accepted opinion is missing");
  expect(draft.version).toBe(3);
  await page.getByRole("tab", { name: "PDF" }).click();
  await expect(
    page.getByRole("heading", { name: "Aperçu PDF réel de l’avis v3" }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "Ouvrir le PDF" })).toBeVisible();
  await expect(
    page.getByRole("status").filter({
      hasText:
        /Ce navigateur n’a pas affiché le PDF intégré|Le lecteur PDF intégré n’est pas disponible/,
    }),
  ).toBeVisible({ timeout: 15_000 });
  const previewUrl =
    `/api/opinions/${encodeURIComponent(draft.id)}/preview.pdf?` +
    new URLSearchParams({
      version: String(draft.version),
      opinion_sha256: draft.content_sha256,
      base_publication_sha256: draft.base_publication_sha256,
    }).toString();
  const pdfResponse = await page.request.get(previewUrl);
  expect(pdfResponse.status(), await pdfResponse.text()).toBe(200);
  const pdfHeaders = pdfResponse.headers();
  expect(pdfHeaders["content-type"]).toContain("application/pdf");
  expect(pdfHeaders["x-opinion-version"]).toBe(String(draft.version));
  expect(pdfHeaders["x-opinion-content-sha256"]).toBe(draft.content_sha256);
  expect(pdfHeaders["x-base-publication-sha256"]).toBe(
    draft.base_publication_sha256,
  );
  expect(pdfHeaders["x-pdf-sha256"]).toMatch(/^[0-9a-f]{64}$/);
  const pdfBytes = await pdfResponse.body();
  expect(pdfBytes.subarray(0, 5).toString("ascii")).toBe("%PDF-");
  expect(createHash("sha256").update(pdfBytes).digest("hex")).toBe(
    pdfHeaders["x-pdf-sha256"],
  );
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
  writeFileSync(pdfPath, pdfBytes);
  const pdfText = normalizePdfText(pdftotext(pdfPath));
  writeFileSync(testInfo.outputPath("opinion-v3.txt"), `${pdfText}\n`, "utf-8");
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
  // Placement: note A follows duplicate occurrence 1 (and its M23 table),
  // while note B follows occurrence 2 (and the M23 figure).
  const analysisText = normalizePdfText(
    "ANALYSIS-PARAGRAPH: le second etage synthetique est decrit dans ce paragraphe. Revision D2.",
  );
  const tailText = normalizePdfText(
    "TAIL-PARAGRAPH: portee synthetique finale de l'article.",
  );
  const noteAPosition = pdfText.indexOf(tokenA as string);
  const noteBPosition = pdfText.indexOf(tokenB as string);
  const analysisPosition = pdfText.indexOf(analysisText);
  const tailPositions = occurrences(pdfText, tailText);
  const m23TableCellPosition = pdfText.indexOf("M23-D2-TABLE-CELL-UNIQUE");
  const m23FigurePosition = pdfText.indexOf("M23-D2-ARTICLE-FIGURE-UNIQUE");
  expect(noteAPosition).toBeGreaterThan(0);
  expect(noteBPosition).toBeGreaterThan(noteAPosition);
  expect(analysisPosition).toBeGreaterThan(0);
  expect(tailPositions).toHaveLength(2);
  expect(analysisPosition).toBeLessThan(tailPositions[0]);
  expect(tailPositions[0]).toBeLessThan(m23TableCellPosition);
  expect(m23TableCellPosition).toBeLessThan(noteAPosition);
  expect(noteAPosition).toBeLessThan(tailPositions[1]);
  expect(tailPositions[1]).toBeLessThan(m23FigurePosition);
  expect(m23FigurePosition).toBeLessThan(noteBPosition);
  expect(occurrences(pdfText, "M23-D2-TABLE-CELL-UNIQUE")).toHaveLength(1);
  const m23FigureMarkerPositions = occurrences(
    pdfText,
    "M23-D2-ARTICLE-FIGURE-UNIQUE",
  );
  expect(m23FigureMarkerPositions).toHaveLength(2);
  const figureReferencePosition = m23FigureMarkerPositions[1];
  if (figureReferencePosition === undefined) {
    throw new Error("the M23 figure cross-reference is missing from the PDF");
  }
  const figureCrossReference = pdfText.indexOf(
    "Référence d'article — Figure",
    figureReferencePosition,
  );
  expect(figureReferencePosition).toBeGreaterThan(noteBPosition);
  expect(figureCrossReference).toBeGreaterThan(figureReferencePosition);
  // The M23 source table remains at its article placement; note B references
  // the separately rendered figure at the second paragraph.
  expect(occurrences(pdfText, "M23 D2 source table")).toHaveLength(1);
  // Only note B prints the verified IOC table and its canonical provenance;
  // M23's unique element content remains single-instance above.
  const tablePositions = occurrences(pdfText, "Tableau IOC (1)");
  const provenancePositions = occurrences(
    pdfText,
    "Observations canoniques de cette enquête : 1",
  );
  expect(tablePositions).toHaveLength(1);
  expect(provenancePositions).toHaveLength(1);
  expect(tablePositions[0]).toBeGreaterThan(noteBPosition);
  expect(provenancePositions[0]).toBeGreaterThan(tablePositions[0] ?? 0);
  expect(provenancePositions[0]).toBeLessThan(pdfText.length);
  expect(occurrences(pdfText, "Tableau IOC (1)")).toHaveLength(1);
  expect(
    occurrences(pdfText, "Observations canoniques de cette enquête : 1"),
  ).toHaveLength(1);
  expect(pdfText).toContain("empreintes vérifiées");
  await capture(page, "pdf-preview", page.locator(".opinion-pdf-preview"));
  const rasterPrefix = testInfo.outputPath("opinion-v3-page");
  execFileSync("pdftoppm", ["-png", "-r", "100", pdfPath, rasterPrefix], {
    encoding: "utf-8",
  });
  mkdirSync(CAPTURES_DIR, { recursive: true });
  const rasterPages = readdirSync(dirname(rasterPrefix))
    .filter((name) => /^opinion-v3-page-\d+\.png$/.test(name))
    .sort(
      (left, right) =>
        Number(left.match(/-(\d+)\.png$/)?.[1] ?? 0) -
        Number(right.match(/-(\d+)\.png$/)?.[1] ?? 0),
    );
  expect(rasterPages.length).toBeGreaterThan(0);
  for (const rasterPage of rasterPages) {
    const sourcePath = join(dirname(rasterPrefix), rasterPage);
    const png = readFileSync(sourcePath);
    const width = png.readUInt32BE(16);
    const height = png.readUInt32BE(20);
    const pageNumber = rasterPage.match(/-(\d+)\.png$/)?.[1];
    if (pageNumber === undefined) {
      throw new Error(`unexpected Typst PDF raster page name: ${rasterPage}`);
    }
    renameSync(
      sourcePath,
      join(CAPTURES_DIR, `pdf-page-${pageNumber}-${width}x${height}.png`),
    );
  }

  // Step 31 — return to the overview and back to the plan, retaining both the
  // selected view and task in the URL and restoring the same selected row.
  await page.getByRole("tab", { name: "Vue d’ensemble" }).click();
  expect(new URL(page.url()).searchParams.get("vue")).toBe("overview");
  expect(new URL(page.url()).searchParams.get("task")).toBe(infra.id);
  await page.getByRole("tab", { name: "Plan et tâches" }).click();
  await expect(
    page
      .getByRole("list", { name: "Liste des tâches" })
      .getByRole("button", { name: new RegExp(INFRA_TITLE) }),
  ).toHaveAttribute("aria-pressed", "true");
  expect(new URL(page.url()).searchParams.get("vue")).toBe("plan");
  expect(new URL(page.url()).searchParams.get("task")).toBe(infra.id);

  // Step 32 — G2 lifecycle: extend then revoke the approved envelope at the end of the
  // journey, so no later Bridge or provider action depends on the rights. The
  // envelope version advances with every budget-changing invocation, so the
  // CAS expectations are relative to the current version, never absolute.
  await page.getByRole("tab", { name: "Vue d’ensemble" }).click();
  await page.locator('a[href="#overview-g2-details"]').click();
  const envelopeLifecycle = page.getByRole("region", {
    name: "Cycle de vie de l’enveloppe G2",
  });
  await expect(
    envelopeLifecycle.getByRole("heading", {
      name: "Cycle de vie de l’enveloppe G2",
    }),
  ).toBeVisible();
  await expect(
    envelopeLifecycle.getByText("Accès externes : actifs"),
  ).toBeVisible();
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
  await expect(envelopeLifecycle.getByText("Enveloppe étendue")).toBeVisible();
  const extendedEnvelope = await getJson<EnvelopeApiView>(
    page,
    `/api/investigations/${investigation.id}/envelope`,
  );
  expect(extendedEnvelope.version).toBe(extendedVersion);
  expect(extendedEnvelope.currently_authorized).toBe(true);
  await expect(
    envelopeLifecycle.getByText("Accès externes : actifs"),
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
    envelopeLifecycle.getByText("Accès externes : révoqués"),
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
  await capture(
    page,
    "overview-revoked",
    envelopeLifecycle.getByRole("heading").first(),
  );

  // External reads are refused after revocation, while local workspace notes
  // remain readable. The VT batch records a denied plan without calling the
  // fake provider, Shodan's signed broker records a policy denial, and Bridge
  // becomes unavailable without submitting to its fake transport.
  const fixtureStateBeforeRefusals = await diagnostics(page);
  const deniedVtResponse = await page.request.post(
    `/api/investigations/${investigation.id}/bootstrap/run`,
    {
      headers: { "Idempotency-Key": "m24-browser-vt-after-revocation" },
    },
  );
  expect(deniedVtResponse.status()).toBe(200);
  const deniedVt = (await deniedVtResponse.json()) as SnapshotResponse;
  expect(deniedVt.plan_state).toBe("blocked");
  const deniedVtOperations = deniedVt.iocs.flatMap((ioc) => ioc.operations);
  expect(deniedVtOperations.length).toBeGreaterThan(0);
  expect(
    deniedVtOperations.every(
      (operation) => operation.status === "policy_denied",
    ),
  ).toBe(true);
  const fixturesAfterVt = (await diagnostics(page)).fixtures;
  expect(fixturesAfterVt.vt_calls).toEqual(
    fixtureStateBeforeRefusals.fixtures.vt_calls,
  );
  expect(fixturesAfterVt.vt_download_calls).toBe(
    fixtureStateBeforeRefusals.fixtures.vt_download_calls,
  );

  const shodanRefusal = await signedSessionAction(request, state, {
    kind: "tool_call",
    session_id: claudeSession.id,
    investigation_id: investigation.id,
    tool_name: "shodan.search",
    schema_version: "1",
    arguments: {
      query: "port:443",
      justification: "Refus G2 synthetique apres revocation.",
      evidence_refs: [],
      page: 1,
    },
    idempotency_key: "m24-browser-shodan-after-revocation",
  });
  expect(shodanRefusal.status_code).toBe(403);
  const shodanRefusalBody = shodanRefusal.body as {
    detail?: { code?: string };
  };
  expect(shodanRefusalBody.detail?.code).toBe("external_disclosure_denied");
  expect((await diagnostics(page)).fixtures.shodan_calls).toEqual(
    fixtureStateBeforeRefusals.fixtures.shodan_calls,
  );

  const activityAfterRevoke = await getJson<InvestigationActivityApi>(
    page,
    `/api/investigations/${investigation.id}/activity?limit=1`,
  );
  const workingStateVersion = activityAfterRevoke.working_state?.version;
  if (workingStateVersion === undefined) {
    throw new Error(
      "the working-state version is unavailable after revocation",
    );
  }
  const bridgeRefusal = await page.request.post(
    `/api/investigations/${investigation.id}/note-proposals`,
    {
      headers: { "Idempotency-Key": "m24-browser-bridge-after-revocation" },
      data: {
        anchor: NOTE_A_ANCHOR,
        target_block_type: "section",
        bridge_block_type: "key_findings",
        consultation_type: "draft_text",
        question: "Refus G2 synthetique apres revocation.",
        expected_working_state_version: workingStateVersion,
      },
    },
  );
  expect(bridgeRefusal.status()).toBe(200);
  const bridgeRefusalBody = (await bridgeRefusal.json()) as {
    invocation_status: string;
    materialised: boolean;
  };
  expect(bridgeRefusalBody.invocation_status).toBe("unavailable");
  expect(bridgeRefusalBody.materialised).toBe(false);
  const fixturesAfterBridge = (await diagnostics(page)).fixtures;
  expect(fixturesAfterBridge.bridge_submissions).toBe(
    fixtureStateBeforeRefusals.fixtures.bridge_submissions,
  );

  await page.getByRole("tab", { name: "Workspace et notes" }).click();
  await expect(workspaceView).toBeVisible();
  await noteButton.click();
  await expect(
    page
      .locator("pre.workspace-notes__content")
      .filter({ hasText: NOTE_EDITED_TEXT }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Éditer" })).toBeEnabled();

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
