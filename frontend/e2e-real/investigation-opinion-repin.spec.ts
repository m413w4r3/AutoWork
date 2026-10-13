import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import type { APIRequestContext, Page } from "@playwright/test";
import { expect, test } from "@playwright/test";

interface InvestigationBrowserState {
  scenario: "investigation-opinion-repin";
  edition_id: string;
  subjects: { A: string; B: string; C: string };
  database_name: string;
  api_port: number;
  candidate_sha256: string;
  investigation_id: string;
  synthesis_artifact_id: string;
  synthesis_sha256: string;
  base_publication_artifact_id: string;
  base_publication_version: number;
  initial_composition_version: number;
  historic_release: {
    manifest_id: string;
    release_id: string;
    render_id: string;
    pdf_sha256: string;
  };
  browser_result?: Record<string, unknown>;
}

function isBrowserState(value: unknown): value is InvestigationBrowserState {
  if (typeof value !== "object" || value === null) return false;
  const state = value as Record<string, unknown>;
  const subjects = state.subjects;
  const history = state.historic_release;
  return (
    state.scenario === "investigation-opinion-repin" &&
    typeof state.edition_id === "string" &&
    typeof state.database_name === "string" &&
    typeof state.api_port === "number" &&
    typeof state.candidate_sha256 === "string" &&
    typeof state.investigation_id === "string" &&
    typeof state.synthesis_artifact_id === "string" &&
    typeof state.synthesis_sha256 === "string" &&
    typeof state.base_publication_artifact_id === "string" &&
    typeof state.base_publication_version === "number" &&
    typeof state.initial_composition_version === "number" &&
    typeof subjects === "object" &&
    subjects !== null &&
    typeof (subjects as Record<string, unknown>).A === "string" &&
    typeof (subjects as Record<string, unknown>).B === "string" &&
    typeof (subjects as Record<string, unknown>).C === "string" &&
    typeof history === "object" &&
    history !== null &&
    typeof (history as Record<string, unknown>).manifest_id === "string" &&
    typeof (history as Record<string, unknown>).release_id === "string" &&
    typeof (history as Record<string, unknown>).render_id === "string" &&
    typeof (history as Record<string, unknown>).pdf_sha256 === "string"
  );
}

function objectAt(value: unknown, context: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(
      `J2-browser-investigation / ${context}: expected an object`,
    );
  }
  return value as Record<string, unknown>;
}

function textAt(value: unknown, context: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(
      `J2-browser-investigation / ${context}: expected non-empty text`,
    );
  }
  return value;
}

function readState(): { path: string; value: InvestigationBrowserState } {
  const path = process.env.WORKFLOW_BROWSER_STATE;
  if (!path) {
    throw new Error(
      "J2-browser-investigation / state: WORKFLOW_BROWSER_STATE is required",
    );
  }
  const parsed: unknown = JSON.parse(readFileSync(path, "utf-8"));
  if (!isBrowserState(parsed)) {
    throw new Error(
      "J2-browser-investigation / state: invalid browser state JSON",
    );
  }
  return { path, value: parsed };
}

async function readComposition(page: Page, editionId: string) {
  const response = await page.evaluate(async (url: string) => {
    const result = await fetch(url);
    return { status: result.status, body: await result.json() };
  }, `/api/editions/${editionId}/composition`);
  expect(
    response.status,
    "composition verification must use the real read API",
  ).toBe(200);
  const body = objectAt(response.body, "composition read");
  return objectAt(body.composition, "composition payload");
}

function compositionEntry(
  composition: Record<string, unknown>,
  subjectId: string,
): Record<string, unknown> {
  const entries = composition.entries;
  if (!Array.isArray(entries)) {
    throw new Error(
      "J2-browser-investigation / composition entries are missing",
    );
  }
  const entry = entries
    .map((value) => objectAt(value, "composition entry"))
    .find((value) => value.subject_id === subjectId);
  if (!entry) {
    throw new Error(
      `J2-browser-investigation / no composition entry for ${subjectId}`,
    );
  }
  return entry;
}

async function runHarnessJobs(request: APIRequestContext, port: number) {
  const response = await request.post(
    `http://127.0.0.1:${port}/__workflow_browser/run-pending-jobs`,
  );
  const responseBody: unknown = await response.json();
  expect(
    response.status(),
    `JobExecutor response: ${JSON.stringify(responseBody)}`,
  ).toBe(200);
  const result = objectAt(responseBody, "job executor response");
  if (!Array.isArray(result.executed)) {
    throw new Error("J2-browser-investigation / executed job list is missing");
  }
  const jobs = result.executed.map((value) => objectAt(value, "executed job"));
  expect(jobs.length).toBeGreaterThan(0);
  expect(jobs.every((job) => job.status === "succeeded")).toBe(true);
  return {
    jobs,
    fixtures: objectAt(result.fixtures, "scripted fixture counters"),
  };
}

test("enquête A, publication augmentée, repin humain et PDF réel", async ({
  page,
  request,
}) => {
  const { path: statePath, value: state } = readState();
  const { edition_id: editionId, subjects } = state;

  await page.goto(`/subjects/${subjects.A}`);
  await expect(page.getByRole("heading", { name: "Article A" })).toBeVisible();
  await page
    .getByRole("button", { name: "Enquête / Avis de l’analyste" })
    .click();
  const synthesisCard = page.locator(
    'section[aria-labelledby="investigation-target-heading"]',
  );
  await expect(
    synthesisCard.getByRole("heading", {
      name: "Synthèse exacte à investiguer",
    }),
  ).toBeVisible();
  await expect(synthesisCard.getByText("SHA-256 canonique")).toBeVisible();
  const displayedSynthesisHash = (
    await synthesisCard.locator("dl dd").nth(2).textContent()
  )?.trim();
  expect(displayedSynthesisHash).toMatch(/^[0-9a-f]{64}$/);

  const startResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname ===
        `/api/subjects/${subjects.A}/investigations`,
  );
  await page
    .getByRole("button", {
      name: /^(Investiguer cette version exacte|Ouvrir l’enquête sur cette version)$/,
    })
    .click();
  const startResponse = await startResponsePromise;
  expect(startResponse.status()).toBe(200);
  const startBody = objectAt(
    await startResponse.json(),
    "investigation start response",
  );
  const investigation = objectAt(
    startBody.investigation,
    "started investigation",
  );
  expect(investigation.id).toBe(state.investigation_id);
  expect(investigation.synthesis_artifact_id).toBe(state.synthesis_artifact_id);
  expect(investigation.synthesis_sha256).toBe(displayedSynthesisHash);
  expect(investigation.synthesis_sha256).toBe(state.synthesis_sha256);

  const envelope = page.locator("#investigation-envelope-panel");
  await expect(envelope).toContainText("Proposition locale non approuvée");
  await envelope
    .getByRole("button", { name: "Approuver", exact: true })
    .click();
  const providers = envelope.getByRole("group", {
    name: "Fournisseurs autorisés",
  });
  await providers.getByRole("checkbox", { name: "vt" }).check();
  await providers.getByRole("checkbox", { name: "shodan" }).check();
  await envelope
    .getByLabel("Autoriser l’acquisition automatique de hits")
    .check();
  await envelope.getByLabel("Autoriser l’envoi à un LLM externe").check();
  await envelope
    .getByLabel("Raison obligatoire")
    .fill(
      "Approuver les pivots VT et Shodan déterministes ainsi qu’un téléchargement de fixture.",
    );
  await envelope.getByLabel(/Je confirme cette décision G2/).check();
  const g2ResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname ===
        `/api/investigations/${state.investigation_id}/envelope/approve`,
  );
  await envelope
    .getByRole("button", { name: "Confirmer l’approbation" })
    .click();
  const g2Response = await g2ResponsePromise;
  expect(g2Response.status()).toBe(200);
  await expect(envelope).toContainText("Enveloppe approuvée · version 1");
  const g2Body = objectAt(await g2Response.json(), "G2 response");
  expect(g2Body.approved).toBe(true);
  expect(g2Body.version).toBe(1);
  const g2Disclosure = objectAt(g2Body.disclosure, "G2 disclosure");
  expect(g2Disclosure.external_llm_allowed).toBe(true);

  const investigationRun = await runHarnessJobs(request, state.api_port);
  const investigationJobs = investigationRun.jobs;
  const persistedInvestigationResponse = await request.get(
    `http://127.0.0.1:${state.api_port}/api/investigations/${state.investigation_id}`,
  );
  expect(persistedInvestigationResponse.status()).toBe(200);
  const persistedInvestigation = objectAt(
    await persistedInvestigationResponse.json(),
    "persisted investigation after JobExecutor",
  );
  expect(
    investigationRun.fixtures.bridge_submissions,
    `executor=${JSON.stringify(investigationJobs)}; investigation=${JSON.stringify(
      {
        status: persistedInvestigation.status,
        stop_reason: persistedInvestigation.stop_reason,
        findings: Array.isArray(persistedInvestigation.findings)
          ? persistedInvestigation.findings.length
          : null,
      },
    )}`,
  ).toBe(6);
  expect(investigationRun.fixtures.vt_file_reports).toBe(1);
  expect(investigationRun.fixtures.vt_searches).toBe(1);
  expect(investigationRun.fixtures.vt_download_calls).toBe(1);
  expect(investigationRun.fixtures.vt_downloaded_sha256s).toEqual([
    state.candidate_sha256,
  ]);
  expect(investigationRun.fixtures.shodan_calls).toBe(1);
  await page.reload();
  await page
    .getByRole("button", { name: "Enquête / Avis de l’analyste" })
    .click();
  await expect(
    page.getByText("The VT pivot returned the synthetic sample candidate."),
  ).toBeVisible();
  await expect(
    page
      .getByLabel("Constats importants")
      .getByText("Shodan observed indexed services on the article IOC."),
  ).toBeVisible();
  await expect(
    page
      .getByLabel("Nouvelles variantes et échantillons")
      .getByText(state.candidate_sha256, { exact: true }),
  ).toBeVisible();

  const sampleCard = page
    .locator('section[aria-labelledby="investigation-samples-heading"] li')
    .filter({ hasText: state.candidate_sha256 });
  await expect(sampleCard).toBeVisible();
  await expect(sampleCard).toContainText("qualification courante : Candidat");
  const sampleLabel = state.candidate_sha256.slice(0, 12);
  await sampleCard
    .getByLabel(`Décision G3 pour ${sampleLabel}`)
    .selectOption("confirmed");
  const evidenceSelect = sampleCard.getByLabel(`Preuves pour ${sampleLabel}`);
  const evidenceOptions = evidenceSelect.locator("option");
  const evidenceIds: string[] = [];
  for (let index = 0; index < (await evidenceOptions.count()); index += 1) {
    const value = await evidenceOptions.nth(index).getAttribute("value");
    if (value) evidenceIds.push(value);
  }
  expect(evidenceIds.length).toBeGreaterThan(0);
  await evidenceSelect.selectOption(evidenceIds);
  await sampleCard
    .getByLabel(`Raison obligatoire pour ${sampleLabel}`)
    .fill(
      "Le candidat est corroboré par les observations VT et Shodan citées.",
    );
  await sampleCard
    .getByLabel(/Je confirme cette décision de qualification humaine/)
    .check();
  const g3ResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname.includes(
        `/api/investigations/${state.investigation_id}/samples/`,
      ) &&
      new URL(response.url()).pathname.endsWith("/qualify"),
  );
  await sampleCard
    .getByRole("button", { name: "Confirmer", exact: true })
    .click();
  const g3Response = await g3ResponsePromise;
  expect(g3Response.status()).toBe(200);
  await expect(sampleCard).toContainText(
    "Qualification enregistrée avec l’acteur authentifié.",
  );

  const opinionSection = page.locator(
    'section[aria-labelledby="analyst-opinion-heading"]',
  );
  const composeResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname ===
        `/api/investigations/${state.investigation_id}/opinion/compose`,
  );
  await opinionSection
    .getByRole("button", { name: "Composer depuis les constats" })
    .click();
  const composeResponse = await composeResponsePromise;
  expect(composeResponse.status()).toBe(200);
  const composeBody = objectAt(
    await composeResponse.json(),
    "opinion compose response",
  );
  const draft = objectAt(composeBody.draft, "opinion draft");
  const opinionId = textAt(draft.id, "opinion id");
  expect(draft.version).toBe(1);

  const reviewMarker = "Relecture humaine J2 après qualification de S2.";
  const opinionText = opinionSection.getByLabel("Texte du bloc").first();
  const originalOpinionText = await opinionText.inputValue();
  await opinionText.fill(`${originalOpinionText} ${reviewMarker}`);
  const revisionResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === `/api/opinions/${opinionId}/revise`,
  );
  await opinionSection
    .getByRole("button", { name: /Enregistrer la révision \(CAS v1\)/ })
    .click();
  const revisionResponse = await revisionResponsePromise;
  expect(revisionResponse.status()).toBe(200);
  await expect(opinionSection).toContainText("2 · draft");
  await expect(opinionSection).toContainText(reviewMarker);

  await opinionSection
    .getByLabel("Raison de la décision")
    .fill("Approuver la version révisée et ses citations affichées.");
  const approvalResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === `/api/opinions/${opinionId}/approve`,
  );
  await opinionSection
    .getByRole("button", { name: "Approuver cette version précise" })
    .click();
  const approvalResponse = await approvalResponsePromise;
  expect(approvalResponse.status()).toBe(200);
  const approvalBody = objectAt(
    await approvalResponse.json(),
    "opinion approval response",
  );
  const approvedOpinion = objectAt(approvalBody.approved, "approved opinion");
  // Approval appends a new immutable approved version after the reviewed draft v2.
  expect(approvedOpinion.version).toBe(3);
  await expect(opinionSection).toContainText("3 · approved");

  const publishResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === `/api/opinions/${opinionId}/publish`,
  );
  await opinionSection
    .getByRole("button", { name: "Proposer la version augmentée" })
    .click();
  const publishResponse = await publishResponsePromise;
  expect(publishResponse.status()).toBe(200);
  const publishBody = objectAt(
    await publishResponse.json(),
    "opinion publish response",
  );
  const augmentedPublication = objectAt(
    publishBody.publication,
    "augmented publication",
  );
  expect(augmentedPublication.version).toBeGreaterThan(
    state.base_publication_version,
  );
  await expect(
    opinionSection.getByRole("heading", { name: "Version augmentée proposée" }),
  ).toBeVisible();
  await expect(opinionSection).toContainText(
    "Repin à faire dans la composition",
  );

  const augmentedArtifactId = augmentedPublication.artifact_id;
  const augmentedVersion = augmentedPublication.version;
  if (
    typeof augmentedArtifactId !== "string" ||
    typeof augmentedVersion !== "number"
  ) {
    throw new Error(
      "J2-browser-investigation / augmented publication identity is missing",
    );
  }
  const availableArtifactRead = await page.evaluate(async (url: string) => {
    const response = await fetch(url);
    return { status: response.status, body: await response.json() };
  }, `/api/subjects/${subjects.A}/production/artifacts/publication`);
  const availableArtifact = objectAt(
    availableArtifactRead.body,
    "available publication artifact read",
  );
  expect(
    availableArtifactRead.status,
    `the real publication read model must expose augmented V${augmentedVersion}: ${JSON.stringify(availableArtifact)}`,
  ).toBe(200);
  expect(availableArtifact.artifact_id).toBe(augmentedArtifactId);
  expect(availableArtifact.version).toBe(augmentedVersion);
  expect(availableArtifact.status).toBe("verified");

  await opinionSection
    .getByRole("link", {
      name: "Ouvrir la composition pour réépingler l’article",
    })
    .click();

  const initialComposition = await readComposition(page, editionId);
  expect(initialComposition.version).toBe(state.initial_composition_version);
  const initialEntryA = compositionEntry(initialComposition, subjects.A);
  expect(initialEntryA.artifact_id).toBe(state.base_publication_artifact_id);
  expect(initialEntryA.artifact_version).toBe(state.base_publication_version);
  const cardA = page.locator(".composition-console__subject").filter({
    has: page.getByRole("heading", { name: "Article A" }),
  });
  const cardB = page.locator(".composition-console__subject").filter({
    has: page.getByRole("heading", { name: "Article B" }),
  });
  const cardC = page.locator(".composition-console__subject").filter({
    has: page.getByRole("heading", { name: "Article C" }),
  });
  await expect(cardA).toBeVisible();
  await expect(cardB).toBeVisible();
  await expect(cardC).toBeVisible();
  await expect(
    cardA.locator(".composition-console__artifact-status"),
  ).toContainText(`version ${augmentedVersion}`);
  await expect(cardA).toContainText(
    "Le pin reste inchangé jusqu’à votre action explicite.",
  );
  await expect(page.locator(".composition-console__heading")).toContainText(
    `Version ${state.initial_composition_version}`,
  );

  const repinResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "PUT" &&
      new URL(response.url()).pathname ===
        `/api/editions/${editionId}/composition`,
  );
  await cardA
    .getByRole("button", { name: "Épingler la version disponible" })
    .click();
  const repinResponse = await repinResponsePromise;
  expect(repinResponse.status()).toBe(200);
  const repinnedComposition = await readComposition(page, editionId);
  expect(repinnedComposition.version).toBe(
    state.initial_composition_version + 1,
  );
  const repinnedEntryA = compositionEntry(repinnedComposition, subjects.A);
  expect(repinnedEntryA.artifact_id).toBe(augmentedArtifactId);
  expect(repinnedEntryA.artifact_version).toBe(augmentedVersion);

  const validation = page.locator(".composition-console__validation");
  await validation.getByRole("checkbox").check();
  const acceptResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname ===
        `/api/editions/${editionId}/composition/accept`,
  );
  await validation
    .getByRole("button", { name: "Accepter la composition" })
    .click();
  const acceptResponse = await acceptResponsePromise;
  expect(acceptResponse.status()).toBe(202);
  const acceptBody = objectAt(
    await acceptResponse.json(),
    "publication accept response",
  );
  expect(acceptBody.composition_version).toBe(
    state.initial_composition_version + 1,
  );

  const publicationRun = await runHarnessJobs(request, state.api_port);
  const publicationJobs = publicationRun.jobs;
  await page.goto(`/editions/${editionId}/publication`);
  await expect(
    page.getByRole("status").filter({ hasText: "Bulletin publié" }),
  ).toBeVisible({
    timeout: 30_000,
  });
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("link", { name: "Télécharger le PDF" }).click(),
  ]);
  const outputDirectory =
    process.env.WORKFLOW_BROWSER_OUTPUT_DIR ??
    "../var/workflow-results/browser/investigation";
  mkdirSync(outputDirectory, { recursive: true });
  const downloadedPdfPath = join(outputDirectory, "investigation-release.pdf");
  await download.saveAs(downloadedPdfPath);
  const downloadedPdf = readFileSync(downloadedPdfPath);
  expect(downloadedPdf.subarray(0, 5).toString("ascii")).toBe("%PDF-");
  const downloadedPdfSha256 = createHash("sha256")
    .update(downloadedPdf)
    .digest("hex");

  state.browser_result = {
    investigation_job_ids: investigationJobs.map((job) => job.job_id),
    investigation_job_statuses: investigationJobs.map((job) => job.status),
    investigation_fixture_stats: investigationRun.fixtures,
    g2_status: g2Response.status(),
    g2_version: g2Body.version,
    synthesis_sha256_ui: displayedSynthesisHash,
    g3_status: g3Response.status(),
    opinion_id: opinionId,
    opinion_approved_version: approvedOpinion.version,
    opinion_content_sha256: approvedOpinion.content_sha256,
    review_marker: reviewMarker,
    augmented_publication_artifact_id: augmentedArtifactId,
    augmented_publication_version: augmentedVersion,
    composition_before_repin_version: initialComposition.version,
    composition_before_repin_a_artifact_id: initialEntryA.artifact_id,
    composition_after_repin_version: repinnedComposition.version,
    composition_after_repin_a_artifact_id: repinnedEntryA.artifact_id,
    composition_after_repin_a_artifact_version: repinnedEntryA.artifact_version,
    accept_status: acceptResponse.status(),
    manifest_id: acceptBody.manifest_id,
    manifest_sha256: acceptBody.manifest_sha256,
    publication_job_ids: publicationJobs.map((job) => job.job_id),
    publication_job_statuses: publicationJobs.map((job) => job.status),
    downloaded_pdf_path: downloadedPdfPath,
    downloaded_pdf_sha256: downloadedPdfSha256,
  };
  writeFileSync(statePath, `${JSON.stringify(state, null, 2)}\n`, "utf-8");
  test.info().annotations.push({
    type: "investigation-opinion-repin",
    description:
      `G2 v${String(g2Body.version)}; opinion v${String(approvedOpinion.version)}; ` +
      `composition v${String(repinnedComposition.version)}; ` +
      `manifest=${String(acceptBody.manifest_id)}; PDF sha256=${downloadedPdfSha256}`,
  });
  console.log(
    "J2-browser-investigation / real route actions and downloaded PDF SHA-256=" +
      downloadedPdfSha256,
  );
});
