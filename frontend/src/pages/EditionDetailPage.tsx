import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { type Edition, getEdition, transitionEdition } from "../api/editions";
import { ErrorMessage } from "../components/ErrorMessage";
import {
  StatusBadge,
  TlpBadge,
  formatPeriod,
} from "../features/editions/editionPresentation";
import { EditionWorkflow } from "../features/edition-workflow/EditionWorkflow";
import { Link } from "../routing";

export function EditionDetailPage({ editionId }: { editionId: string }) {
  const queryClient = useQueryClient();
  const edition = useQuery({
    queryKey: ["edition", editionId],
    queryFn: () => getEdition(editionId),
  });
  const transition = useMutation({
    mutationFn: (current: Edition) => transitionEdition(current, "archived"),
    onSuccess: (updated) => {
      queryClient.setQueryData(["edition", editionId], updated);
      void queryClient.invalidateQueries({ queryKey: ["editions"] });
    },
  });
  if (edition.isPending) return <p role="status">Chargement de l’édition…</p>;
  if (edition.isError)
    return (
      <ErrorMessage error={edition.error} fallback="Édition inaccessible." />
    );
  const current = edition.data;
  return (
    <section className="detail-page">
      <Link to="/editions">← Toutes les éditions</Link>
      <div className="detail-heading">
        <div>
          <p className="eyebrow">{formatPeriod(current.period_start)}</p>
          <h1>{current.country}</h1>
          <div className="badge-row">
            <StatusBadge status={current.status} />
            <TlpBadge tlp={current.tlp} />
          </div>
        </div>
        <p>Version {current.version}</p>
      </div>
      <section className="progress-panel" aria-labelledby="global-progress">
        <h2 id="global-progress">Progression globale</h2>
        <progress max={100} value={current.progress_percent}>
          {current.progress_percent} %
        </progress>
        <strong>{current.progress_percent} %</strong>
      </section>
      <dl className="edition-facts">
        <div>
          <dt>Langues</dt>
          <dd>{current.languages.join(", ")}</dd>
        </div>
        <div>
          <dt>Objectif indicatif d’articles</dt>
          <dd>{current.target_articles}</dd>
        </div>
        <div>
          <dt>Profil de sources</dt>
          <dd>{current.source_profile}</dd>
        </div>
        <div>
          <dt>Édition précédente</dt>
          <dd>
            {current.previous_edition_id ? (
              <Link to={`/editions/${current.previous_edition_id}`}>
                Ouvrir l’édition précédente
              </Link>
            ) : (
              "Aucune"
            )}
          </dd>
        </div>
      </dl>
      <EditionWorkflow edition={current} />
      <section className="danger-zone" aria-labelledby="edition-archive">
        <h2 id="edition-archive">Actions secondaires</h2>
        {transition.error ? (
          <ErrorMessage
            error={transition.error}
            fallback="Archivage impossible."
          />
        ) : null}
        {current.status !== "assembling" &&
        current.allowed_transitions.includes("archived") ? (
          <button
            className="button button--secondary"
            disabled={transition.isPending}
            onClick={() => transition.mutate(current)}
          >
            {transition.isPending ? "Archivage…" : "Archiver l’édition"}
          </button>
        ) : null}
      </section>
    </section>
  );
}
