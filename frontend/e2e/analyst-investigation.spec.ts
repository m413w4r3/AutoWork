import { expect, test } from "@playwright/test";

import { createAnalystInvestigationApi } from "./support/analystInvestigationApi";

test("enquête : lancer, suivre, réviser, approuver et proposer une publication augmentée", async ({
  page,
}) => {
  const api = createAnalystInvestigationApi();
  await api.install(page);
  await page.goto(`/subjects/${api.ids.subject}`);

  await page
    .getByRole("button", { name: "Enquête / Avis de l’analyste" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Synthèse exacte à investiguer" }),
  ).toBeVisible();
  await expect(page.getByText(api.ids.run)).toBeVisible();
  await expect(page.getByText(api.ids.synthesis)).toBeVisible();
  await expect(page.getByText("a".repeat(64))).toBeVisible();

  await page
    .getByRole("button", { name: "Investiguer cette version exacte" })
    .click();
  await expect(
    page.getByRole("heading", { name: `Enquête ${api.ids.investigation}` }),
  ).toBeVisible();
  await expect(
    page.getByText(/Une variante complémentaire est étayée/),
  ).toBeVisible();
  await expect(
    page.getByText(/Capacités indisponibles, limites locales/),
  ).toBeVisible();

  await expect(
    page.getByText(
      "Proposition locale non approuvée · aucun droit externe accordé",
    ),
  ).toBeVisible();
  await page.getByLabel("vt", { exact: true }).check();
  await page.getByLabel("Autoriser l’acquisition automatique de hits").check();
  await page.getByLabel("Téléchargements").fill("2");
  await page
    .getByLabel("Raison obligatoire", { exact: true })
    .fill(
      "Les pivots VT et deux acquisitions sont nécessaires pour vérifier la chaîne.",
    );
  await page.getByLabel(/Je confirme cette décision G2/).check();
  await page.getByRole("button", { name: "Confirmer l’approbation" }).click();
  await expect(page.getByText(/Enveloppe approuvée · version 1/)).toBeVisible();

  await page
    .getByLabel("Décision G3 pour dddddddddddd")
    .selectOption("confirmed");
  await page
    .getByLabel("Raison obligatoire pour dddddddddddd")
    .fill("La relation et l’analyse locale corroborent l’appartenance.");
  await page
    .getByLabel("Preuves pour dddddddddddd")
    .selectOption(api.ids.evidence);
  await page
    .getByLabel(/Je confirme cette décision de qualification humaine/)
    .check();
  await page.getByRole("button", { name: "Confirmer", exact: true }).click();
  await expect(
    page.getByText(/Qualification enregistrée avec l’acteur authentifié/),
  ).toBeVisible();

  await page
    .getByLabel("Demande (2 000 caractères maximum)")
    .fill(
      "Vérifier si le hash ciblé partage le comportement de la variante S2.",
    );
  await page.getByLabel("Type de cible (facultatif)").selectOption("hash");
  await page.getByLabel("Cible (256 caractères maximum)").fill("e".repeat(64));
  await page.getByRole("button", { name: "Demander cette analyse" }).click();
  await expect(
    page.getByText(/Demande prise en compte pour le prochain tour/),
  ).toBeVisible();

  await page
    .getByRole("button", { name: "Composer depuis les constats" })
    .click();
  await expect(page.getByLabel("Texte du bloc")).toHaveValue(
    "Une variante complémentaire est étayée.",
  );
  await page
    .getByLabel("Texte du bloc")
    .fill("La variante S2 complète l’article étudié.");
  await page
    .getByRole("button", { name: "Enregistrer la révision (CAS v1)" })
    .click();

  await page
    .getByLabel("Raison de la décision")
    .fill("Les preuves et limites ont été relues.");
  await page
    .getByRole("button", { name: "Approuver cette version précise" })
    .click();
  await page
    .getByRole("button", { name: "Proposer la version augmentée" })
    .click();

  await expect(
    page.getByRole("heading", { name: "Version augmentée proposée" }),
  ).toBeVisible();
  await expect(
    page.getByText(/Repin à faire dans la composition/),
  ).toBeVisible();
  await expect(
    page.getByRole("link", {
      name: "Ouvrir la composition pour réépingler l’article",
    }),
  ).toHaveAttribute("href", `/editions/${api.ids.edition}/composition`);

  const launch = api.requests.find(
    (request) =>
      request.method === "POST" &&
      request.path === `/api/subjects/${api.ids.subject}/investigations`,
  );
  expect(launch?.body).toEqual({
    production_run_id: api.ids.run,
    synthesis_artifact_id: api.ids.synthesis,
    synthesis_sha256: "a".repeat(64),
  });
  expect(launch?.idempotencyKey).toBeTruthy();

  const revision = api.requests.find(
    (request) => request.path === `/api/opinions/${api.ids.opinion}/revise`,
  );
  expect(revision?.body).toMatchObject({
    expected_version: 1,
    blocks: [
      expect.objectContaining({
        block_type: "key_findings",
        text: "La variante S2 complète l’article étudié.",
      }),
    ],
  });
  expect(revision?.idempotencyKey).toBeTruthy();

  const approval = api.requests.find(
    (request) => request.path === `/api/opinions/${api.ids.opinion}/approve`,
  );
  expect(approval?.body).toEqual({
    version: 2,
    expected_sha256: "c".repeat(64),
    reason: "Les preuves et limites ont été relues.",
  });
  expect(approval?.idempotencyKey).toBeTruthy();

  const envelopeApproval = api.requests.find(
    (request) =>
      request.path ===
      `/api/investigations/${api.ids.investigation}/envelope/approve`,
  );
  expect(envelopeApproval?.body).toMatchObject({
    expected_version: null,
    allowed_providers: ["vt"],
    acquire_hits_allowed: true,
    max_acquire_volume: 2,
    reason:
      "Les pivots VT et deux acquisitions sont nécessaires pour vérifier la chaîne.",
  });
  expect(envelopeApproval?.idempotencyKey).toBeTruthy();

  const qualification = api.requests.find(
    (request) =>
      request.path ===
      `/api/investigations/${api.ids.investigation}/samples/${api.ids.association}/qualify`,
  );
  expect(qualification?.body).toMatchObject({
    expected_version: 1,
    qualification: "confirmed",
    evidence_refs: [api.ids.evidence],
  });
  expect(qualification?.idempotencyKey).toBeTruthy();

  const targetedRequest = api.requests.find(
    (request) =>
      request.path === `/api/investigations/${api.ids.investigation}/requests`,
  );
  expect(targetedRequest?.body).toMatchObject({
    text: "Vérifier si le hash ciblé partage le comportement de la variante S2.",
    target: { kind: "hash", value: "e".repeat(64) },
  });
  expect(targetedRequest?.idempotencyKey).toBeTruthy();
});
