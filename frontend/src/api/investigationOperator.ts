import { useInfiniteQuery } from "@tanstack/react-query";

import { investigationRequest } from "./investigation";

export interface ArticleClaimSource {
  id: string;
  statement: string;
  source_refs: string[];
}

export interface OperatorContextSectionPage {
  schema: "investigation-operator-context-section.v1";
  investigation_id: string;
  section: "article_claims";
  offset: number;
  limit: number;
  total: number;
  next_offset: number | null;
  items: ArticleClaimSource[];
}

export const investigationOperatorQueryKeys = {
  articleClaims: (investigationId: string) =>
    ["investigation-operator-article-claims", investigationId] as const,
};

export function getArticleClaimSources(
  investigationId: string,
  offset: number,
): Promise<OperatorContextSectionPage> {
  const query = new URLSearchParams({
    offset: String(offset),
    limit: "20",
  });
  return investigationRequest(
    `/api/investigations/${encodeURIComponent(investigationId)}/operator/context/section/article_claims?${query.toString()}`,
  );
}

export function useArticleClaimSources(investigationId: string) {
  return useInfiniteQuery({
    queryKey: investigationOperatorQueryKeys.articleClaims(investigationId),
    queryFn: ({ pageParam }) =>
      getArticleClaimSources(investigationId, pageParam),
    initialPageParam: 0,
    getNextPageParam: (lastPage) => lastPage.next_offset ?? undefined,
    enabled: Boolean(investigationId),
  });
}
