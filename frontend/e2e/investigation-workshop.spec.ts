import { mkdirSync } from "node:fs";
import { join } from "node:path";

import { expect, test, type Page } from "@playwright/test";

import {
  createWorkshopApi,
  ids as workshopIds,
  type WorkshopApi,
} from "./support/workshopApi";

/**
 * M24 workshop cockpit, desktop only (1440×900, keyboard pass at 1280×800).
 *
 * Every API call is intercepted by `support/workshopApi.ts`; no backend,
 * workspace sidecar or provider is contacted. The five views are opened from
 * the Enquête tab of a subject whose synthesis is verified.
 */

test.describe.configure({ timeout: 45_000 });

const SHOTS = join(process.cwd(), "test-results", "workshop-cockpit");

/** The node tsconfig has no DOM lib; element evaluates use this local shape. */
interface Box {
  width: number;
  height: number;
  top: number;
  left: number;
}

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

  const lifecycle = page.getByRole("region", {
    name: "Cycle de vie de l’enveloppe G2",
  });
  await expect(lifecycle).toBeVisible();
  await expect(
    lifecycle.getByRole("button", { name: "Étendre l’enveloppe" }),
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

test("vue d’ensemble : extension puis révocation G2 de l’enveloppe approuvée", async ({
  page,
}) => {
  const api = createWorkshopApi({
    existingInvestigation: true,
    envelopeApproved: true,
  });
  await api.install(page);
  await page.goto(`/subjects/${api.ids.subject}`);
  await page
    .getByRole("button", { name: "Enquête / Avis de l’analyste" })
    .click();
  await page
    .getByRole("button", { name: "Ouvrir l’enquête sur cette version" })
    .click();

  const lifecycle = page.getByRole("region", {
    name: "Cycle de vie de l’enveloppe G2",
  });
  await expect(lifecycle).toBeVisible();
  await expect(
    lifecycle.getByText("Enveloppe G2 active — version 1"),
  ).toBeVisible();
  await expect(
    lifecycle.getByRole("button", { name: /Approuver/ }),
  ).toHaveCount(0);

  const extendReason = "Budget VT supplémentaire pour la piste 2.";
  await lifecycle.getByRole("button", { name: "Étendre l’enveloppe" }).click();
  await lifecycle.getByLabel("Motif de l’extension").fill(extendReason);
  await lifecycle.getByLabel(/Je confirme cette extension/).check();
  await lifecycle
    .getByRole("button", { name: "Confirmer l’extension" })
    .click();
  await expect(
    lifecycle.getByText("Enveloppe étendue — version 2"),
  ).toBeVisible();

  const extension = api.requests.find(
    (request) =>
      request.method === "POST" &&
      request.path ===
        `/api/investigations/${api.ids.investigation}/envelope/extend`,
  );
  expect(extension?.body).toMatchObject({
    expected_version: 1,
    reason: extendReason,
  });
  expect(extension?.idempotencyKey).toBeTruthy();

  const revokeTrigger = lifecycle.getByRole("button", {
    name: "Révoquer l’enveloppe",
  });
  await revokeTrigger.click();
  const dialog = page.getByRole("dialog", {
    name: "Révoquer l’enveloppe G2 ?",
  });
  await expect(dialog).toBeVisible();
  await expect(
    dialog.getByLabel("Motif obligatoire de la révocation"),
  ).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(revokeTrigger).toBeFocused();

  const revokeReason = "Les recherches externes sont terminées.";
  await revokeTrigger.click();
  await dialog
    .getByLabel("Motif obligatoire de la révocation")
    .fill(revokeReason);
  await dialog.getByLabel(/Je confirme la révocation de l’enveloppe/).check();
  await dialog.getByRole("button", { name: "Confirmer la révocation" }).click();

  await expect(
    lifecycle.getByText("Enveloppe révoquée — droits externes coupés"),
  ).toBeVisible();
  await expect(
    lifecycle.getByRole("button", { name: /Approuver/ }),
  ).toHaveCount(0);
  await expect(
    lifecycle.getByRole("button", { name: "Étendre l’enveloppe" }),
  ).toHaveCount(0);
  await expect(
    lifecycle.getByRole("button", { name: "Révoquer l’enveloppe" }),
  ).toHaveCount(0);

  const revocation = api.requests.find(
    (request) =>
      request.method === "POST" &&
      request.path ===
        `/api/investigations/${api.ids.investigation}/envelope/revoke`,
  );
  expect(revocation?.body).toMatchObject({
    expected_version: 2,
    reason: revokeReason,
  });
  expect(revocation?.idempotencyKey).toBeTruthy();
  expect(api.unhandled).toEqual([]);
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

  const timeline = page.getByRole("region", { name: "Chronologie commune" });
  const entries = [
    [
      "Passation v1 · Humain → Opérateur",
      "Passation",
      "handoff:v1:human->operator",
    ],
    ["Finding importé", "Contribution", "contribution:finding:imported"],
    [
      "Session Codex CLI · En cours",
      "Session d’agent",
      "agent_session:codex_cli:running",
    ],
    [
      "Instruction humaine · En file (agent occupé)",
      "Instruction humaine",
      "instruction:queued",
    ],
  ] as const;
  for (const [sentence, label, raw] of entries) {
    const entry = timeline.getByRole("listitem").filter({ hasText: sentence });
    await expect(entry.getByText(label, { exact: true })).toBeVisible();
    // The raw projection token stays readable as the item title.
    await expect(entry.getByTitle(raw)).toHaveText(sentence);
  }
  await expect(
    timeline.getByText("Non probant — élément de travail"),
  ).toHaveCount(4);
  await expect(timeline.getByText("Preuve canonique")).toHaveCount(1);

  // Technical refs carry a French label; persisted ids stay whole in the
  // title while the visible value is abbreviated.
  const decisionEntry = timeline
    .getByRole("listitem")
    .filter({ hasText: "Approbation d’un pivot" });
  await expect(decisionEntry.getByText("Type de cible")).toBeVisible();
  await expect(decisionEntry.getByText("pivot", { exact: true })).toBeVisible();
  await expect(decisionEntry.getByTitle(workshopIds.sample)).toHaveText(
    `${workshopIds.sample.slice(0, 12)}…`,
  );

  // An operator actor id is shown with the adapter label, not `operator:...`.
  const toolEntry = timeline
    .getByRole("listitem")
    .filter({ hasText: "Téléchargement d’un échantillon VT" });
  await expect(toolEntry.getByText("Opérateur · Codex CLI")).toBeVisible();
  await expect(toolEntry.getByText("Clé d’opération")).toBeVisible();
  await expect(toolEntry.getByText("operator:digest:payload")).toBeVisible();

  await page.getByLabel(/Type d’événement/).selectOption("instruction");
  await expect(
    timeline.getByText("Instruction humaine · En file (agent occupé)"),
  ).toBeVisible();
  await expect(
    timeline.getByText("Passation v1 · Humain → Opérateur"),
  ).toHaveCount(0);
  await page.getByLabel(/Type d’événement/).selectOption("all");

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

test("demander à Bridge depuis une tâche : avis consultatif, registre et aucune mutation de tâche", async ({
  page,
}) => {
  const api = createWorkshopApi();
  await launchWorkshop(page, api);
  await openView(page, "Plan et tâches");

  await page.getByLabel(/Je confirme l’acceptation de cette version/).check();
  await page
    .getByRole("button", { name: "Accepter le plan et créer les tâches" })
    .click();
  await expect(
    page.getByText(/Plan accepté : 2 tâche\(s\) créée\(s\)/),
  ).toBeVisible();

  const card = page
    .getByRole("article")
    .filter({ hasText: "Vérifier le domaine C2" });
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: "Demander à Bridge" }).click();
  await expect(
    card.getByText(/ni résultat de tâche, ni preuve, ni décision/),
  ).toBeVisible();

  await card.getByLabel("Type de demande").selectOption("critique");
  await card
    .getByLabel("Question pour Bridge")
    .fill("Le domaine est-il bien un C2 ?");
  await card
    .getByRole("button", { name: "Envoyer la demande à Bridge" })
    .click();

  await expect(card.getByText(/Demande : Reçue — Parsée/)).toBeVisible();
  const ledgerRow = card
    .locator(".m24-consultations li")
    .filter({ hasText: "Critique technique" });
  await expect(ledgerRow).toHaveCount(1);
  await expect(ledgerRow).toContainText("Reçue");

  const consultation = api.requests.find(
    (request) =>
      request.method === "POST" &&
      request.path ===
        `/api/investigations/${api.ids.investigation}/tasks/${api.ids.taskReady}/consultations`,
  );
  expect(consultation?.body).toMatchObject({
    consultation_type: "critique",
    question: "Le domaine est-il bien un C2 ?",
    include_previous_consultation: false,
  });
  expect(consultation?.idempotencyKey).toBeTruthy();
  expect(
    api.requests.filter(
      (request) =>
        request.method === "POST" && request.path.endsWith("/commands"),
    ),
  ).toEqual([]);
  expect(api.unhandled).toEqual([]);

  await capture(page, "05-demander-a-bridge-tache");
});

test("ouvre une enquête existante sans relancer un start legacy", async ({
  page,
}) => {
  const api = createWorkshopApi({ existingInvestigation: true });
  await api.install(page);
  await page.goto(`/subjects/${api.ids.subject}`);
  await page
    .getByRole("button", { name: "Enquête / Avis de l’analyste" })
    .click();

  const open = page.getByRole("button", {
    name: "Ouvrir l’enquête sur cette version",
  });
  await expect(open).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Lancer l’atelier d’investigation" }),
  ).toBeDisabled();
  await open.click();

  await expect(
    page.getByRole("tablist", { name: "Vues du cockpit d’enquête" }),
  ).toBeVisible();
  expect(
    api.requests.filter(
      (request) =>
        request.method === "POST" &&
        request.path === `/api/subjects/${api.ids.subject}/investigations`,
    ),
  ).toEqual([]);
  expect(api.unhandled).toEqual([]);
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

test("échantillons : qualification G3 motivée avec observation de l’enquête", async ({
  page,
}) => {
  const api = createWorkshopApi();
  await launchWorkshop(page, api);
  await openView(page, "Échantillons et résultats");

  await expect(
    page.getByRole("heading", { name: "Qualification G3 des échantillons" }),
  ).toBeVisible();
  await page.getByLabel(/Décision G3 pour/).selectOption("confirmed");
  await page
    .getByLabel(/Motif de la décision/)
    .fill("Le binaire est confirmé par l’observation de l’enquête.");
  await page
    .getByLabel(/Observations de la même enquête/)
    .selectOption(api.ids.observation);
  await page.getByLabel(/Je confirme cette décision G3/).check();
  const submit = page.getByRole("button", {
    name: "Enregistrer la décision G3",
  });
  await expect(submit).toBeEnabled();
  await submit.click();

  await expect(page.getByText(/Décision G3 enregistrée/)).toBeVisible();
  const qualify = api.requests.find(
    (request) =>
      request.method === "POST" &&
      request.path ===
        `/api/investigations/${api.ids.investigation}/samples/${api.ids.association}/qualify`,
  );
  expect(qualify?.body).toMatchObject({
    expected_version: 1,
    qualification: "confirmed",
    reason: "Le binaire est confirmé par l’observation de l’enquête.",
    evidence_refs: [api.ids.observation],
    correlation_id: expect.stringMatching(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    ),
  });
  expect(qualify?.idempotencyKey).toBeTruthy();
  expect(api.unhandled).toEqual([]);
});

test("attribution : passation honnête, ouverture masquée sans agent hôte", async ({
  page,
}) => {
  const api = createWorkshopApi({
    transferOutcome: "success",
    capabilitiesState: "host_unavailable",
  });
  await launchWorkshop(page, api);
  await openView(page, "Plan et tâches");
  await page.getByLabel(/Je confirme l’acceptation de cette version/).check();
  await page
    .getByRole("button", { name: "Accepter le plan et créer les tâches" })
    .click();

  const readyCard = page
    .getByRole("article")
    .filter({ hasText: "Vérifier le domaine C2" });
  await expect(readyCard).toBeVisible();
  await expect(
    readyCard.getByText(
      /Les attributions et transferts créent une passation versionnée/,
    ),
  ).toBeVisible();
  await readyCard.getByRole("button", { name: "Attribuer à Codex" }).click();

  await expect(
    page.getByText(
      /Passation v1 créée pour Codex CLI — aucune session ouverte pour l’instant/,
    ),
  ).toBeVisible();
  await expect(
    readyCard.getByRole("button", { name: "Ouvrir la session Codex CLI" }),
  ).toHaveCount(0);
  await expect(
    readyCard.getByText(/Ouverture de session masquée : Agent hôte absent/),
  ).toBeVisible();
  expect(
    api.requests.filter(
      (request) =>
        request.method === "POST" &&
        request.path ===
          `/api/investigations/${api.ids.investigation}/agent-sessions`,
    ),
  ).toEqual([]);

  const transfer = api.requests.find(
    (request) =>
      request.method === "POST" &&
      request.path.endsWith(`/tasks/${api.ids.taskReady}/transfer`),
  );
  expect(transfer?.body).toMatchObject({
    to_actor_kind: "operator",
    to_adapter_kind: "codex_cli",
    open_session: false,
    expected_status_version: 1,
  });
  expect(transfer?.idempotencyKey).toBeTruthy();

  await openView(page, "Workspace et notes");
  await page
    .getByRole("button", { name: /notes\/humaines\/hypothese\.md/ })
    .click();
  await expect(
    page.getByLabel("Lier à une tâche").locator("option", {
      hasText: "lead-a · Vérifier le domaine C2",
    }),
  ).toHaveCount(1);
  expect(api.unhandled).toEqual([]);
});

test("attribution puis ouverture explicite de la session Codex", async ({
  page,
}) => {
  const api = createWorkshopApi({ transferOutcome: "success" });
  await launchWorkshop(page, api);
  await openView(page, "Plan et tâches");
  await page.getByLabel(/Je confirme l’acceptation de cette version/).check();
  await page
    .getByRole("button", { name: "Accepter le plan et créer les tâches" })
    .click();

  const readyCard = page
    .getByRole("article")
    .filter({ hasText: "Vérifier le domaine C2" });
  await readyCard.getByRole("button", { name: "Attribuer à Codex" }).click();

  const openButton = readyCard.getByRole("button", {
    name: "Ouvrir la session Codex CLI",
  });
  await expect(openButton).toBeVisible();
  await openButton.click();

  await expect(
    readyCard.getByText(/session ouverte ; la tâche passe en cours/),
  ).toBeVisible();
  const open = api.requests.find(
    (request) =>
      request.method === "POST" &&
      request.path ===
        `/api/investigations/${api.ids.investigation}/agent-sessions`,
  );
  expect(open?.body).toEqual({
    task_id: api.ids.taskReady,
    adapter_kind: "codex_cli",
    handoff_id: api.ids.decision,
    executable: "codex",
    model: null,
  });
  expect(open?.idempotencyKey).toBeTruthy();

  await expect(
    page.getByText("Codex CLI · Vérifier le domaine C2"),
  ).toBeVisible();
  await expect(page.getByText("En cours").first()).toBeVisible();
  await expect(page.getByText("Aucun opérateur actif.")).toHaveCount(0);
  expect(api.unhandled).toEqual([]);
});

test("ouverture de session refusée par la divulgation externe", async ({
  page,
}) => {
  const api = createWorkshopApi({
    transferOutcome: "success",
    openSessionOutcome: "external_llm_refused",
  });
  await launchWorkshop(page, api);
  await openView(page, "Plan et tâches");
  await page.getByLabel(/Je confirme l’acceptation de cette version/).check();
  await page
    .getByRole("button", { name: "Accepter le plan et créer les tâches" })
    .click();

  const readyCard = page
    .getByRole("article")
    .filter({ hasText: "Vérifier le domaine C2" });
  await readyCard.getByRole("button", { name: "Attribuer à Codex" }).click();
  await readyCard
    .getByRole("button", { name: "Ouvrir la session Codex CLI" })
    .click();

  await expect(readyCard.getByRole("alert")).toContainText(
    /Divulgation externe refusée/,
  );
  await expect(page.getByText("Aucun opérateur actif.")).toBeVisible();
  expect(
    api.requests.filter(
      (request) =>
        request.method === "POST" &&
        request.path ===
          `/api/investigations/${api.ids.investigation}/agent-sessions`,
    ),
  ).toHaveLength(1);
  expect(api.unhandled).toEqual([]);
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

  // M6 : aucune session active dans ce scénario, l’instruction agent est
  // proposée avec une raison explicite et reste désactivée.
  await expect(
    page.getByText(
      /Action indisponible : aucune session active pour cette tâche/,
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Demander à l’agent de continuer" }),
  ).toBeDisabled();

  // M1 : la relecture Bridge utilise la version d’état de travail lue.
  await page
    .getByLabel("Question à Bridge sur cette note")
    .fill("La note est-elle cohérente avec l’observation ?");
  await page
    .getByRole("button", { name: "Demander à Bridge de relire" })
    .click();
  await expect(page.getByText(/Relecture Bridge demandée/)).toBeVisible();
  const bridge = api.requests.find(
    (request) =>
      request.method === "POST" &&
      request.path ===
        `/api/investigations/${api.ids.investigation}/workspace/notes/revision-2/ask-bridge`,
  );
  expect(bridge?.body).toMatchObject({
    consultation_type: "interpretation",
    question: "La note est-elle cohérente avec l’observation ?",
    expected_working_state_version: 5,
  });
  expect(bridge?.idempotencyKey).toBeTruthy();
  await expect(page.getByText(/avis consultatif — non preuve/)).toBeVisible();

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

test("note de l’analyste : blocs de contenu référencés puis vérifiés", async ({
  page,
}) => {
  const api = createWorkshopApi();
  await launchWorkshop(page, api);
  await openView(page, "Note de l’analyste");

  const requestButton = page.getByRole("button", {
    name: "Demander à Bridge",
    exact: true,
  });
  await expect(requestButton).toBeEnabled();

  const elementSelect = page.getByLabel("Élément à référencer");
  await expect(
    elementSelect.getByRole("option", {
      name: /Volume des commandes.*non proposable : graphique sans preuve/,
    }),
  ).toBeDisabled();
  await expect(
    page.getByText(/Un élément M23 de cette ancre est grisé/),
  ).toBeVisible();
  await elementSelect.selectOption("table:table-commands");
  await page.getByRole("button", { name: "Référencer cet élément" }).click();

  const ioc = page.getByRole("group", {
    name: /Tableau IOC depuis les observations canoniques/,
  });
  await ioc.getByRole("checkbox", { name: /sample-1/ }).check();
  await ioc.getByRole("button", { name: "Ajouter le tableau IOC (1)" }).click();

  const blocksRegion = page.getByRole("region", {
    name: /Références de contenu \(2\)/,
  });
  await expect(blocksRegion.getByText(/Élément d’article/)).toBeVisible();
  await expect(blocksRegion.getByText(/Tableau IOC \(1\)/)).toBeVisible();

  await requestButton.click();

  const request = api.requests.find(
    (item) =>
      item.method === "POST" &&
      item.path ===
        `/api/investigations/${api.ids.investigation}/note-proposals`,
  );
  expect(request?.body).toMatchObject({
    anchor: "lead:0001",
    content_blocks: [
      {
        block_type: "article_element",
        kind: "table",
        element_key: "table-commands",
        anchor: "lead:0001",
      },
      {
        block_type: "ioc_table",
        observation_ids: [api.ids.observation],
        title: "Tableau IOC (1)",
      },
    ],
  });

  const resolved = page.getByRole("region", {
    name: "Aperçu lecture seule des blocs vérifiés",
  });
  await expect(resolved.getByText(/Commandes observées/)).toBeVisible();
  await expect(
    resolved.getByText(/cross-référence vers l’ancre/),
  ).toBeVisible();
  await expect(
    resolved.getByRole("table", { name: "Tableau IOC (1)" }),
  ).toBeVisible();
  const review = page.getByRole("region", { name: /Proposition ciblée/ });
  await expect(
    review.getByRole("link", { name: "Ouvrir dans l’atelier éditorial" }),
  ).toHaveAttribute("href", `/subjects/${api.ids.subject}/editor`);

  await page.getByLabel(/J’ai examiné le texte stocké/).check();
  await page
    .getByRole("button", { name: /Accepter la proposition v4/ })
    .click();
  await expect(
    page.getByRole("heading", { name: "Note acceptée" }),
  ).toBeVisible();
  expect(api.unhandled).toEqual([]);
});

test("note de l’analyste : aucune largeur ne déborde aux trois résolutions", async ({
  page,
}) => {
  const api = createWorkshopApi({
    contentBlocks: [
      {
        block_type: "ioc_table",
        observation_ids: [workshopIds.observation, workshopIds.longObservation],
        title: "Tableau IOC (2)",
      },
    ],
  });
  await launchWorkshop(page, api);
  await openView(page, "Note de l’analyste");

  // The widest realistic value is the bootstrap observation option: a long
  // object type, a full UUID object id and a dotted source. Its short label is
  // shown, the full identity stays in the option title.
  const observationSelect = page.getByLabel("Observation source");
  const longOption = observationSelect.locator(
    `option[value="${workshopIds.longObservation}"]`,
  );
  await expect(longOption).toHaveAttribute(
    "title",
    /investigation\.bootstrap_snapshot/,
  );

  await page.getByRole("button", { name: "Examiner la proposition" }).click();
  await expect(
    page.getByRole("region", { name: /Proposition ciblée/ }),
  ).toBeVisible();
  // The read-only IOC table (full UUID observation ids) is rendered too.
  await expect(
    page.getByRole("table", { name: "Tableau IOC (2)" }),
  ).toBeVisible();

  for (const viewport of [
    { width: 1280, height: 800 },
    { width: 1440, height: 900 },
    { width: 1920, height: 1080 },
  ]) {
    await page.setViewportSize(viewport);
    await page.evaluate("window.scrollTo(0, 0)");
    // The node tsconfig has no DOM lib; these evaluate calls stay strings and
    // locator bounding boxes carry the layout numbers.
    const clientWidth = await page.evaluate<number>(
      "document.documentElement.clientWidth",
    );
    const scrollWidth = await page.evaluate<number>(
      "document.documentElement.scrollWidth",
    );
    const mainBox = await page.locator("main").boundingBox();
    const cardBox = await page
      .locator("section.job-card.opinion-notes")
      .boundingBox();
    expect(clientWidth, `${viewport.width}px client width`).toBe(
      viewport.width,
    );
    expect(
      scrollWidth,
      `${viewport.width}px document scroll width`,
    ).toBeLessThanOrEqual(clientWidth);
    expect(mainBox).not.toBeNull();
    expect(cardBox).not.toBeNull();
    expect(
      cardBox?.width ?? Number.POSITIVE_INFINITY,
      `${viewport.width}px opinion card width`,
    ).toBeLessThanOrEqual((mainBox?.width ?? 0) + 1);
  }
});

test("cases à cocher et radios : le contrôle reste collé au libellé", async ({
  page,
}) => {
  const api = createWorkshopApi();
  await launchWorkshop(page, api);

  const inlineChoices = async () =>
    page
      .locator(
        "label:has(> input[type='checkbox']), label:has(> input[type='radio'])",
      )
      .evaluateAll((labels) =>
        labels.map((label) => {
          const element = label as unknown as {
            querySelector(selector: string): {
              getBoundingClientRect(): Box;
            } | null;
            getBoundingClientRect(): Box;
            textContent: string | null;
          };
          const input = element.querySelector(
            "input[type='checkbox'], input[type='radio']",
          );
          if (input === null) throw new Error("label without control");
          const labelRect = element.getBoundingClientRect();
          const inputRect = input.getBoundingClientRect();
          return {
            text: (element.textContent ?? "").trim().slice(0, 48),
            inputWidth: Math.round(inputRect.width),
            inputHeight: Math.round(inputRect.height),
            offsetFromLabel: Math.round(inputRect.left - labelRect.left),
            topFromLabel: Math.round(inputRect.top - labelRect.top),
          };
        }),
      );

  // Another view (G2 envelope approval confirmation) uses the same inline
  // pattern and must not regress either.
  const overviewChoices = await inlineChoices();
  expect(overviewChoices.length).toBeGreaterThan(0);
  for (const choice of overviewChoices) {
    expect(choice.inputWidth, choice.text).toBeLessThanOrEqual(24);
    expect(choice.inputHeight, choice.text).toBeLessThanOrEqual(24);
    expect(choice.offsetFromLabel, choice.text).toBeLessThanOrEqual(4);
  }

  await openView(page, "Note de l’analyste");
  await page.getByRole("button", { name: "Examiner la proposition" }).click();
  await expect(
    page.getByRole("region", { name: /Proposition ciblée/ }),
  ).toBeVisible();

  const acceptance = page
    .locator("label")
    .filter({ hasText: "J’ai examiné le texte stocké" });
  await expect(acceptance.getByRole("checkbox")).toBeVisible();
  const acceptanceMetrics = await acceptance.evaluate((label) => {
    const element = label as unknown as {
      querySelector(selector: string): {
        getBoundingClientRect(): Box;
      } | null;
      getBoundingClientRect(): Box;
    };
    const input = element.querySelector("input[type='checkbox']");
    if (input === null) throw new Error("missing acceptance checkbox");
    const labelRect = element.getBoundingClientRect();
    const inputRect = input.getBoundingClientRect();
    return {
      inputWidth: Math.round(inputRect.width),
      inputHeight: Math.round(inputRect.height),
      offsetFromLabel: Math.round(inputRect.left - labelRect.left),
      topFromLabel: Math.round(inputRect.top - labelRect.top),
    };
  });
  expect(acceptanceMetrics.inputWidth).toBeLessThanOrEqual(24);
  expect(acceptanceMetrics.inputHeight).toBeLessThanOrEqual(24);
  expect(acceptanceMetrics.offsetFromLabel).toBeLessThanOrEqual(4);
  expect(acceptanceMetrics.topFromLabel).toBeLessThanOrEqual(4);

  const opinionChoices = await inlineChoices();
  expect(opinionChoices.length).toBeGreaterThanOrEqual(15);
  for (const choice of opinionChoices) {
    expect(choice.inputWidth, choice.text).toBeLessThanOrEqual(24);
    expect(choice.inputHeight, choice.text).toBeLessThanOrEqual(24);
    expect(choice.offsetFromLabel, choice.text).toBeLessThanOrEqual(4);
  }
});

test("note de l’analyste : des blocs périmés bloquent l’acceptation", async ({
  page,
}) => {
  const block = {
    block_type: "article_element" as const,
    kind: "table" as const,
    element_key: "table-commands",
    element_sha256: "c".repeat(64),
    anchor: "lead:0001",
  };
  const api = createWorkshopApi({
    contentBlocks: [block],
    contentBlocksState: "stale",
  });
  await launchWorkshop(page, api);
  await openView(page, "Note de l’analyste");

  await page.getByRole("button", { name: "Examiner la proposition" }).click();

  const alert = page.getByRole("alert").filter({
    hasText: /Un élément d’article référencé a changé/,
  });
  await expect(alert).toBeVisible();
  await expect(alert).toContainText("note_block_ref_stale");
  await page.getByLabel(/J’ai examiné le texte stocké/).check();
  await expect(
    page.getByRole("button", { name: /Accepter la proposition v4/ }),
  ).toBeDisabled();

  await page.getByRole("button", { name: "Recharger la proposition" }).click();
  await expect
    .poll(
      () =>
        api.requests.filter(
          (item) =>
            item.method === "GET" &&
            item.path ===
              `/api/investigations/${api.ids.investigation}/note-proposals/${api.ids.proposal}`,
        ).length,
    )
    .toBeGreaterThanOrEqual(2);
  expect(api.requests.filter((item) => item.path.endsWith("/accept"))).toEqual(
    [],
  );
});
