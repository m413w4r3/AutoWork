import { useEffect, useState } from "react";

import type {
  ArtifactResponse,
  PublicationArtifactPreview,
} from "../../api/production";

const PUBLICATION_PREVIEW_STATUS_LABELS: Record<string, string> = {
  IN_PROGRESS: "Rendu en cours",
  READY: "PDF prêt",
  FAILED: "Échec du rendu",
  STALE: "Obsolète",
};

export function PublicationPdfPanel({
  artifact,
  preview,
  pdf,
  loading,
  error,
}: {
  artifact: ArtifactResponse;
  preview: PublicationArtifactPreview | undefined;
  pdf: Blob | undefined;
  loading: boolean;
  error: Error | null;
}) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!pdf) {
      setObjectUrl(null);
      return;
    }
    const nextUrl = URL.createObjectURL(pdf);
    setObjectUrl(nextUrl);
    return () => URL.revokeObjectURL(nextUrl);
  }, [pdf]);

  const stale =
    preview?.status === "STALE" ||
    (preview !== undefined && preview.artifact_id !== artifact.artifact_id) ||
    (preview !== undefined && preview.artifact_version !== artifact.version) ||
    (preview !== undefined &&
      preview.current_artifact_id !== preview.artifact_id);
  const currentId = preview?.current_artifact_id ?? artifact.artifact_id;

  return (
    <section
      className="publication-pdf-preview"
      aria-label="Aperçu PDF compilé"
    >
      <div className="publication-pdf-preview__header">
        <h3>PDF compilé</h3>
        <span
          className={`badge is-${stale ? "stale" : (preview?.status ?? "loading").toLowerCase()}`}
          role="status"
        >
          {stale
            ? PUBLICATION_PREVIEW_STATUS_LABELS.STALE
            : (preview && PUBLICATION_PREVIEW_STATUS_LABELS[preview.status]) ||
              (loading ? "Préparation du PDF" : "État indisponible")}
        </span>
      </div>
      {preview ? (
        <dl className="publication-pdf-preview__metadata">
          <div>
            <dt>Artifact</dt>
            <dd>
              {preview.artifact_id} · version {preview.artifact_version}
            </dd>
          </div>
          <div>
            <dt>Identité du rendu</dt>
            <dd>{preview.render_identity ?? "En attente de calcul"}</dd>
          </div>
          <div>
            <dt>Version publiée</dt>
            <dd>
              {preview.render_disposition === "ACCEPTED_VERSION"
                ? `Version acceptée de l’édition ${preview.published_edition_version ?? ""}`
                : "Rendu explicite de cet artifact"}
            </dd>
          </div>
        </dl>
      ) : null}
      {stale ? (
        <p className="publication-pdf-preview__warning" role="alert">
          Ce PDF correspond à l’artifact{" "}
          {preview?.artifact_id ?? artifact.artifact_id}, version{" "}
          {preview?.artifact_version ?? artifact.version}. L’artifact courant
          est {currentId}, version{" "}
          {preview?.current_artifact_version ?? artifact.version}; le PDF
          obsolète n’est pas affiché avec cette proposition.
        </p>
      ) : null}
      {preview?.status === "IN_PROGRESS" ? (
        <p aria-live="polite">
          Le rendu Typst est en cours. Cette vue se mettra à jour
          automatiquement.
        </p>
      ) : null}
      {preview?.status === "FAILED" ? (
        <p className="error-message" role="alert">
          {preview.error_message ??
            preview.error_code ??
            "Le PDF n’a pas pu être compilé."}
        </p>
      ) : null}
      {error ? (
        <p className="error-message" role="alert">
          {String(error)}
        </p>
      ) : null}
      {preview?.status === "READY" && !stale && objectUrl ? (
        <object
          aria-label={`PDF de l’artifact ${preview.artifact_id}, version ${preview.artifact_version}`}
          className="publication-pdf-preview__viewer"
          data={objectUrl}
          data-testid="publication-pdf-viewer"
          type="application/pdf"
        >
          <p>
            Le lecteur PDF du navigateur n’est pas disponible.{" "}
            <a href={objectUrl} rel="noreferrer" target="_blank">
              Ouvrir le PDF
            </a>
          </p>
        </object>
      ) : null}
    </section>
  );
}
