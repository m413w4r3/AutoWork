import type { Page } from "@playwright/test";

import type { Subject } from "../../../src/api/editions";
import { editorialAppOrigin } from "../editorialOrigin";

export const EDITORIAL_APP_ORIGIN = editorialAppOrigin();

export const EDITORIAL_FIXTURES = {
  subject: {
    id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    edition_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    title: "Synthetic editorial article",
    slug: "synthetic-editorial-article",
    tlp: "CLEAR",
    version: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  } satisfies Subject,
  article: {
    title: "Synthetic editorial article",
    paragraphs: [
      {
        id: "synthetic-paragraph-001",
        position: 0,
        text: "Repeated synthetic paragraph for editorial occurrence tests.",
      },
      {
        id: "synthetic-paragraph-002",
        position: 1,
        text: "Repeated synthetic paragraph for editorial occurrence tests.",
      },
    ],
  },
  brief: {
    id: "synthetic-brief-001",
    title: "Synthetic editorial brief",
    summary: "A fixture used only for local editorial tests.",
  },
  enrichment: {
    id: "synthetic-enrichment-001",
    figures: [
      {
        archiveId: "synthetic-figure-001",
        sha256:
          "44a1d590acdf039e4e198f3ef812e1e6f37ccef2226f0471be79c65331446dc2",
        mimeType: "image/svg+xml",
        widthPx: 320,
        heightPx: 180,
        caption: "Synthetic archived figure",
      },
    ],
  },
} as const;

export interface EditorialApiCapture {
  requests: string[];
  unexpectedRequests: string[];
}

export async function stubEditorialApi(
  page: Page,
): Promise<EditorialApiCapture> {
  const capture: EditorialApiCapture = {
    requests: [],
    unexpectedRequests: [],
  };
  const subjectPath = `/api/subjects/${EDITORIAL_FIXTURES.subject.id}`;
  const contentPath = `${subjectPath}/content`;

  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();

    if (url.origin !== EDITORIAL_APP_ORIGIN) {
      capture.unexpectedRequests.push(`${method} ${url.origin}${url.pathname}`);
      await route.abort("blockedbyclient");
      return;
    }

    if (!url.pathname.startsWith("/api/")) {
      await route.continue();
      return;
    }

    capture.requests.push(url.pathname);
    if (method === "GET" && url.pathname === subjectPath) {
      await route.fulfill({ json: EDITORIAL_FIXTURES.subject });
      return;
    }
    if (method === "GET" && url.pathname === contentPath) {
      await route.fulfill({ status: 404, body: "" });
      return;
    }
    const threadPrefix = `${subjectPath}/editor/threads/`;
    if (method === "GET" && url.pathname.startsWith(threadPrefix)) {
      const etag = '"playwright-editorial-thread-empty"';
      if (route.request().headers()["if-none-match"] === etag) {
        await route.fulfill({ status: 304, headers: { ETag: etag } });
        return;
      }
      const occurrenceId = url.pathname.slice(threadPrefix.length);
      const binding = {
        subject_id: EDITORIAL_FIXTURES.subject.id,
        production_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        occurrence_id: occurrenceId,
        artifact_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        artifact_version: 1,
        canonical_sha256:
          "44a1d590acdf039e4e198f3ef812e1e6f37ccef2226f0471be79c65331446dc2",
      };
      const afterSequence = Number(
        url.searchParams.get("after_sequence") ?? "0",
      );
      const timeline: { sequence: number }[] = [];
      await route.fulfill({
        status: 200,
        headers: { ETag: etag },
        json: {
          kind: "unopened",
          binding,
          current: binding,
          bridge_status: "ready",
          event_sequence: 0,
          timeline: timeline.filter((event) => event.sequence > afterSequence),
          etag,
        },
      });
      return;
    }

    capture.unexpectedRequests.push(`${method} ${url.pathname}`);
    await route.abort("blockedbyclient");
  });

  return capture;
}
