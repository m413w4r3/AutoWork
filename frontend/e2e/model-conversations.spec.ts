import { expect, test } from "@playwright/test";

test("crée, sélectionne, continue et archive une conversation d’analyse", async ({
  page,
}) => {
  const subjectId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const conversationId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  let turnCount = 0;
  let archived = false;
  let created = false;
  const postedModes: string[] = [];
  const conversation = () => ({
    id: conversationId,
    provider: "openai",
    transport: "chatgpt_bridge",
    purpose: "analyst_assistance",
    subject_id: subjectId,
    title: "Pivots infrastructure",
    status: archived ? "archived" : turnCount ? "ready" : "pending",
    requested_model: "chatgpt-web",
    expected_profile: "CTI",
    turn_count: turnCount,
    last_used_at: turnCount ? "2026-08-10T12:00:00Z" : null,
    evidence_warning: "not_primary_evidence",
  });

  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith("/workbench")) {
        await route.fulfill({
          json: {
            subject_id: subjectId,
            sources: [],
            claims: [],
            indicators: [],
          },
        });
        return;
      }
      if (url.pathname.endsWith("/turns")) {
        if (request.method() === "POST") {
          const payload = request.postDataJSON() as { mode: string };
          postedModes.push(payload.mode);
          turnCount += 1;
          await route.fulfill({
            json: { id: `turn-${turnCount}`, sequence: turnCount },
          });
          return;
        }
        await route.fulfill({
          json: Array.from({ length: turnCount }, (_, index) => ({
            id: `turn-${index + 1}`,
            sequence: index + 1,
            model_run_id: `run-${index + 1}`,
            correlation_id: `correlation-${index + 1}`,
            status: "succeeded",
            input_text: `Question ${index + 1}`,
            output_text: `Réponse ${index + 1}`,
            error: null,
          })),
        });
        return;
      }
      if (url.pathname.endsWith("/archive")) {
        archived = true;
        await route.fulfill({ json: conversation() });
        return;
      }
      if (
        url.pathname === "/api/model-conversations" &&
        request.method() === "POST"
      ) {
        created = true;
        await route.fulfill({ status: 201, json: conversation() });
        return;
      }
      if (url.pathname === "/api/model-conversations") {
        await route.fulfill({ json: created ? [conversation()] : [] });
        return;
      }
      await route.fulfill({ status: 404, json: {} });
    },
  );

  await page.goto(`/subjects/${subjectId}`);
  await page.getByRole("button", { name: "Conversations" }).click();
  await page
    .getByLabel("Titre défini par l’application")
    .fill("Pivots infrastructure");
  await page.getByRole("button", { name: "Nouvelle conversation" }).click();
  await expect(
    page.getByRole("heading", { name: "Pivots infrastructure" }),
  ).toBeVisible();
  await page.getByLabel("Question").fill("Question 1");
  await page
    .getByLabel(/La classification et la politique de diffusion/)
    .check();
  await page
    .getByRole("button", { name: "Envoyer le premier message" })
    .click();
  await expect(page.getByText("Réponse 1")).toBeVisible();
  await page.getByLabel("Question").fill("Question 2");
  await page
    .getByRole("button", { name: "Continuer cette conversation" })
    .click();
  await expect(page.getByText("Réponse 2")).toBeVisible();
  expect(postedModes).toEqual(["fresh", "continue"]);
  await page.getByRole("button", { name: "Archiver" }).click();
  await expect(page.getByLabel("Question")).toHaveCount(0);
});
