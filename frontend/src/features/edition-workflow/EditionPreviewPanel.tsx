import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import {
  editionPreviewPdfUrl,
  fetchEditionPreviewPdf,
  getEditionCompositionPreview,
  getEditionPreview,
  pdfInspectionUrl,
  renderInputInspectionUrl,
} from "../../api/publication";
import type {
  PublicationDocument,
  PublicationDocumentArtifact,
} from "../../api/production";
import { PublicationDocumentView } from "../../components/ProductionArtifactView";

function publicationContent(
  document: PublicationDocumentArtifact,
): PublicationDocument {
  if (document.schema_version === "7") {
    return document.base_publication.base_publication;
  }
  if (document.schema_version === "6") return document.base_publication;
  return document;
}

function classificationKey(position: number, subjectId: string) {
  return `${position}:${subjectId}`;
}

export function EditionPreviewPanel({
  editionId,
  compositionPreview = false,
}: {
  editionId: string;
  compositionPreview?: boolean;
}) {
  const previousHash = useRef<string | null>(null);
  const [inspectionLinks, setInspectionLinks] = useState<{
    pdfUrl: string;
    inspectionId: string | null;
    pdfSha256: string | null;
  } | null>(null);
  const preview = useQuery({
    queryKey: [
      compositionPreview ? "edition-composition-preview" : "edition-preview",
      editionId,
    ],
    queryFn: async () => {
      const result = compositionPreview
        ? await getEditionCompositionPreview(editionId, previousHash.current)
        : await getEditionPreview(editionId, previousHash.current);
      previousHash.current = result.preview_input_hash;
      return result;
    },
    retry: false,
  });
  const inspectPdf = useMutation({
    mutationFn: (previewInputHash: string) =>
      fetchEditionPreviewPdf(editionId, previewInputHash),
    retry: false,
    onSuccess: (result) => {
      if (inspectionLinks) URL.revokeObjectURL(inspectionLinks.pdfUrl);
      setInspectionLinks({
        pdfUrl: URL.createObjectURL(result.content),
        inspectionId: result.inspectionId,
        pdfSha256: result.pdfSha256,
      });
    },
  });

  useEffect(
    () => () => {
      if (inspectionLinks) URL.revokeObjectURL(inspectionLinks.pdfUrl);
    },
    [inspectionLinks],
  );

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
  const classificationsByPublication = new Map(
    (current.classifications ?? []).map((classification) => [
      classificationKey(classification.position, classification.subject_id),
      classification,
    ]),
  );
  const classifiedPublications = current.document.publications
    .slice()
    .sort((left, right) => left.position - right.position)
    .flatMap((publication) => {
      const classification = classificationsByPublication.get(
        classificationKey(publication.position, publication.subject_id),
      );
      return classification ? [{ publication, classification }] : [];
    });
  if (classifiedPublications.length !== current.document.publications.length) {
    return (
      <p className="error-message" role="alert">
        La classification des publications est indisponible pour cette
        prévisualisation.
      </p>
    );
  }
  const articles = classifiedPublications.filter(
    ({ classification }) => classification.kind === "article",
  );
  const briefs = classifiedPublications.filter(
    ({ classification }) => classification.kind === "brief",
  );
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
      {current.partial ? (
        <aside className="composition-console__warning" role="status">
          Aperçu partiel : des sujets manquent ou sont reportés. Cet aperçu ne
          vaut pas acceptation finale.
          {current.missing_subject_ids.length > 0 ? (
            <p>Sujets manquants : {current.missing_subject_ids.join(", ")}</p>
          ) : null}
          {current.deferred_subject_ids.length > 0 ? (
            <p>Sujets reportés : {current.deferred_subject_ids.join(", ")}</p>
          ) : null}
        </aside>
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
        {articles.length > 0 ? (
          <section aria-label="ARTICLES">
            <h3>ARTICLES</h3>
            {articles.map(({ publication, classification }) => {
              return (
                <section key={classification.anchor} id={classification.anchor}>
                  <h4>Article {classification.number}</h4>
                  <PublicationDocumentView
                    document={publicationContent(publication.document)}
                    publicationKind={classification.kind}
                  />
                </section>
              );
            })}
          </section>
        ) : null}
        {briefs.length > 0 ? (
          <section aria-label="BRÈVES">
            <h3>BRÈVES</h3>
            {briefs.map(({ publication, classification }) => {
              return (
                <section key={classification.anchor} id={classification.anchor}>
                  <h4>Brève {classification.number}</h4>
                  <PublicationDocumentView
                    document={publicationContent(publication.document)}
                    publicationKind={classification.kind}
                  />
                </section>
              );
            })}
          </section>
        ) : null}
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
      {!current.stale ? (
        <div className="edition-preview__inspection-actions">
          <button
            type="button"
            className="button button--secondary"
            disabled={inspectPdf.isPending}
            onClick={() => inspectPdf.mutate(current.preview_input_hash)}
          >
            {inspectPdf.isPending
              ? "Chargement du PDF…"
              : "Préparer l’inspection Typst et PDF"}
          </button>
          {inspectPdf.error ? (
            <p role="alert" className="error-message">
              Le PDF de prévisualisation n’a pas pu être chargé :{" "}
              {String(inspectPdf.error)}
            </p>
          ) : null}
          {inspectionLinks ? (
            <nav aria-label="Inspections du PDF de prévisualisation">
              <a
                href={inspectionLinks.pdfUrl}
                download={`edition-${editionId}-preview.pdf`}
              >
                Télécharger le PDF chargé
              </a>
              {inspectionLinks.inspectionId ? (
                <a
                  href={renderInputInspectionUrl(
                    inspectionLinks.inspectionId,
                    true,
                  )}
                >
                  Inspecter les entrées Typst
                </a>
              ) : null}
              {inspectionLinks.pdfSha256 ? (
                <a href={pdfInspectionUrl(inspectionLinks.pdfSha256)}>
                  Inspecter le PDF
                </a>
              ) : null}
            </nav>
          ) : null}
        </div>
      ) : null}
      <p className="edition-preview__provenance">
        Hash d’entrée : <code>{current.preview_input_hash.slice(0, 12)}…</code>{" "}
        · {current.artifacts.length} publication
        {current.artifacts.length > 1 ? "s" : ""}
      </p>
    </section>
  );
}
