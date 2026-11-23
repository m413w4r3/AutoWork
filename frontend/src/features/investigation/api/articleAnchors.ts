/**
 * Canonical article anchor catalog (`GET /api/investigations/{id}/article-anchors`,
 * F4-D B4).
 *
 * The server resolves the base exactly as note acceptance does: the pinned
 * base of the existing opinion when one exists, else the current verified
 * PUBLICATION. The route is the only read of the rendered anchor space; the
 * note-proposal request stays validated server-side, so a stale base or a
 * removed anchor is refused with a typed code rather than guessed here.
 */

import { useQuery } from "@tanstack/react-query";

import { getJson, investigationPath } from "./client";
import type { Uuid } from "./types";

export type ArticleAnchorSource = "pinned_opinion_base" | "current_base";
export type ArticleElementKind = "table" | "chart" | "diagram" | "figure";

/** One rendered paragraph anchor; indexes are the canonical zero-based ones. */
export interface ArticleAnchorViewV1 {
  anchor: string;
  text: string;
  text_sha256: string;
  section_index: number | null;
  paragraph_index: number | null;
}

/** One M23 element rendered in the resolved base. */
export interface ArticleElementViewV1 {
  kind: ArticleElementKind;
  key: string;
  title: string | null;
  caption: string | null;
  element_sha256: string;
  anchor: string | null;
}

export interface ArticleAnchorCatalogViewV1 {
  base_publication_artifact_id: Uuid;
  base_publication_version: number;
  base_publication_sha256: string;
  source: ArticleAnchorSource;
  anchors: ArticleAnchorViewV1[];
  elements: ArticleElementViewV1[];
  truncated: boolean;
}

export const articleAnchorsKeys = {
  all: (investigationId: string) =>
    ["investigation", investigationId, "article-anchors"] as const,
};

export function getArticleAnchors(
  investigationId: string,
  signal?: AbortSignal,
): Promise<ArticleAnchorCatalogViewV1> {
  return getJson<ArticleAnchorCatalogViewV1>(
    investigationPath(investigationId, "/article-anchors"),
    signal,
  );
}

export function useArticleAnchors(investigationId: string) {
  return useQuery({
    queryKey: articleAnchorsKeys.all(investigationId),
    queryFn: ({ signal }) => getArticleAnchors(investigationId, signal),
    enabled: investigationId !== "",
  });
}
