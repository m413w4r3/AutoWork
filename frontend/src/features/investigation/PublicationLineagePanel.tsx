import { useQuery } from "@tanstack/react-query";

import {
  analystOpinionPreviewPdfUrl,
  useAnalystOpinion,
} from "../../api/analystOpinion";
import {
  getPublicationArtifact,
  getPublicationArtifactPreview,
} from "../../api/production";

export function PublicationLineagePanel({
  investigationId,
  subjectId,
}: {
  investigationId: string;
  subjectId: string;
}) {
  const artifactQuery = useQuery({
    queryKey: ["publication-artifact", subjectId],
    queryFn: () => getPublicationArtifact(subjectId),
    enabled: Boolean(subjectId),
    retry: false,
  });
  const artifact = artifactQuery.data;
  const previewQuery = useQuery({
    queryKey: [
      "publication-artifact-preview",
      subjectId,
      artifact?.artifact_id ?? "none",
    ],
    queryFn: () =>
      getPublicationArtifactPreview(subjectId, artifact?.artifact_id ?? ""),
    enabled: Boolean(subjectId && artifact?.artifact_id),
    retry: false,
  });
  const opinionQuery = useAnalystOpinion(investigationId);
  const draft = opinionQuery.data?.draft;
  const provenance = opinionQuery.data?.provenance;
  const augmentation = artifact?.metadata.publication_augmentation;
  const nature =
    augmentation && typeof augmentation === "object"
      ? "Publication enrichie"
      : "Publication de base";
  const preview = previewQuery.data;
  const draftPdfUrl =
    draft && provenance
      ? analystOpinionPreviewPdfUrl(draft, provenance.base_publication_sha256)
      : null;

  return (
    <section className="job-card" aria-labelledby="pdf-lineage-heading">
      <h2 id="pdf-lineage-heading">Lignage des PDF</h2>
      <p>
        Comparaison de l’artefact publié et du brouillon d’avis courant. Chaque
        lien fixe les identités affichées ci-dessous.
      </p>
      {artifactQuery.isPending ? (
        <p role="status">Chargement de la publication…</p>
      ) : artifactQuery.isError ? (
        <p>Aucune publication lisible pour ce sujet.</p>
      ) : artifact ? (
        <section aria-labelledby="published-pdf-heading">
          <h3 id="published-pdf-heading">Publié</h3>
          <p>
            <strong>{nature}</strong> · artefact v{artifact.version} ·{" "}
            <code>{artifact.artifact_id}</code>
          </p>
          <p>
            SHA-256 · <code>{artifact.canonical_sha256 ?? "inconnu"}</code>
          </p>
          {preview?.pdf_url ? (
            <p>
              <a href={preview.pdf_url} download>
                Ouvrir le PDF publié v{preview.artifact_version}
              </a>{" "}
              <span role="status">
                {preview.status === "STALE" ? "historique" : "actuel"}
              </span>
            </p>
          ) : previewQuery.isPending ? (
            <p role="status">Préparation du lien versionné…</p>
          ) : (
            <p>
              PDF publié indisponible : {preview?.error_code ?? "non rendu"}.
            </p>
          )}
        </section>
      ) : null}

      <section aria-labelledby="draft-pdf-heading">
        <h3 id="draft-pdf-heading">Brouillon courant</h3>
        {opinionQuery.isPending ? (
          <p role="status">Chargement de l’avis…</p>
        ) : draft && provenance ? (
          <>
            <p>
              <strong>Aperçu d’avis draft</strong> · avis v{draft.version} ·{" "}
              <code>{draft.id}</code>
            </p>
            <p>
              SHA-256 · <code>{draft.content_sha256}</code>
            </p>
            <p>
              Publication de base épinglée ·{" "}
              <code>{provenance.base_publication_artifact_id}</code> · SHA-256 ·{" "}
              <code>{provenance.base_publication_sha256}</code>
            </p>
            {draftPdfUrl ? (
              <p>
                <a href={draftPdfUrl} download>
                  Prévisualiser le brouillon v{draft.version}
                </a>{" "}
                <span>actuel · non approuvé</span>
              </p>
            ) : null}
          </>
        ) : opinionQuery.isError ? (
          <p>L’avis courant est indisponible.</p>
        ) : (
          <p>Aucun brouillon d’avis courant à comparer.</p>
        )}
      </section>
    </section>
  );
}
