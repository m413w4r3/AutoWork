import { mkdirSync } from "node:fs";
import { join } from "node:path";

import { expect, test, type Page } from "@playwright/test";

import { createWorkshopApi, type WorkshopApi } from "./support/workshopApi";

/**
 * M24 workshop cockpit, desktop only (1440×900, keyboard pass at 1280×800).
 *
 * Every API call is intercepted by `support/workshopApi.ts`; no backend,
 * workspace sidecar or provider is contacted. The five views are opened from
 * the Enquête tab of a subject whose synthesis is verified.
 */

test.describe.configure({ timeout: 45_000 });

const SHOTS = join(process.cwd(), "test-results", "workshop-cockpit");

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ reducedMotion: "reduce" });
});

async function capture(page: Page, name: string) {
  mkdirSync(SHOTS, { recursive: true });
  await page
    .getByRole("tablist", { name: "Vues du cockpit d’enquête" })
    .evaluate((element) => {
      const target = element as unknown as {
        scrollIntoView: (options: { block: "start" }) => void;
      };
      target.scrollIntoView({ block: "start" });
    });
  await page.waitForTimeout(100);
  await page.screenshot({ path: join(SHOTS, `${name}-1440x900.png`) });
}

async function launchWorkshop(page: Page, api: WorkshopApi) {
  await api.install(page);
  await page.goto(`/subjects/${api.ids.subject}`);
  await page
    .getByRole("button", { name: "Enquête / Avis de l’analyste" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Synthèse exacte à investiguer" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Lancer l’atelier d’investigation" })
    .click();
  await expect(
    page.getByRole("tablist", { name: "Vues du cockpit d’enquête" }),
  ).toBeVisible();
}

async function openView(page: Page, name: string) {
  await page.getByRole("tab", { name }).click();
}

test("lance l’atelier M24 et navigue les cinq vues au clavier", async ({
  page,
}) => {
  const api = createWorkshopApi();
  await launchWorkshop(page, api);

  const launch = api.requests.find(
    (request) =>
      request.method === "POST" &&
      request.path === `/api/subjects/${api.ids.subject}/investigations`,
  );
  expect(launch?.body).toEqual({
    production_run_id: api.ids.run,
    synthesis_artifact_id: api.ids.synthesis,
    synthesis_sha256: "a".repeat(64),
    creation_profile: "m24_workshop",
    execution_mode: "operator_led",
  });
  expect(launch?.idempotencyKey).toBeTruthy();

  const tablist = page.getByRole("tablist", {
    name: "Vues du cockpit d’enquête",
  });
  const tabs = tablist.getByRole("tab");
  await expect(tabs).toHaveCount(5);
  for (const label of [
    "Vue d’ensemble",
    "Plan et tâches",
    "Échantillons et résultats",
    "Workspace et notes",
    "Note de l’analyste",
  ]) {
    await expect(tablist.getByRole("tab", { name: label })).toBeVisible();
  }
  for (const tab of await tabs.all()) {
    await expect(tab).toHaveAttribute("aria-controls", "workshop-view-panel");
  }
  await expect(page.getByRole("tabpanel")).toHaveAttribute("tabindex", "0");

  const overviewTab = tablist.getByRole("tab", { name: "Vue d’ensemble" });
  const planTab = tablist.getByRole("tab", { name: "Plan et tâches" });
  const opinionTab = tablist.getByRole("tab", { name: "Note de l’analyste" });
  await overviewTab.focus();
  await expect(overviewTab).toBeFocused();

  await page.keyboard.press("ArrowRight");
  await expect(planTab).toBeFocused();
  await expect(planTab).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tabpanel")).toHaveAttribute(
    "aria-labelledby",
    "workshop-tab-plan",
  );

  await page.keyboard.press("End");
  await expect(opinionTab).toBeFocused();
  await expect(opinionTab).toHaveAttribute("aria-selected", "true");

  await page.setViewportSize({ width: 1280, height: 800 });
  await page.keyboard.press("Home");
  await expect(overviewTab).toBeFocused();
  await page.keyboard.press("ArrowLeft");
  await expect(opinionTab).toBeFocused();
  await expect(page.getByRole("tabpanel")).toHaveAttribute(
    "aria-labelledby",
    "workshop-tab-opinion",
  );
});

test("vue d’ensemble : agent hôte absent, workspace indisponible et G2 bloqué", async ({
  page,
}) => {
  const api = createWorkshopApi({
    agentStatus: "unavailable",
    workspaceState: "unbound",
  });
  await launchWorkshop(page, api);

  await expect(page.getByText("Agent hôte absent")).toBeVisible();
  await expect(
    page.getByText(/Les actions sur le workspace local sont bloquées/),
  ).toBeVisible();
  await expect(
    page.getByText("Workspace local indisponible", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("workspace_local_unavailable", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(
      /L’état du workspace local est indisponible : la décision G2 reste bloquée/,
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Approuver l’enveloppe/ }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Créer le workspace" }),
  ).toHaveCount(0);

  const writes = api.requests.filter(
    (request) =>
      request.method === "POST" &&
      (request.path.endsWith("/workspace/ensure") ||
        request.path.endsWith("/bootstrap/envelope/approve")),
  );
  expect(writes).toEqual([]);
  expect(api.unhandled).toEqual([]);
});

test("vue d’ensemble : création du workspace puis approbation G2 explicite", async ({
  page,
}) => {
  const api = createWorkshopApi({ workspaceState: "unbound" });
  await launchWorkshop(page, api);

  await expect(page.getByText("Workspace local à créer")).toBeVisible();
  await page.getByRole("button", { name: "Créer le workspace" }).click();
  const workspaceRegion = page.getByRole("region", {
    name: "État du workspace",
  });
  await expect(workspaceRegion).toBeVisible();
  await expect(workspaceRegion.getByText("ws-m24-01")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Créer le workspace" }),
  ).toHaveCount(0);

  const ensure = api.requests.find(
    (request) =>
      request.method === "POST" &&
      request.path ===
        `/api/investigations/${api.ids.investigation}/workspace/ensure`,
  );
  expect(ensure).toBeDefined();

  const reason =
    "Approuver l’enveloppe initiale bornée aux IOC cités de l’article.";
  await page.getByLabel(/Motif de la décision/).fill(reason);
  await page.getByLabel(/Je confirme approuver/).check();
  const approve = page.getByRole("button", {
    name: /Approuver l’enveloppe/,
  });
  await expect(approve).toBeEnabled();
  await approve.click();

  await expect(
    page.getByText("Enveloppe G2 approuvée — version 1"),
  ).toBeVisible();
  await expect(
    page.getByText(/Décision f1f1f1f1-f1f1-41f1-81f1-f1f1f1f1f1f1/),
  ).toBeVisible();

  const approval = api.requests.find(
    (request) =>
      request.path ===
      `/api/investigations/${api.ids.investigation}/bootstrap/envelope/approve`,
  );
  expect(approval?.body).toMatchObject({
    reason,
    proposal_sha256: "b".repeat(64),
    correlation_id: expect.stringMatching(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    ),
  });
  expect(approval?.idempotencyKey).toBeTruthy();
  expect(api.unhandled).toEqual([]);

  await capture(page, "01-vue-ensemble");
});

test("plan et tâches : acceptation, action absente et CAS périmé", async ({
  page,
}) => {
  const api = createWorkshopApi();
  await launchWorkshop(page, api);
  await openView(page, "Plan et tâches");

  await expect(
    page.getByRole("heading", { name: "Plan Bridge" }),
  ).toBeVisible();
  await expect(page.getByText("Piste 1 — Analyser le dropper")).toBeVisible();

  await page.getByLabel(/Je confirme l’acceptation de cette version/).check();
  await page
    .getByRole("button", { name: "Accepter le plan et créer les tâches" })
    .click();
  await expect(
    page.getByText(
      /Plan accepté : 2 tâche\(s\) créée\(s\), 0 déjà présente\(s\)/,
    ),
  ).toBeVisible();

  const accept = api.requests.find(
    (request) =>
      request.path ===
      `/api/investigations/${api.ids.investigation}/plans/${api.ids.plan}/accept`,
  );
  expect(accept?.body).toEqual({ expected_sha256: "f".repeat(64) });
  expect(accept?.idempotencyKey).toBeTruthy();

  const readyCard = page
    .getByRole("article")
    .filter({ hasText: "Vérifier le domaine C2" });
  const proposedCard = page
    .getByRole("article")
    .filter({ hasText: "Pivoter sur l’infrastructure" });
  await expect(readyCard).toBeVisible();
  await expect(proposedCard).toBeVisible();

  const proposedActions = page.getByRole("group", {
    name: "Actions pour Pivoter sur l’infrastructure",
  });
  await expect(
    proposedActions.getByRole("button", { name: "Transférer" }),
  ).toHaveCount(0);
  await expect(
    proposedActions.getByRole("button", { name: "Démarrer la tâche" }),
  ).toHaveCount(0);
  await expect(
    proposedActions.getByRole("button", { name: "Annuler la tâche" }),
  ).toBeVisible();

  await readyCard
    .getByRole("button", { name: "Je prends cette tâche" })
    .click();
  await expect(
    page.getByText(
      /La tâche a changé depuis son chargement \(version obsolète\)/,
    ),
  ).toBeVisible();

  const transfer = api.requests.find(
    (request) =>
      request.method === "POST" &&
      request.path.endsWith(`/tasks/${api.ids.taskReady}/transfer`),
  );
  expect(transfer?.body).toMatchObject({ expected_status_version: 1 });
  expect(transfer?.idempotencyKey).toBeTruthy();
  expect(api.unhandled).toEqual([]);

  await capture(page, "02-plan-et-taches");
});

test("échantillons et résultats : inventaire, statuts et candidats jusqu’à G3", async ({
  page,
}) => {
  const api = createWorkshopApi();
  await launchWorkshop(page, api);
  await openView(page, "Échantillons et résultats");

  await expect(
    page.getByRole("heading", { name: "Inventaire IOC" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Seeds — déjà dans l’article" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Découverts — pivots" }),
  ).toBeVisible();
  await expect(
    page.getByRole("table", { name: /Seeds — déjà dans l’article/ }),
  ).toBeVisible();
  await expect(
    page.getByText("Candidat — décision G3 humaine requise").first(),
  ).toBeVisible();
  await expect(page.getByText("En quarantaine").first()).toBeVisible();
  await expect(
    page.getByText(/Téléchargé et haché vérifié/).first(),
  ).toBeVisible();
  await expect(
    page.getByText(
      /une qualification « candidat » reste soumise à une décision G3 humaine/,
    ),
  ).toBeVisible();
  await expect(
    page.getByText(
      /Un nouvel échantillon reste candidat jusqu’à une confirmation G3 humaine/,
    ),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Collecte en attente de l’approbation humaine de l’enveloppe G2.",
      { exact: true },
    ),
  ).toBeVisible();
  expect(api.unhandled).toEqual([]);

  await capture(page, "03-echantillons-et-resultats");
});

test("workspace et notes : arborescence, note non vérifiée et résolution de conflit", async ({
  page,
}) => {
  const api = createWorkshopApi();
  await launchWorkshop(page, api);
  await openView(page, "Workspace et notes");

  await expect(
    page.getByRole("heading", { name: "Arborescence et fichiers" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: /notes\/humaines\/hypothese\.md/ })
    .click();
  await expect(page.getByText("Note de travail — non probante")).toBeVisible();
  await expect(
    page.getByText("Hypothèse humaine — non vérifiée"),
  ).toBeVisible();
  await expect(
    page.getByText(/Un fichier local n’est pas une preuve canonique/),
  ).toBeVisible();

  await expect(
    page.getByRole("heading", { name: "Conflits", exact: true }),
  ).toBeVisible();
  const conflict = page.getByRole("article", {
    name: "Conflit sur notes/humaines/hypothese.md",
  });
  await expect(conflict).toBeVisible();
  await expect(
    conflict.getByText("Version locale conservée.", { exact: true }),
  ).toBeVisible();
  await expect(
    conflict.getByText("Version proposée conservée.", { exact: true }),
  ).toBeVisible();

  await conflict.getByLabel("Résolution").selectOption("keep_proposed");
  await conflict.getByRole("button", { name: "Résoudre le conflit" }).click();
  await expect(page.getByText(/Décision enregistrée/)).toBeVisible();
  await expect(page.getByText("Aucun conflit non résolu.")).toBeVisible();

  const resolution = api.requests.find(
    (request) =>
      request.path ===
      `/api/investigations/${api.ids.investigation}/workspace/conflicts/${api.ids.conflict}/resolve`,
  );
  expect(resolution?.body).toMatchObject({
    choice: "keep_proposed",
    expected_current_sha256: "3".repeat(64),
    expected_proposed_sha256: "4".repeat(64),
  });
  expect(resolution?.idempotencyKey).toBeTruthy();
  expect(api.unhandled).toEqual([]);

  await capture(page, "04-workspace-et-notes");
});

test("note de l’analyste : deux ancres, proposition Bridge acceptée par hash", async ({
  page,
}) => {
  const api = createWorkshopApi();
  await launchWorkshop(page, api);
  await openView(page, "Note de l’analyste");

  const notesRegion = page.getByRole("region", { name: "Notes par ancre" });
  await expect(notesRegion).toBeVisible();
  await expect(
    notesRegion.getByRole("heading", { name: /Ancre lead:0001 · 1 note/ }),
  ).toBeVisible();
  await expect(
    notesRegion.getByRole("heading", {
      name: /Ancre section:2:paragraph:0007 · 1 note/,
    }),
  ).toBeVisible();
  await expect(page.getByText("Rédigée par Bridge").first()).toBeVisible();
  await expect(
    page.getByText(
      /Aucun champ ne permet de saisir du texte éditorial de l’avis/,
    ),
  ).toBeVisible();

  const requestButton = page.getByRole("button", {
    name: "Demander à Bridge",
    exact: true,
  });
  await expect(requestButton).toBeEnabled();
  await requestButton.click();

  const request = api.requests.find(
    (item) =>
      item.method === "POST" &&
      item.path ===
        `/api/investigations/${api.ids.investigation}/note-proposals`,
  );
  expect(request?.body).toMatchObject({
    anchor: "lead:0001",
    target_block_type: "lead",
    bridge_block_type: "key_findings",
    consultation_type: "draft_text",
    expected_working_state_version: 5,
    base_opinion_version: 3,
    base_opinion_sha256: "a".repeat(64),
  });
  expect(request?.idempotencyKey).toBeTruthy();

  await expect(
    page.getByRole("heading", {
      name: /Proposition ciblée · ancre lead:0001/,
    }),
  ).toBeVisible();
  await expect(
    page.getByText("Texte Bridge proposé pour le chapô.").first(),
  ).toBeVisible();
  const editableValues = await page
    .locator("textarea, input")
    .evaluateAll((elements) =>
      elements.map((element) => {
        const value = (element as unknown as { value?: unknown }).value;
        return typeof value === "string" ? value : "";
      }),
    );
  expect(editableValues).not.toContain("Texte Bridge proposé pour le chapô.");

  await page.getByLabel(/J’ai examiné le texte stocké/).check();
  const accept = page.getByRole("button", {
    name: /Accepter la proposition v4 · SHA-256 222222222222/,
  });
  await expect(accept).toBeEnabled();
  await accept.click();

  await expect(
    page.getByRole("heading", { name: "Note acceptée" }),
  ).toBeVisible();
  await expect(page.getByText(/Avis v4 · SHA-256 555555555555/)).toBeVisible();
  const acceptance = api.requests.find(
    (item) =>
      item.method === "POST" &&
      item.path ===
        `/api/investigations/${api.ids.investigation}/note-proposals/${api.ids.proposal}/accept`,
  );
  expect(acceptance?.body).toMatchObject({
    expected_status_version: 4,
    expected_base_version: 3,
    expected_base_sha256: "a".repeat(64),
  });
  expect(acceptance?.idempotencyKey).toBeTruthy();
  expect(api.unhandled).toEqual([]);

  await capture(page, "05-note-de-lanalyste");
});

test("note de l’analyste : une proposition périmée n’est pas acceptable", async ({
  page,
}) => {
  const api = createWorkshopApi({ staleProposal: true });
  await launchWorkshop(page, api);
  await openView(page, "Note de l’analyste");

  const proposalsRegion = page.getByRole("region", {
    name: "Propositions Bridge",
  });
  await expect(
    proposalsRegion
      .getByRole("listitem")
      .filter({ hasText: "lead:0001" })
      .getByText("Périmée", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Examiner la proposition" }).click();

  await expect(page.getByText(/Proposition périmée/)).toBeVisible();
  await expect(
    page.getByText(/Son texte ne peut plus être accepté/),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Accepter la proposition v5/ }),
  ).toBeDisabled();
  expect(api.requests.filter((item) => item.path.endsWith("/accept"))).toEqual(
    [],
  );
});
