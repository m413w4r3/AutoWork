import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import {
  decideEditionRepairsBulk,
  getEditionRepairPage,
  rebuildEditionReviewItem,
  type EditionRepairArticle,
  type EditionRepairItem,
  type EditionRepairPage,
  type EditionRepairSummary,
  type ProductionRepairAction,
  type ReviewItem,
} from "../../api/publication";
import { ApiError } from "../../api/editions";
import { ReviewItemCard } from "./ReviewItemCard";
import {
  RepairQueue,
  repairIssueMatchesFilter,
  repairReasonLabel,
  type RepairQueueFilter,
} from "./RepairQueue";
import { RepairIssueInspector } from "./RepairIssueInspector";
import { RepairRebuildBar } from "./RepairRebuildBar";

const EMPTY_SUMMARY: EditionRepairSummary = {
  unresolved_total: 0,
  sources_to_supply: 0,
  rejected_iocs_to_review: 0,
  rejected_rules_to_review: 0,
  rejected_other_artifacts: 0,
  articles_with_repairs: 0,
  articles_needing_rebuild: 0,
};

const ACTIVE_REBUILD_STATUSES = new Set(["queued", "running"]);
const EMPTY_REPAIR_PAGES: EditionRepairPage[] = [];

function invalidateRepairDesk(
  queryClient: ReturnType<typeof useQueryClient>,
  editionId: string,
) {
  void queryClient.invalidateQueries({
    queryKey: ["edition-repair", editionId],
  });
  void queryClient.invalidateQueries({
    queryKey: ["edition-review", editionId],
  });
  void queryClient.invalidateQueries({ queryKey: ["batch", editionId] });
  void queryClient.invalidateQueries({ queryKey: ["edition", editionId] });
}

function actionLabel(action: ProductionRepairAction): string {
  return action === "include" ? "inclusion" : "exclusion";
}

function searchMatches(item: EditionRepairItem, search: string): boolean {
  const normalizedSearch = search.trim().toLocaleLowerCase("fr-FR");
  if (!normalizedSearch) return true;
  return [
    item.article_title,
    item.source_id,
    item.source_title,
    item.artifact_type,
    item.preview,
    item.reason_code,
    repairReasonLabel(item.reason_code),
  ]
    .filter((value): value is string => Boolean(value))
    .some((value) =>
      value.toLocaleLowerCase("fr-FR").includes(normalizedSearch),
    );
}

function deduplicateArticles(
  pages: EditionRepairArticle[],
): EditionRepairArticle[] {
  const bySubject = new Map<string, EditionRepairArticle>();
  for (const article of pages) bySubject.set(article.subject_id, article);
  return Array.from(bySubject.values());
}

export function RepairDesk({
  editionId,
  reviewItems,
  readOnly,
  onSummaryChange,
}: {
  editionId: string;
  reviewItems: ReviewItem[];
  readOnly: boolean;
  onSummaryChange: (summary: EditionRepairSummary) => void;
}) {
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<RepairQueueFilter>("all");
  const [subjectFilter, setSubjectFilter] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [selectedKeys, setSelectedKeys] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const [bulkAction, setBulkAction] = useState<ProductionRepairAction | null>(
    null,
  );
  const [message, setMessage] = useState<string | null>(null);
  const [forcedRebuilds, setForcedRebuilds] = useState<
    ReadonlyMap<string, string>
  >(() => new Map());
  const [pendingRebuilds, setPendingRebuilds] = useState<ReadonlySet<string>>(
    () => new Set(),
  );

  const hasActiveReviewRun = reviewItems.some((item) =>
    ACTIVE_REBUILD_STATUSES.has(item.run_status),
  );
  const repairs = useInfiniteQuery({
    queryKey: ["edition-repair", editionId],
    queryFn: ({ pageParam }) =>
      getEditionRepairPage(editionId, {
        status: "all",
        cursor: pageParam,
        limit: 100,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor,
    // Read-only never disables reading: a historical review must show the same
    // queue, counters and evidence. Only the mutations below are withdrawn.
    refetchInterval: hasActiveReviewRun && !readOnly ? 2_000 : false,
  });

  const pages = repairs.data?.pages ?? EMPTY_REPAIR_PAGES;
  const items = useMemo(() => {
    const byKey = new Map<string, EditionRepairItem>();
    for (const page of pages) {
      for (const item of page.items) byKey.set(item.repair_key, item);
    }
    return Array.from(byKey.values());
  }, [pages]);
  const summary = repairs.data?.pages[0]?.summary ?? EMPTY_SUMMARY;
  const blockingSubjectIds = useMemo(
    () =>
      new Set(
        reviewItems
          .filter((item) => item.blocking)
          .map((item) => item.subject_id),
      ),
    [reviewItems],
  );
  const visibleItems = useMemo(
    () =>
      items.filter(
        (item) =>
          (!subjectFilter || item.subject_id === subjectFilter) &&
          repairIssueMatchesFilter(item, filter, blockingSubjectIds) &&
          searchMatches(item, search),
      ),
    [blockingSubjectIds, filter, items, search, subjectFilter],
  );
  const articles = useMemo(() => {
    const current = deduplicateArticles(pages.flatMap((page) => page.articles));
    const bySubject = new Map(
      current.map((article) => [article.subject_id, article]),
    );
    for (const [subjectId, recommendedStage] of forcedRebuilds) {
      if (!bySubject.has(subjectId)) {
        bySubject.set(subjectId, {
          subject_id: subjectId,
          has_pending_projection: false,
          recommended_stage: recommendedStage,
          active_repair_count: 0,
          resolved_since_last_build_count: 1,
        });
      }
    }
    return Array.from(bySubject.values());
  }, [forcedRebuilds, pages]);
  const titles = useMemo(
    () => new Map(reviewItems.map((item) => [item.subject_id, item.title])),
    [reviewItems],
  );
  const selectedItem =
    items.find((item) => item.repair_key === selectedKey) ?? null;
  const selectedBulkItems = items.filter(
    (item) =>
      selectedKeys.has(item.repair_key) &&
      !item.resolved &&
      item.kind !== "supplemental_source_unarchived" &&
      item.artifact_id !== null,
  );

  useEffect(() => {
    const localRebuildCount = articles.filter(
      (article) =>
        article.has_pending_projection ||
        article.resolved_since_last_build_count > 0,
    ).length;
    onSummaryChange({
      ...summary,
      articles_needing_rebuild: Math.max(
        summary.articles_needing_rebuild,
        localRebuildCount,
        pendingRebuilds.size,
      ),
    });
  }, [articles, onSummaryChange, pendingRebuilds.size, summary]);

  useEffect(() => {
    setPendingRebuilds((current) => {
      const next = new Set(
        Array.from(current).filter((subjectId) => {
          const article = articles.find(
            (candidate) => candidate.subject_id === subjectId,
          );
          const reviewItem = reviewItems.find(
            (candidate) => candidate.subject_id === subjectId,
          );
          const reviewStillRunning = Boolean(
            reviewItem && ACTIVE_REBUILD_STATUSES.has(reviewItem.run_status),
          );
          return Boolean(
            reviewStillRunning ||
            (article &&
              (article.has_pending_projection ||
                article.resolved_since_last_build_count > 0)),
          );
        }),
      );
      return next.size === current.size ? current : next;
    });
  }, [articles, reviewItems]);

  useEffect(() => {
    setSelectedKeys((current) => {
      const next = new Set(
        Array.from(current).filter((key) =>
          items.some((item) => item.repair_key === key),
        ),
      );
      return next.size === current.size ? current : next;
    });
    if (selectedKey && !items.some((item) => item.repair_key === selectedKey)) {
      setSelectedKey(null);
    }
  }, [items, selectedKey]);

  const decideBulk = useMutation({
    mutationFn: (action: ProductionRepairAction) =>
      decideEditionRepairsBulk(
        editionId,
        selectedBulkItems.map((item) => ({
          repairKey: item.repair_key,
          action,
          observedSubjectId: item.subject_id,
          observedRunId: item.run_id,
          observedArtifactId: item.artifact_id as string,
          observedPipelineGeneration: item.pipeline_generation,
          // The backend fence: the batch must revise exactly what was listed.
          expectedEffectiveDecisionId: item.effective_decision_id,
        })),
      ),
    retry: false,
    onSuccess: (_result, action) => {
      setSelectedKeys(new Set());
      setBulkAction(null);
      setMessage(
        `${selectedBulkItems.length} décisions d’${actionLabel(action)} enregistrées.`,
      );
      invalidateRepairDesk(queryClient, editionId);
    },
    onError: (error: unknown) => {
      setBulkAction(null);
      if (
        error instanceof ApiError &&
        (error.code === "production_repair_stale" ||
          error.code === "production_repair_decision_changed")
      ) {
        setMessage("Certains éléments ont changé. La file a été rechargée.");
        invalidateRepairDesk(queryClient, editionId);
      } else {
        setMessage(
          error instanceof Error ? error.message : "L’action groupée a échoué.",
        );
      }
    },
  });

  const rebuild = useMutation({
    mutationFn: (subjectId: string) =>
      rebuildEditionReviewItem(editionId, subjectId),
    retry: false,
    onMutate: (subjectId) => {
      setPendingRebuilds((current) => new Set(current).add(subjectId));
    },
    onSuccess: (result, subjectId) => {
      if (result.action === "awaiting_repair_decision") {
        setMessage(
          "La reconstruction attend encore des décisions dans la file de réparation.",
        );
        setPendingRebuilds((current) => {
          const next = new Set(current);
          next.delete(subjectId);
          return next;
        });
      } else {
        setMessage(
          "Reconstruction lancée. Les arbitrages déjà enregistrés sont conservés.",
        );
      }
      if (result.action !== "awaiting_repair_decision") {
        setForcedRebuilds((current) => {
          const next = new Map(current);
          next.delete(subjectId);
          return next;
        });
      }
      invalidateRepairDesk(queryClient, editionId);
    },
    onError: (error: unknown, subjectId) => {
      setPendingRebuilds((current) => {
        const next = new Set(current);
        next.delete(subjectId);
        return next;
      });
      if (
        error instanceof ApiError &&
        error.code === "production_repair_stale"
      ) {
        setMessage(
          "L’article a changé. La file a été rechargée avant une nouvelle tentative.",
        );
        invalidateRepairDesk(queryClient, editionId);
      } else {
        setMessage(
          error instanceof Error
            ? error.message
            : "La reconstruction a échoué.",
        );
      }
    },
  });

  const changeRepair = () => {
    setMessage("Décision enregistrée.");
    invalidateRepairDesk(queryClient, editionId);
  };

  const handleFilterChange = (nextFilter: RepairQueueFilter) => {
    setFilter(nextFilter);
    setSubjectFilter(null);
    setSelectedKey(null);
    setSelectedKeys(new Set());
  };

  const filterArticleIssues = (
    nextFilter: RepairQueueFilter,
    subjectId: string,
  ) => {
    setFilter(nextFilter);
    setSubjectFilter(subjectId);
    setSelectedKey(null);
    setSelectedKeys(new Set());
  };

  const runRebuild = (subjectId: string) => {
    if (!pendingRebuilds.has(subjectId)) rebuild.mutate(subjectId);
  };

  return (
    <section className="repair-desk" aria-labelledby="repair-desk-heading">
      <div className="repair-desk__summary">
        <div>
          <p className="eyebrow">Poste de travail</p>
          <h3 id="repair-desk-heading">Revue technique</h3>
          <p>
            {items.filter((item) => item.resolved).length} / {items.length}{" "}
            éléments arbitrés
            {repairs.hasNextPage ? " sur les éléments chargés" : ""}
          </p>
        </div>
        <div
          className="repair-summary-counters"
          aria-label="Résumé actionnable de la revue"
        >
          <button type="button" onClick={() => handleFilterChange("blocking")}>
            <strong>
              {reviewItems.filter((item) => item.blocking).length}
            </strong>
            <span>Articles bloquants</span>
          </button>
          <button type="button" onClick={() => handleFilterChange("sources")}>
            <strong>{summary.sources_to_supply}</strong>
            <span>Sources à fournir</span>
          </button>
          <button type="button" onClick={() => handleFilterChange("ioc")}>
            <strong>{summary.rejected_iocs_to_review}</strong>
            <span>IOC à arbitrer</span>
          </button>
          <button type="button" onClick={() => handleFilterChange("rules")}>
            <strong>{summary.rejected_rules_to_review}</strong>
            <span>Règles à arbitrer</span>
          </button>
          <button type="button" onClick={() => handleFilterChange("other")}>
            <strong>{summary.rejected_other_artifacts}</strong>
            <span>Autres pertes</span>
          </button>
        </div>
      </div>

      {message ? (
        <p className="repair-desk__message" role="status">
          {message}
        </p>
      ) : null}
      {repairs.isPending ? (
        <p role="status">Chargement de la file de réparation…</p>
      ) : null}
      {repairs.isError ? (
        <p className="error-message" role="alert">
          La file de réparation est inaccessible : {repairs.error.message}
        </p>
      ) : null}

      {readOnly ? (
        <p className="workflow-read-only-note">
          Cette revue historique est disponible en lecture seule : la file, les
          preuves et l’audit des décisions restent consultables, aucune
          modification n’est possible.
        </p>
      ) : null}

      <div className="repair-desk__workspace">
        <div>
          <RepairQueue
            selectable={!readOnly}
            items={visibleItems}
            filter={filter}
            search={search}
            selectedKey={selectedKey}
            selectedKeys={selectedKeys}
            blockingSubjectIds={blockingSubjectIds}
            onFilterChange={handleFilterChange}
            onSearchChange={setSearch}
            onSelect={(item) => {
              setSelectedKey(item.repair_key);
              setMessage(null);
            }}
            onToggleSelection={(item, selected) => {
              setSelectedKeys((current) => {
                const next = new Set(current);
                if (selected) next.add(item.repair_key);
                else next.delete(item.repair_key);
                return next;
              });
            }}
          />
          {!readOnly && selectedKeys.size > 0 ? (
            <div className="repair-bulk-actions" aria-label="Actions groupées">
              <p>
                {selectedKeys.size} élément{selectedKeys.size > 1 ? "s" : ""}{" "}
                sélectionné
                {selectedKeys.size > 1 ? "s" : ""}.
              </p>
              {selectedBulkItems.length !== selectedKeys.size ? (
                <p>
                  Les sources ne peuvent pas être incluses ou exclues en action
                  groupée.
                </p>
              ) : null}
              {bulkAction ? (
                <div className="repair-bulk-actions__confirmation">
                  <p>
                    Confirmer l&apos;{actionLabel(bulkAction)} de{" "}
                    {selectedBulkItems.length} élément
                    {selectedBulkItems.length > 1 ? "s" : ""} ?
                  </p>
                  <button
                    className="button"
                    type="button"
                    disabled={
                      decideBulk.isPending || selectedBulkItems.length === 0
                    }
                    onClick={() => decideBulk.mutate(bulkAction)}
                  >
                    Confirmer l&apos;{actionLabel(bulkAction)} de{" "}
                    {selectedBulkItems.length} élément
                    {selectedBulkItems.length > 1 ? "s" : ""}
                  </button>
                  <button
                    className="button button--secondary"
                    type="button"
                    onClick={() => setBulkAction(null)}
                  >
                    Annuler
                  </button>
                </div>
              ) : (
                <div className="repair-desk__actions">
                  <button
                    className="button"
                    type="button"
                    disabled={selectedBulkItems.length !== selectedKeys.size}
                    onClick={() => setBulkAction("include")}
                  >
                    Inclure {selectedBulkItems.length} éléments
                  </button>
                  <button
                    className="button button--danger"
                    type="button"
                    disabled={selectedBulkItems.length !== selectedKeys.size}
                    onClick={() => setBulkAction("exclude")}
                  >
                    Exclure {selectedBulkItems.length} éléments
                  </button>
                </div>
              )}
            </div>
          ) : null}
        </div>
        <RepairIssueInspector
          editionId={editionId}
          item={selectedItem}
          article={
            reviewItems.find(
              (candidate) => candidate.subject_id === selectedItem?.subject_id,
            ) ?? null
          }
          readOnly={readOnly}
          onChanged={changeRepair}
          onArchived={(item) => {
            const recommendedStage = item.recommended_stage;
            if (recommendedStage) {
              setForcedRebuilds((current) => {
                const next = new Map(current);
                next.set(item.subject_id, recommendedStage);
                return next;
              });
            }
            setMessage(
              "Source archivée — reconstruction des références nécessaire.",
            );
          }}
        />
      </div>

      {repairs.hasNextPage ? (
        <button
          className="button button--secondary repair-desk__load-more"
          type="button"
          disabled={repairs.isFetchingNextPage}
          onClick={() => void repairs.fetchNextPage()}
        >
          {repairs.isFetchingNextPage ? "Chargement…" : "Charger la suite"}
        </button>
      ) : null}

      {!readOnly ? (
        <RepairRebuildBar
          articles={articles}
          titles={titles}
          pendingSubjects={pendingRebuilds}
          readOnly={readOnly}
          onRebuild={runRebuild}
          onRebuildAll={() => {
            for (const article of articles) {
              if (
                (article.has_pending_projection ||
                  article.resolved_since_last_build_count > 0) &&
                !pendingRebuilds.has(article.subject_id)
              ) {
                rebuild.mutate(article.subject_id);
              }
            }
          }}
        />
      ) : null}

      <section
        className="review-articles"
        aria-labelledby="review-articles-heading"
      >
        <div className="review-articles__heading">
          <div>
            <p className="eyebrow">Périmètre</p>
            <h3 id="review-articles-heading">Articles</h3>
          </div>
          <span>
            {reviewItems.length} article{reviewItems.length > 1 ? "s" : ""}
          </span>
        </div>
        {reviewItems.length > 0 ? (
          <ol className="review-item-list" aria-label="Articles à revoir">
            {reviewItems.map((item) => (
              <ReviewItemCard
                key={`${item.subject_id}-${item.position}`}
                editionId={editionId}
                item={item}
                readOnly={readOnly}
                onRepairFilter={(nextFilter, subjectId) =>
                  filterArticleIssues(nextFilter, subjectId)
                }
              />
            ))}
          </ol>
        ) : (
          <p className="empty-state">Aucun article à revoir.</p>
        )}
      </section>
    </section>
  );
}
