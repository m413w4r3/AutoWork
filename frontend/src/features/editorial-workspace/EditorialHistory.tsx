import { useInfiniteQuery } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { ApiError } from "../../api/editions";
import {
  getEditorialHistory,
  type EditorialHistoryItem,
  type EditorialProposalState,
} from "../../api/editorialWorkspace";
import { editorialWorkspaceQueryKeys } from "./editorialWorkspaceQueries";

interface EditorialHistoryProps {
  subjectId: string;
  onClose: () => void;
  onOpenThread: (occurrenceId: string) => void;
  autoFocusHeading?: boolean;
}

const stateLabels: Record<EditorialProposalState, string> = {
  preparing: "Préparation en cours",
  proposed: "En attente",
  refused: "Refusée",
  needs_review: "À examiner",
  accepted: "Acceptée",
  rejected: "Rejetée",
  stale: "Périmée",
};

function actorLabel(actorDisplay: string): string {
  return actorDisplay.split(" · ", 1)[0] || "Opérateur";
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("fr-FR", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function technicalRows(item: EditorialHistoryItem) {
  return [
    ["Identifiant de proposition", item.proposal_id],
    ["Identifiant du bloc", item.occurrence_id],
    ["Identifiant de l’acteur", item.actor_id],
    ["Artefact canonique résultant", item.canonical_result_artifact_id ?? "—"],
    ["Identifiant du fil", item.thread_id ?? "—"],
  ] as const;
}

export function EditorialHistory({
  subjectId,
  onClose,
  onOpenThread,
  autoFocusHeading = true,
}: EditorialHistoryProps) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const historyQuery = useInfiniteQuery({
    queryKey: editorialWorkspaceQueryKeys.history(subjectId),
    queryFn: ({ pageParam }) =>
      getEditorialHistory(subjectId, {
        cursor: pageParam ?? undefined,
        limit: 20,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    retry: false,
  });
  const items = historyQuery.data?.pages.flatMap((page) => page.items) ?? [];
  const errorMessage =
    historyQuery.error instanceof ApiError
      ? historyQuery.error.message
      : "L’historique est momentanément indisponible.";

  useEffect(() => {
    if (!autoFocusHeading) return;
    headingRef.current?.focus({ preventScroll: true });
  }, [autoFocusHeading]);

  return (
    <section
      className="editorial-history"
      aria-labelledby="editorial-history-heading"
      aria-busy={historyQuery.isPending || historyQuery.isFetchingNextPage}
      aria-live="polite"
    >
      <div className="editorial-history__header">
        <div>
          <p className="editorial-history__eyebrow">Consultation</p>
          <h2 id="editorial-history-heading" ref={headingRef} tabIndex={-1}>
            Historique des propositions
          </h2>
        </div>
        <button
          className="button button--secondary"
          type="button"
          onClick={onClose}
        >
          Fermer l’historique
        </button>
      </div>

      {historyQuery.isPending ? (
        <p className="editorial-history__state" role="status">
          Chargement de l’historique…
        </p>
      ) : null}

      {historyQuery.isError ? (
        <div className="editorial-history__state" role="alert">
          <p>{errorMessage}</p>
          <button
            className="button button--secondary"
            type="button"
            onClick={() => void historyQuery.refetch()}
          >
            Réessayer
          </button>
        </div>
      ) : null}

      {historyQuery.isSuccess && items.length === 0 ? (
        <p className="editorial-history__state">
          Aucune proposition dans l’historique.
        </p>
      ) : null}

      {items.length > 0 ? (
        <ol className="editorial-history__list" aria-label="Propositions">
          {items.map((item) => (
            <li className="editorial-history__item" key={item.proposal_id}>
              <article>
                <div className="editorial-history__item-heading">
                  <h3>{item.operation_label}</h3>
                  <span className="editorial-history__state-label">
                    {stateLabels[item.state]}
                  </span>
                </div>
                <dl className="editorial-history__summary">
                  <div>
                    <dt>Origine</dt>
                    <dd>
                      {item.origin === "manual_patch"
                        ? "Modification manuelle"
                        : "Bridge"}
                    </dd>
                  </div>
                  <div>
                    <dt>Version résultante</dt>
                    <dd>
                      {item.canonical_result_version === null
                        ? "Aucune nouvelle version canonique"
                        : `Version canonique v${item.canonical_result_version}`}
                    </dd>
                  </div>
                  <div>
                    <dt>Date</dt>
                    <dd>
                      <time dateTime={item.created_at}>
                        {formatDate(item.created_at)}
                      </time>
                    </dd>
                  </div>
                  <div>
                    <dt>Acteur</dt>
                    <dd>{actorLabel(item.actor_display)}</dd>
                  </div>
                </dl>
                {item.origin === "bridge_draft" && item.thread_url ? (
                  <a
                    className="editorial-history__thread-link"
                    href={`/subjects/${encodeURIComponent(subjectId)}/editor?thread=${encodeURIComponent(item.occurrence_id)}`}
                    onClick={(event) => {
                      if (
                        event.button !== 0 ||
                        event.metaKey ||
                        event.ctrlKey ||
                        event.shiftKey ||
                        event.altKey
                      ) {
                        return;
                      }
                      event.preventDefault();
                      onOpenThread(item.occurrence_id);
                    }}
                  >
                    Ouvrir le fil Bridge
                  </a>
                ) : null}
                <details className="editorial-history__technical">
                  <summary>Détails techniques</summary>
                  <dl>
                    {technicalRows(item).map(([label, value]) => (
                      <div key={label}>
                        <dt>{label}</dt>
                        <dd>{value}</dd>
                      </div>
                    ))}
                  </dl>
                </details>
              </article>
            </li>
          ))}
        </ol>
      ) : null}

      {historyQuery.hasNextPage ? (
        <button
          className="button button--secondary editorial-history__load-more"
          type="button"
          disabled={historyQuery.isFetchingNextPage}
          onClick={() => void historyQuery.fetchNextPage()}
        >
          {historyQuery.isFetchingNextPage ? "Chargement…" : "Charger plus"}
        </button>
      ) : null}
    </section>
  );
}
