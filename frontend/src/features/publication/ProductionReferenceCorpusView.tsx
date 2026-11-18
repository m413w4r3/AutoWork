import type { ProductionReferenceCorpus } from "./publicationArtifactSchemas";
import {
  REFERENCE_KIND_LABELS,
  REFERENCE_ROLE_LABELS,
  REFERENCE_TIER_LABELS,
} from "./productionStageLabels";

const COLLECTION_STATE_LABELS: Record<string, string> = {
  archived: "Archivée",
  extracted: "Extraite",
  completed: "Terminée",
  unavailable: "Indisponible",
  blocked: "Bloquée",
  failed_retryable: "Échec — nouvel essai possible",
  failed_terminal: "Échec définitif",
};

export function ProductionReferenceCorpusView({
  corpus,
}: {
  corpus: ProductionReferenceCorpus;
}) {
  return (
    <section className="production-reference-corpus">
      <h3>Corpus de références</h3>
      {corpus.sources.length === 0 ? (
        <p>Aucune source dans le corpus.</p>
      ) : (
        <ul>
          {corpus.sources.map((source) => (
            <li key={source.canonical_url}>
              <article>
                <h4>{source.title ?? source.canonical_url}</h4>
                <dl>
                  <dt>Source</dt>
                  <dd>
                    <a href={source.canonical_url}>{source.canonical_url}</a>
                  </dd>
                  <dt>Provenance</dt>
                  <dd>{REFERENCE_TIER_LABELS[source.tier]}</dd>
                  <dt>Rôle</dt>
                  <dd>{REFERENCE_ROLE_LABELS[source.role] ?? source.role}</dd>
                  <dt>Type</dt>
                  <dd>{REFERENCE_KIND_LABELS[source.kind]}</dd>
                  <dt>Éditeur</dt>
                  <dd>{source.publisher ?? "Non renseigné"}</dd>
                  <dt>Date de publication</dt>
                  <dd>{source.published_at ?? "Non renseignée"}</dd>
                  <dt>État de collecte</dt>
                  <dd>
                    {COLLECTION_STATE_LABELS[source.collection_state] ??
                      source.collection_state}
                  </dd>
                  <dt>Éligibilité</dt>
                  <dd>
                    {source.eligible_for_extraction
                      ? "Éligible à l’extraction"
                      : "Non éligible à l’extraction"}
                  </dd>
                </dl>
              </article>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
