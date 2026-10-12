import { expect, test } from "@playwright/test";
import { readFileSync, writeFileSync } from "node:fs";

interface BrowserState {
  edition_id: string;
  subjects: { A: string; B: string; C: string };
  database_name: string;
  country: string;
  initial_composition_version: number;
  exclusion_reason: string;
  browser_result?: {
    exclusion_status: number;
    initial_composition_version: number;
    composition_version: number;
    decision_id: string;
  };
}

interface CompositionEntry {
  subject_id: string;
  disposition: string;
  review_decision_id: string | null;
}

interface ExclusionResponse {
  decision_id: string;
  composition: {
    version: number;
    entries: CompositionEntry[];
  };
}

function isBrowserState(value: unknown): value is BrowserState {
  if (typeof value !== "object" || value === null) return false;
  const state = value as Record<string, unknown>;
  const subjects = state.subjects;
  return (
    typeof state.edition_id === "string" &&
    typeof state.database_name === "string" &&
    typeof state.country === "string" &&
    typeof state.initial_composition_version === "number" &&
    typeof state.exclusion_reason === "string" &&
    typeof subjects === "object" &&
    subjects !== null &&
    typeof (subjects as Record<string, unknown>).A === "string" &&
    typeof (subjects as Record<string, unknown>).B === "string" &&
    typeof (subjects as Record<string, unknown>).C === "string"
  );
}

function objectAt(value: unknown, context: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`W10-browser-exclusion / ${context}: expected an object`);
  }
  return value as Record<string, unknown>;
}

function readState(): { path: string; value: BrowserState } {
  const path = process.env.WORKFLOW_BROWSER_STATE;
  if (!path) {
    throw new Error(
      "W10-browser-exclusion / state: WORKFLOW_BROWSER_STATE is required",
    );
  }
  const parsed: unknown = JSON.parse(readFileSync(path, "utf-8"));
  if (!isBrowserState(parsed)) {
    throw new Error(
      "W10-browser-exclusion / state: invalid browser state JSON",
    );
  }
  return { path, value: parsed };
}

test("exclut B avec le backend PostgreSQL réel et accepte A/C", async ({
  page,
}) => {
  const { path: statePath, value: state } = readState();
  const editionId = state.edition_id;
  const exclusionPath = `/api/editions/${editionId}/composition/exclusions/${state.subjects.B}`;

  await page.goto(`/editions/${editionId}/composition`);
  const health = await page.evaluate(async () => {
    try {
      const response = await fetch("/api/health");
      return {
        ok: true,
        status: response.status,
        detail: await response.text(),
      };
    } catch (error) {
      return {
        ok: false,
        status: 0,
        detail: error instanceof Error ? error.message : String(error),
      };
    }
  });
  expect(
    health.ok,
    `W10-browser-exclusion / backend API is unreachable through the Vite proxy: ${health.detail}`,
  ).toBe(true);
  expect(
    health.status,
    `W10-browser-exclusion / expected GET /api/health to return HTTP 200: ${health.detail}`,
  ).toBe(200);

  const initialResponse = await page.evaluate(async (url: string) => {
    const response = await fetch(url);
    return {
      status: response.status,
      body: await response.json(),
    };
  }, `/api/editions/${editionId}/composition`);
  expect(
    initialResponse.status,
    "W10-browser-exclusion / initial composition must come from the real API",
  ).toBe(200);
  const initialRead = objectAt(initialResponse.body, "initial composition");
  const initialComposition = objectAt(
    initialRead.composition,
    "initial composition payload",
  );
  const initialVersion = initialComposition.version;
  if (typeof initialVersion !== "number") {
    throw new Error(
      "W10-browser-exclusion / initial composition version is missing",
    );
  }
  expect(initialVersion).toBe(state.initial_composition_version);
  const initialEntries = initialComposition.entries;
  expect(Array.isArray(initialEntries)).toBe(true);
  const includedSubjectIds = (initialEntries as unknown[])
    .map((entry) => objectAt(entry, "initial composition entry"))
    .filter((entry) => entry.disposition === "included")
    .map((entry) => entry.subject_id);
  expect(includedSubjectIds).toEqual([
    state.subjects.A,
    state.subjects.B,
    state.subjects.C,
  ]);

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
  await cardB.getByRole("button", { name: "Exclure…" }).click();
  const reasonField = page.getByLabel("Motif obligatoire de l’exclusion");
  const confirmExclusion = page.getByRole("button", {
    name: "Confirmer l’exclusion motivée",
  });
  await expect(confirmExclusion).toBeDisabled();
  await reasonField.fill(state.exclusion_reason);
  await expect(confirmExclusion).toBeEnabled();

  const exclusionResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === exclusionPath,
  );
  await confirmExclusion.click();
  const exclusionResponse = await exclusionResponsePromise;
  expect(
    exclusionResponse.status(),
    "W10-browser-exclusion / real POST exclusion response must be HTTP 200",
  ).toBe(200);
  const exclusionBody =
    (await exclusionResponse.json()) as unknown as ExclusionResponse;
  expect(exclusionBody.decision_id).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
  );
  expect(exclusionBody.composition.version).toBe(initialVersion + 1);
  const excludedB = exclusionBody.composition.entries.find(
    (entry) => entry.subject_id === state.subjects.B,
  );
  expect(excludedB).toMatchObject({
    subject_id: state.subjects.B,
    disposition: "excluded",
    review_decision_id: exclusionBody.decision_id,
  });

  state.browser_result = {
    exclusion_status: exclusionResponse.status(),
    initial_composition_version: initialVersion,
    composition_version: exclusionBody.composition.version,
    decision_id: exclusionBody.decision_id,
  };
  writeFileSync(statePath, `${JSON.stringify(state, null, 2)}\n`, "utf-8");
  test.info().annotations.push({
    type: "real-api-exclusion",
    description: `HTTP ${exclusionResponse.status()}; decision_id=${exclusionBody.decision_id}; composition v${exclusionBody.composition.version}`,
  });
  console.log(
    `W10-browser-exclusion / observed POST HTTP ${exclusionResponse.status()} decision_id=${exclusionBody.decision_id} composition=v${exclusionBody.composition.version}`,
  );

  await expect(cardB.getByText("Exclu", { exact: true })).toBeVisible();
  await expect(cardB).toContainText(exclusionBody.decision_id);
  await expect(cardB).toContainText("workflow-analyst");
  await expect(cardB).toContainText(state.exclusion_reason);

  await expect(
    page.getByText("Aucun blocage de validation signalé par l’API."),
    "W10-browser-exclusion / Review must clear blocking diagnostics after the applicable exclusion",
  ).toBeVisible();
  const validationSnapshot = await page.evaluate(async (url: string) => {
    const response = await fetch(url);
    return {
      status: response.status,
      body: await response.json(),
    };
  }, `/api/editions/${editionId}/composition/validation`);
  expect(validationSnapshot.status).toBe(200);
  const validation = objectAt(
    validationSnapshot.body,
    "composition validation",
  );
  expect(validation.can_accept).toBe(true);
  if (!Array.isArray(validation.diagnostics)) {
    throw new Error(
      "W10-browser-exclusion / validation diagnostics are missing",
    );
  }
  const diagnosticsForB = (validation.diagnostics as unknown[])
    .map((diagnostic) => objectAt(diagnostic, "validation diagnostic"))
    .filter((diagnostic) => diagnostic.subject_id === state.subjects.B);
  expect(
    diagnosticsForB.every(
      (diagnostic) => diagnostic.code === "subject_excluded",
    ),
    "W10-browser-exclusion / B may have an informational subject_excluded diagnostic, but no blocker",
  ).toBe(true);
  const confirmation = page.getByRole("checkbox");
  await expect(confirmation).toBeEnabled();
  await confirmation.check();
  const acceptPath = `/api/editions/${editionId}/composition/accept`;
  const acceptResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === acceptPath,
  );
  await page.getByRole("button", { name: "Accepter la composition" }).click();
  const acceptResponse = await acceptResponsePromise;
  expect(
    acceptResponse.status(),
    "W10-browser-exclusion / real composition acceptance must return HTTP 202",
  ).toBe(202);
  const acceptBody = objectAt(await acceptResponse.json(), "accept response");
  expect(acceptBody.manifest_id).toBeTruthy();
  test.info().annotations.push({
    type: "real-api-manifest",
    description: `HTTP ${acceptResponse.status()}; manifest_id=${String(acceptBody.manifest_id)}`,
  });
});
