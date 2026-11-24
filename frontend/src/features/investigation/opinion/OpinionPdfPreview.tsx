import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import {
  AnalystOpinionPreviewError,
  fetchAnalystOpinionPreviewPdf,
} from "../../../api/analystOpinion";
import type { OpinionVersionV1 } from "./types";

const STALE_PREVIEW_CODES = new Set([
  "stale_version",
  "version_hash_mismatch",
  "stale_base",
  "base_identity_mismatch",
]);

type PreviewStatus = "generating" | "ready" | "stale" | "failed";
type ViewerStatus = "checking" | "available" | "fallback";

function isStalePreviewError(error: unknown): boolean {
  return (
    error instanceof AnalystOpinionPreviewError &&
    error.code !== null &&
    STALE_PREVIEW_CODES.has(error.code)
  );
}

function pdfSizeLabel(size: number): string {
  if (size < 1024) return size + " octets";
  if (size < 1024 * 1024) return (size / 1024).toFixed(1) + " Ko";
  return (size / (1024 * 1024)).toFixed(1) + " Mo";
}

export function OpinionPdfPreview({
  opinion,
  previewUrl,
}: {
  opinion: OpinionVersionV1;
  previewUrl: string;
}) {
  const basePublicationSha256 = opinion.provenance.base_publication_sha256;
  const previewQuery = useQuery({
    queryKey: [
      "analyst-opinion-preview-pdf",
      opinion.id,
      opinion.version,
      opinion.content_sha256,
      basePublicationSha256,
    ],
    queryFn: ({ signal }) =>
      fetchAnalystOpinionPreviewPdf(opinion, basePublicationSha256, signal),
    retry: false,
    staleTime: Infinity,
  });
  const fetchedPdf = previewQuery.data;
  const pdfIsOutOfDate =
    fetchedPdf !== undefined &&
    (fetchedPdf.opinionId !== opinion.id ||
      fetchedPdf.opinionVersion !== opinion.version ||
      fetchedPdf.opinionSha256 !== opinion.content_sha256 ||
      fetchedPdf.basePublicationSha256 !== basePublicationSha256);
  const status: PreviewStatus =
    pdfIsOutOfDate || isStalePreviewError(previewQuery.error)
      ? "stale"
      : previewQuery.isError
        ? "failed"
        : previewQuery.isPending || fetchedPdf === undefined
          ? "generating"
          : "ready";
  const currentPdf = status === "ready" ? fetchedPdf : undefined;
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [viewerStatus, setViewerStatus] = useState<ViewerStatus>("checking");

  useEffect(() => {
    if (currentPdf === undefined || typeof URL.createObjectURL !== "function") {
      setObjectUrl(null);
      return;
    }
    const nextUrl = URL.createObjectURL(currentPdf.blob);
    setObjectUrl(nextUrl);
    return () => URL.revokeObjectURL(nextUrl);
  }, [currentPdf]);

  useEffect(() => {
    if (objectUrl === null) {
      setViewerStatus("checking");
      return;
    }
    setViewerStatus("checking");
  }, [objectUrl]);

  useEffect(() => {
    if (objectUrl === null || viewerStatus !== "checking") return;
    const pdfViewerEnabled = (
      navigator as Navigator & { pdfViewerEnabled?: boolean }
    ).pdfViewerEnabled;
    if (pdfViewerEnabled === false) {
      setViewerStatus("fallback");
      return;
    }
    const timeoutId = window.setTimeout(() => {
      setViewerStatus("fallback");
    }, 2500);
    return () => window.clearTimeout(timeoutId);
  }, [objectUrl, viewerStatus]);

  function showFallback() {
    setViewerStatus("fallback");
  }

  const statusLabel: Record<PreviewStatus, string> = {
    generating: "Préparation du PDF",
    ready: "PDF prêt",
    stale: "PDF obsolète",
    failed: "PDF indisponible",
  };

  return (
    <section
      className="job-card opinion-pdf-preview"
      aria-labelledby="opinion-pdf-heading"
    >
      <header className="opinion-pdf-preview__header">
        <h3 id="opinion-pdf-heading">
          Aperçu PDF réel de l’avis v{opinion.version}
        </h3>
        <span
          className={"opinion-pdf-preview__status is-" + status}
          role="status"
          aria-live="polite"
        >
          {statusLabel[status]}
        </span>
      </header>
      <dl className="opinion-pdf-preview__metadata">
        <div>
          <dt>Version de l’avis</dt>
          <dd>v{opinion.version}</dd>
        </div>
        <div>
          <dt>Taille du PDF</dt>
          <dd>
            {currentPdf === undefined
              ? status === "generating"
                ? "En attente de génération"
                : "Indisponible"
              : pdfSizeLabel(currentPdf.blob.size)}
          </dd>
        </div>
      </dl>

      {status === "generating" ? (
        <p className="opinion-pdf-preview__loading" role="status">
          Chargement de l’aperçu PDF…
        </p>
      ) : null}
      {status === "stale" ? (
        <p className="opinion-pdf-preview__warning" role="alert">
          L’avis ou sa base de publication a changé. Actualisez l’avis avant
          d’ouvrir ce PDF.
        </p>
      ) : null}
      {status === "failed" ? (
        <div className="opinion-pdf-preview__failure" role="alert">
          <p>Le serveur n’a pas pu préparer l’aperçu PDF.</p>
          <button
            type="button"
            className="button button--secondary"
            onClick={() => void previewQuery.refetch()}
          >
            Réessayer
          </button>
        </div>
      ) : null}

      {currentPdf !== undefined &&
      objectUrl === null &&
      typeof URL.createObjectURL !== "function" ? (
        <div className="opinion-pdf-preview__fallback" role="status">
          <p>
            Le lecteur PDF intégré n’est pas disponible dans ce navigateur.
            Ouvrez le document ou téléchargez-le pour le consulter.
          </p>
          <p className="opinion-pdf-preview__actions">
            <a
              className="button button--secondary"
              href={previewUrl}
              target="_blank"
              rel="noreferrer"
            >
              Ouvrir le PDF
            </a>
            <a
              className="button button--secondary"
              href={previewUrl}
              download="avis-analyste.pdf"
            >
              Télécharger
            </a>
          </p>
        </div>
      ) : null}

      {currentPdf !== undefined &&
      objectUrl === null &&
      typeof URL.createObjectURL === "function" ? (
        <p className="opinion-pdf-preview__loading" role="status">
          Préparation du lecteur PDF…
        </p>
      ) : null}

      {currentPdf !== undefined && objectUrl !== null ? (
        viewerStatus === "fallback" ? (
          <div className="opinion-pdf-preview__fallback" role="status">
            <p>
              Ce navigateur n’a pas affiché le PDF intégré. Ouvrez le document
              dans un nouvel onglet ou téléchargez-le.
            </p>
            <p className="opinion-pdf-preview__actions">
              <a
                className="button button--secondary"
                href={previewUrl}
                target="_blank"
                rel="noreferrer"
              >
                Ouvrir le PDF
              </a>
              <a
                className="button button--secondary"
                href={previewUrl}
                download="avis-analyste.pdf"
              >
                Télécharger
              </a>
            </p>
          </div>
        ) : (
          <>
            {viewerStatus === "checking" ? (
              <p className="opinion-pdf-preview__loading" role="status">
                Vérification du lecteur PDF…
              </p>
            ) : null}
            <iframe
              data-testid="opinion-pdf-viewer"
              className={
                "opinion-pdf-preview__viewer" +
                (viewerStatus === "checking" ? " is-checking" : "")
              }
              title="Aperçu PDF réel de l’avis"
              src={objectUrl}
              onLoad={() => setViewerStatus("available")}
              onError={showFallback}
            >
              Votre navigateur ne peut pas afficher l’aperçu PDF.
            </iframe>
          </>
        )
      ) : null}
    </section>
  );
}
