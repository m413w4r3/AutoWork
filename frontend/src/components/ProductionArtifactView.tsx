/**
 * Production Artifact Viewer
 * Displays references, extraction, synthesis, or brief artifacts
 */

import { useQuery } from "@tanstack/react-query";
import {
  getReferencesArtifact,
  getExtractionArtifact,
  getSynthesisArtifact,
  getBriefArtifact,
  type ArtifactResponse,
} from "../api/production";

interface ProductionArtifactViewProps {
  subjectId: string;
  stage: "references" | "extraction" | "synthesis" | "brief";
  onClose?: () => void;
}

const STAGE_LABELS: Record<string, string> = {
  references: "Références",
  extraction: "Extraction CTI",
  synthesis: "Synthèse",
  brief: "Aperçu de la brève",
};

const STATUS_LABELS: Record<string, string> = {
  verified: "Vérifié",
  stale: "Obsolète",
  needs_review: "À vérifier",
};

function getArtifactFetcher(
  stage: string,
): (subjectId: string) => Promise<ArtifactResponse> {
  switch (stage) {
    case "references":
      return getReferencesArtifact;
    case "extraction":
      return getExtractionArtifact;
    case "synthesis":
      return getSynthesisArtifact;
    case "brief":
      return getBriefArtifact;
    default:
      throw new Error(`Unknown stage: ${stage}`);
  }
}

export function ProductionArtifactView({
  subjectId,
  stage,
  onClose,
}: ProductionArtifactViewProps) {
  const fetcher = getArtifactFetcher(stage);

  const { data: artifact, isLoading, error } = useQuery({
    queryKey: ["production-artifact", subjectId, stage],
    queryFn: () => fetcher(subjectId),
  });

  if (isLoading) {
    return (
      <section className="artifact-view">
        <h2>{STAGE_LABELS[stage]}</h2>
        <p>Chargement du contenu…</p>
      </section>
    );
  }

  if (error) {
    return (
      <section className="artifact-view">
        <h2>{STAGE_LABELS[stage]}</h2>
        <p className="error-message">
          Impossible de charger l'artifact : {String(error)}
        </p>
      </section>
    );
  }

  if (!artifact) {
    return (
      <section className="artifact-view">
        <h2>{STAGE_LABELS[stage]}</h2>
        <p>Aucun contenu disponible pour cette étape.</p>
      </section>
    );
  }

  return (
    <section className="artifact-view">
      <div className="artifact-view__header">
        <div>
          <h2>{STAGE_LABELS[stage]}</h2>
          <p className="artifact-version">
            Version {artifact.version} •{" "}
            <span className={`badge is-${artifact.status}`}>
              {STATUS_LABELS[artifact.status] ?? artifact.status}
            </span>
          </p>
        </div>
        {onClose && (
          <button className="button button--secondary" onClick={onClose}>
            Fermer
          </button>
        )}
      </div>

      {artifact.metadata && Object.keys(artifact.metadata).length > 0 && (
        <div className="artifact-metadata">
          <details>
            <summary>Métadonnées</summary>
            <pre>{JSON.stringify(artifact.metadata, null, 2)}</pre>
          </details>
        </div>
      )}

      {artifact.rendered_content && (
        <div className="artifact-content">
          <div className="rendered-markdown">
            {/* Render as HTML/Markdown - would need a markdown parser */}
            <pre>{artifact.rendered_content}</pre>
          </div>
        </div>
      )}

      {artifact.canonical_content && (
        <div className="artifact-canonical">
          <details>
            <summary>Contenu canonique</summary>
            <pre>{JSON.stringify(artifact.canonical_content, null, 2)}</pre>
          </details>
        </div>
      )}

      {!artifact.rendered_content && !artifact.canonical_content && (
        <p>Aucun contenu à afficher.</p>
      )}

      <div className="artifact-meta">
        <p>ID de l'artifact : {artifact.artifact_id}</p>
      </div>
    </section>
  );
}
