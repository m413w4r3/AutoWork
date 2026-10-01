import { useQuery } from "@tanstack/react-query";
import { useRef } from "react";

import { editionPreviewPdfUrl, getEditionPreview } from "../../api/publication";
import { PublicationDocumentView } from "../../components/ProductionArtifactView";

export function EditionPreviewPanel({ editionId }: { editionId: string }) {
  const previousHash = useRef<string | null>(null);
  const preview = useQuery({
    queryKey: ["edition-preview", editionId],
    queryFn: async () => {
      const result = await getEditionPreview(editionId, previousHash.current);
      previousHash.current = result.preview_input_hash;
      return result;
    },
    retry: false,
  });

  if (preview.isPending)
    return <p role="status">Chargement de la prévisualisation…</p>;
  if (preview.isError) {
    return (
      <p className="error-message" role="alert">
        La prévisualisation est inaccessible : {String(preview.error)}
      </p>
    );
  }
  if (!preview.data) return null;

  const current = preview.data;
  return (
    <section
      className="edition-preview"
      aria-labelledby="edition-preview-heading"
    >
      <div className="edition-preview__heading">
        <div>
          <p className="eyebrow">Prévisualisation</p>
          <h2 id="edition-preview-heading">Vue bulletin</h2>
        </div>
        <button
          className="button button--secondary"
          type="button"
          onClick={() => void preview.refetch()}
        >
          Actualiser
        </button>
      </div>
      {current.stale ? (
        <p className="verification-warning" role="alert">
          Cette prévisualisation est obsolète : l’édition ou un article a
          changé. Actualisez-la avant téléchargement.
        </p>
      ) : null}
      <div className="edition-preview__document">
        <section aria-label="Métadonnées du bulletin">
          <h3>
            {current.document.edition.country} (
            {current.document.edition.country_code})
          </h3>
          <dl>
            <div>
              <dt>Période</dt>
              <dd>
                {current.document.edition.period_start} –{" "}
                {current.document.edition.period_end}
              </dd>
            </div>
            <div>
              <dt>TLP</dt>
              <dd>{current.document.edition.tlp}</dd>
            </div>
            <div>
              <dt>Langues</dt>
              <dd>{current.document.edition.languages.join(", ")}</dd>
            </div>
          </dl>
        </section>
        {current.document.publications
          .slice()
          .sort((left, right) => left.position - right.position)
          .map((publication) => (
            <section key={publication.position}>
              <h3>Article {String(publication.position).padStart(2, "0")}</h3>
              <PublicationDocumentView document={publication.document} />
            </section>
          ))}
      </div>
      <p>
        {current.stale ? (
          <button className="button" type="button" disabled>
            Télécharger le PDF de prévisualisation
          </button>
        ) : (
          <a
            className="button"
            href={editionPreviewPdfUrl(editionId, current.preview_input_hash)}
            download
          >
            Télécharger le PDF de prévisualisation
          </a>
        )}
      </p>
      <p className="edition-preview__provenance">
        Hash d’entrée : <code>{current.preview_input_hash.slice(0, 12)}…</code>{" "}
        · {current.artifacts.length} article
        {current.artifacts.length > 1 ? "s" : ""}
      </p>
    </section>
  );
}
