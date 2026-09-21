import { expect, test } from "@playwright/test";

import {
  selectionWireBoard,
  selectionWireItem,
  selectionWireLastDecision,
} from "./support/selectionWire";

test("Sélection canonique : confirme, rejoue et reflète l'enrichissement", async ({
  page,
}) => {
  const editionId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const subjectA = "11111111-1111-4111-8111-111111111111";
  const subjectB = "22222222-2222-4222-8222-222222222222";
  const candidateA1 = "33333333-3333-4333-8333-333333333331";
  const candidateA2 = "33333333-3333-4333-8333-333333333332";
  const selectedSubject = "44444444-4444-4444-8444-444444444444";
  const decisionA = "55555555-5555-4555-8555-555555555551";
  const decisionB = "55555555-5555-4555-8555-555555555552";
  let confirmed = false;
  let laterSnapshot = false;
  const posts: Array<{ body: unknown; key: string | undefined }> = [];

  const itemFor = (
    id: string,
    title: string,
    candidateIds: string[],
    state: "undecided" | "ignored" | "selected",
    subjectId: string | null,
    decisionId: string,
  ) =>
    selectionWireItem({
      discovery_subject_id: id,
      title,
      summary: `Présentation neutre de ${title}.`,
      member_candidate_ids: candidateIds,
      effective_state: state,
      subject_id: subjectId,
      last_decision:
        state === "undecided"
          ? null
          : selectionWireLastDecision({
              id: decisionId,
              action: state === "selected" ? "select" : "ignore",
              subject_id: subjectId,
            }),
      updated_since_decision: state === "selected" && laterSnapshot,
    });

  const board = () =>
    selectionWireBoard({
      edition_id: editionId,
      snapshot_id: laterSnapshot
        ? "66666666-6666-4666-8666-666666666662"
        : "66666666-6666-4666-8666-666666666661",
      snapshot_version: laterSnapshot ? 2 : 1,
      items: [
        itemFor(
          subjectA,
          "Article A",
          laterSnapshot ? [candidateA1, candidateA2] : [candidateA1],
          confirmed ? "selected" : "undecided",
          confirmed ? selectedSubject : null,
          decisionA,
        ),
        itemFor(
          subjectB,
          "Article B",
          ["77777777-7777-4777-8777-777777777777"],
          confirmed ? "ignored" : "undecided",
          null,
          decisionB,
        ),
      ],
    });

  await page.route("/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === `/api/editions/${editionId}`)
      return route.fulfill({
        json: {
          id: editionId,
          country: "Iran",
          country_code: "IR",
          period_start: "2026-08-01",
          period_end: "2026-08-31",
          tlp: "AMBER",
          languages: ["fr"],
          state: "open",
          version: 1,
          created_at: "2026-08-29T00:00:00Z",
          updated_at: "2026-08-29T00:00:00Z",
        },
      });
    if (
      path === `/api/editions/${editionId}/selection` &&
      request.method() === "GET"
    )
      return route.fulfill({ json: board() });
    if (
      path === `/api/editions/${editionId}/selection/decisions` &&
      request.method() === "POST"
    ) {
      posts.push({
        body: request.postDataJSON(),
        key: request.headers()["idempotency-key"],
      });
      confirmed = true;
      return route.fulfill({ json: board() });
    }
    if (path === `/api/subjects/${selectedSubject}`)
      return route.fulfill({
        json: {
          id: selectedSubject,
          edition_id: editionId,
          title: "Article A",
          slug: "article-a",
          tlp: "AMBER",
          version: 1,
          created_at: "2026-08-29T00:00:00Z",
          updated_at: "2026-08-29T00:00:00Z",
        },
      });
    return route.fulfill({ status: 404, json: {} });
  });

  await page.goto(`/editions/${editionId}/selection`);

  const cardA = page.getByRole("article").filter({
    has: page.getByRole("heading", { name: "Article A", exact: true }),
  });
  const cardB = page.getByRole("article").filter({
    has: page.getByRole("heading", { name: "Article B", exact: true }),
  });
  await expect(cardA).toHaveCount(1);
  await expect(cardB).toHaveCount(1);

  // A = Traiter, B = Ignorer, scoped to each card rather than to a DOM index.
  await cardA.getByRole("button", { name: "Traiter", exact: true }).click();
  await cardB.getByRole("button", { name: "Ignorer", exact: true }).click();

  // A single batch confirmation carries both decisions.
  await page
    .getByRole("button", { name: /Confirmer les décisions \(2\)/ })
    .click();

  const expectedDecisions = {
    snapshot_version: 1,
    decisions: [
      {
        discovery_subject_id: subjectA,
        action: "select",
        expected_decision_id: null,
      },
      {
        discovery_subject_id: subjectB,
        action: "ignore",
        expected_decision_id: null,
      },
    ],
  };
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0].body).toEqual(expectedDecisions);
  const idempotencyKey = posts[0].key;
  expect(idempotencyKey).toBeTruthy();

  // Only A materializes a Subject; B stays without one.
  const subjectLink = page.getByRole("link", { name: "Sujet créé" });
  await expect(subjectLink).toHaveCount(1);
  await expect(subjectLink).toHaveAttribute(
    "href",
    `/subjects/${selectedSubject}`,
  );
  await expect(cardA.getByRole("link", { name: "Sujet créé" })).toHaveCount(1);
  await expect(cardB.getByRole("link", { name: "Sujet créé" })).toHaveCount(0);
  await expect(cardB).toContainText("Ignoré");

  // Reload keeps both states.
  await page.reload();
  await expect(page.getByRole("link", { name: "Sujet créé" })).toHaveCount(1);
  await expect(cardA.getByRole("link", { name: "Sujet créé" })).toHaveAttribute(
    "href",
    `/subjects/${selectedSubject}`,
  );
  await expect(cardB.getByRole("link", { name: "Sujet créé" })).toHaveCount(0);

  // Replaying the very same Idempotency-Key must not create anything more.
  await page.evaluate(
    ({ edition, key, payload }) => {
      void fetch(`/api/editions/${edition}/selection/decisions`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": key,
        },
        body: JSON.stringify(payload),
      });
    },
    {
      edition: editionId,
      key: idempotencyKey as string,
      payload: expectedDecisions,
    },
  );
  await expect.poll(() => posts.length).toBe(2);
  expect(posts[1].key).toBe(idempotencyKey);
  expect(posts[1].body).toEqual(expectedDecisions);

  await page.reload();
  await expect(page.getByRole("link", { name: "Sujet créé" })).toHaveCount(1);
  await expect(cardA.getByRole("link", { name: "Sujet créé" })).toHaveAttribute(
    "href",
    `/subjects/${selectedSubject}`,
  );

  // A richer snapshot enriches A without changing its subject_id.
  laterSnapshot = true;
  await page.reload();
  await expect(cardA).toContainText("Mis à jour depuis la décision");
  await expect(page.getByRole("link", { name: "Sujet créé" })).toHaveCount(1);
  await expect(cardA.getByRole("link", { name: "Sujet créé" })).toHaveAttribute(
    "href",
    `/subjects/${selectedSubject}`,
  );
  await expect(cardB.getByRole("link", { name: "Sujet créé" })).toHaveCount(0);
});
