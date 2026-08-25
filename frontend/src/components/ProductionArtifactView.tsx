import { useQuery } from "@tanstack/react-query";
import {
  getReferencesArtifact,
  getExtractionArtifact,
  getSynthesisArtifact,
  getBriefArtifact,
  type ArtifactResponse,
  type BriefDocumentV1,
  type RichSpan,
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

function isBriefDocument(value: unknown): value is BriefDocumentV1 {
  return (
    typeof value === "object" &&
    value !== null &&
    "schema_version" in value &&
    value.schema_version === "1" &&
    "timeline" in value &&
    Array.isArray(value.timeline)
  );
}

function RichText({ spans }: { spans: RichSpan[] }) {
  return spans.map((span, index) => {
    if (span.kind === "citation") {
      return (
        <sup key={index} className="semantic-citation">
          {span.source_ids.join(", ")}
        </sup>
      );
    }
    if (span.kind === "actor" || span.kind === "malware") {
      return <strong key={index}>{span.text}</strong>;
    }
    if (span.kind === "emphasis") {
      return <em key={index}>{span.text}</em>;
    }
    return (
      <span key={index} className={`semantic-${span.kind}`}>
        {span.text}
      </span>
    );
  });
}

const IOC_LABELS: Record<string, string> = {
  ip: "Adresses IP",
  domain: "Noms de domaine",
  url: "URL",
  hash: "Fichiers",
};

function BriefPreview({ document }: { document: BriefDocumentV1 }) {
  const visibleGroups = document.indicators.filter(
    (group) => IOC_LABELS[group.artifact_type] && group.values.length > 0,
  );
  return (
    <article className="brief-preview">
      <h3>{document.title}</h3>
      <div className="brief-preview__timeline">
        {document.timeline.map((entry, index) => (
          <p key={index}>
            {entry.date && (
              <strong className="semantic-date">
                {new Intl.DateTimeFormat("fr-FR", { dateStyle: "long" }).format(
                  new Date(`${entry.date}T00:00:00`),
                )}
                {" : "}
              </strong>
            )}
            <RichText spans={entry.content} />
          </p>
        ))}
      </div>
      <h4>Synthèse</h4>
      {document.synthesis.map((paragraph, index) => (
        <p key={index}>
          <RichText spans={paragraph} />
        </p>
      ))}
      {visibleGroups.length > 0 && (
        <section className="brief-preview__indicators">
          <h4>IOC</h4>
          {visibleGroups.map((group) => (
            <div key={group.artifact_type}>
              <h5>{IOC_LABELS[group.artifact_type]}</h5>
              <ul>
                {group.values.map((indicator) => (
                  <li key={indicator.normalized_value}>
                    <code>{indicator.normalized_value}</code>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </section>
      )}
    </article>
  );
}

export function ProductionArtifactView({
  subjectId,
  stage,
  onClose,
}: ProductionArtifactViewProps) {
  const fetcher = getArtifactFetcher(stage);

  const {
    data: artifact,
    isLoading,
    error,
  } = useQuery({
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

      {stage === "brief" && isBriefDocument(artifact.canonical_content) && (
        <BriefPreview document={artifact.canonical_content} />
      )}

      {stage === "brief" && artifact.rendered_content && (
        <p>
          <a
            className="button button--secondary"
            download="breve-pandoc.md"
            href={`data:text/markdown;charset=utf-8,${encodeURIComponent(artifact.rendered_content)}`}
          >
            Télécharger le Markdown Pandoc
          </a>
        </p>
      )}

      {stage !== "brief" && artifact.rendered_content && (
        <div className="artifact-content">
          <div className="rendered-markdown">
            <pre>{artifact.rendered_content}</pre>
          </div>
        </div>
      )}

      {stage !== "brief" && artifact.canonical_content && (
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
