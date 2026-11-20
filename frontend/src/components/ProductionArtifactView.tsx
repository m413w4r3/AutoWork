import { useQuery } from "@tanstack/react-query";

import {
  getEditorialEnrichmentArtifact,
  getExtractionArtifact,
  getPublicationArtifact,
  getPublicationArtifactPreview,
  getPublicationPreviewPdf,
  getReferencesArtifact,
  getRelevanceProjectionArtifact,
  getSynthesisArtifact,
  isProductionExtractionV1,
  isProductionSynthesisV1,
  type ArtifactResponse,
} from "../api/production";
import { PublicationDiagnosticsPanel } from "../features/publication/PublicationDiagnosticsPanel";
import { PublicationDocumentView } from "../features/publication/PublicationDocumentView";
import { PublicationPdfPanel } from "../features/publication/PublicationPdfPanel";
import { ProductionReferenceCorpusView } from "../features/publication/ProductionReferenceCorpusView";
import {
  ExtractionPreview,
  ProductionExtractionPreview,
} from "../features/publication/ProductionExtractionPreview";
import { ProductionSynthesisView } from "../features/publication/ProductionSynthesisView";
import {
  publicationDocumentBody,
  recordValue,
  relevanceProjectionForDisplay,
} from "../features/publication/publicationArtifactDisplay";
import {
  isExtractionDocument,
  isPublicationDocument,
  isProductionReferenceCorpus,
} from "../features/publication/publicationArtifactSchemas";
import { TargetedEditorialRevisionPanel } from "./TargetedEditorialRevisionPanel";

export { PublicationDocumentView };

interface ProductionArtifactViewProps {
  subjectId: string;
  stage:
    | "references"
    | "extraction"
    | "relevance_projection"
    | "synthesis"
    | "editorial_enrichment"
    | "publication";
  onClose?: () => void;
}

const STAGE_LABELS: Record<string, string> = {
  references: "Références",
  extraction: "Extraction CTI",
  relevance_projection: "Périmètre des preuves",
  synthesis: "Synthèse",
  editorial_enrichment: "Enrichissement éditorial",
  publication: "Aperçu de la publication",
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
    case "relevance_projection":
      return getRelevanceProjectionArtifact;
    case "synthesis":
      return getSynthesisArtifact;
    case "editorial_enrichment":
      return getEditorialEnrichmentArtifact;
    case "publication":
      return getPublicationArtifact;
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

  const {
    data: artifact,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["production-artifact", subjectId, stage],
    queryFn: () => fetcher(subjectId),
    refetchInterval: stage === "publication" ? 2500 : false,
  });

  const publicationEnvelope =
    stage === "publication" &&
    artifact &&
    isPublicationDocument(artifact.canonical_content)
      ? artifact.canonical_content
      : null;
  const publicationDocument = publicationEnvelope
    ? publicationEnvelope.schema_version === "8" ||
      publicationEnvelope.schema_version === "9"
      ? publicationDocumentBody(publicationEnvelope.base_publication)
      : publicationDocumentBody(publicationEnvelope)
    : null;
  const publicationKind =
    publicationEnvelope?.schema_version === "7"
      ? publicationEnvelope.publication_kind
      : publicationEnvelope?.schema_version === "8" ||
          publicationEnvelope?.schema_version === "9"
        ? publicationEnvelope.base_publication.publication_kind
        : null;
  const publicationInputArtifacts = recordValue(
    artifact?.metadata.input_artifacts,
  );
  const expectedEditorialEnrichmentArtifactId =
    publicationInputArtifacts?.editorial_enrichment_artifact_id;
  const editorialEnrichmentArtifactQuery = useQuery({
    queryKey: [
      "production-artifact",
      subjectId,
      "editorial_enrichment",
      expectedEditorialEnrichmentArtifactId,
    ],
    queryFn: () => getEditorialEnrichmentArtifact(subjectId),
    enabled:
      publicationDocument !== null &&
      typeof expectedEditorialEnrichmentArtifactId === "string",
  });
  const editorialEnrichmentArtifact =
    typeof expectedEditorialEnrichmentArtifactId === "string" &&
    editorialEnrichmentArtifactQuery.data?.artifact_id ===
      expectedEditorialEnrichmentArtifactId
      ? editorialEnrichmentArtifactQuery.data
      : undefined;
  const publicationPreviewQuery = useQuery({
    queryKey: [
      "publication-preview",
      subjectId,
      artifact?.artifact_id,
      artifact?.version,
    ],
    queryFn: () => {
      if (!artifact?.artifact_id) {
        throw new Error("Aucun artifact de publication à rendre.");
      }
      return getPublicationArtifactPreview(subjectId, artifact.artifact_id);
    },
    enabled: publicationDocument !== null,
    refetchInterval: (query) =>
      query.state.data?.status === "IN_PROGRESS" ? 1500 : false,
  });
  const publicationPreview = publicationPreviewQuery.data;
  const publicationPdfQuery = useQuery({
    queryKey: [
      "publication-preview-pdf",
      subjectId,
      artifact?.artifact_id,
      publicationPreview?.render_identity,
    ],
    queryFn: () => {
      if (!publicationPreview) {
        throw new Error("Les métadonnées du PDF sont indisponibles.");
      }
      return getPublicationPreviewPdf(publicationPreview);
    },
    enabled:
      publicationDocument !== null &&
      publicationPreview?.status === "READY" &&
      publicationPreview.artifact_id === artifact?.artifact_id,
    retry: false,
  });

  // Canonical evidence presentation needs the exact source metadata, never a
  // positional or textual match against the synthesis narrative.
  const { data: extractionArtifact } = useQuery({
    queryKey: ["production-artifact", subjectId, "extraction"],
    queryFn: () => getExtractionArtifact(subjectId),
    enabled: stage === "synthesis",
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

  const renderedContent = artifact.rendered_content ?? null;

  const referencesCorpus =
    stage === "references" &&
    isProductionReferenceCorpus(artifact.canonical_content)
      ? artifact.canonical_content
      : null;

  const canonicalContent: unknown = artifact.canonical_content;
  const synthesisDocument =
    stage === "synthesis" && isProductionSynthesisV1(canonicalContent)
      ? canonicalContent
      : null;

  const extractionCanonical: unknown = extractionArtifact?.canonical_content;
  const extractionDocument = isProductionExtractionV1(extractionCanonical)
    ? extractionCanonical
    : null;

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
          {artifact.reused ? (
            <p className="production-reuse-notice">
              Réutilisé depuis un calcul précédent
              {artifact.reused_from_artifact_id
                ? ` · artifact source : ${artifact.reused_from_artifact_id}`
                : ""}
              {artifact.reused_from_created_at
                ? ` · calcul original : ${new Date(artifact.reused_from_created_at).toLocaleString()}`
                : ""}
            </p>
          ) : null}
        </div>
        {onClose && (
          <button className="button button--secondary" onClick={onClose}>
            Fermer
          </button>
        )}
      </div>

      {stage !== "extraction" &&
        stage !== "publication" &&
        artifact.metadata &&
        Object.keys(artifact.metadata).length > 0 && (
          <div className="artifact-metadata">
            <details>
              <summary>Métadonnées</summary>
              <pre>{JSON.stringify(artifact.metadata, null, 2)}</pre>
            </details>
          </div>
        )}

      {stage === "publication" && publicationDocument && (
        <>
          <PublicationPdfPanel
            artifact={artifact}
            preview={publicationPreview}
            pdf={publicationPdfQuery.data}
            loading={publicationPreviewQuery.isLoading}
            error={publicationPreviewQuery.error ?? publicationPdfQuery.error}
          />
          <PublicationDiagnosticsPanel
            diagnostics={artifact.metadata}
            document={publicationDocument}
            editorialEnrichment={editorialEnrichmentArtifact}
          />
          <PublicationDocumentView
            document={publicationDocument}
            publicationKind={publicationKind}
          />
        </>
      )}

      {stage === "editorial_enrichment" && (
        <TargetedEditorialRevisionPanel
          subjectId={subjectId}
          stage="editorial_enrichment"
          artifact={artifact}
        />
      )}

      {stage === "extraction" &&
        isProductionExtractionV1(artifact.canonical_content) && (
          <ProductionExtractionPreview document={artifact.canonical_content} />
        )}

      {stage === "extraction" &&
        !isProductionExtractionV1(artifact.canonical_content) &&
        isExtractionDocument(artifact.canonical_content) && (
          <ExtractionPreview document={artifact.canonical_content} />
        )}

      {stage === "relevance_projection" && artifact.canonical_content ? (
        <section
          className="artifact-content"
          aria-label="Décisions de périmètre"
        >
          <p>
            Les références indexées sont développées pour faciliter la lecture.
            Les décisions ambiguës restent visibles ici avec leur motif et les
            preuves qui les soutiennent.
          </p>
          <pre>
            {JSON.stringify(
              relevanceProjectionForDisplay(artifact.canonical_content),
              null,
              2,
            )}
          </pre>
        </section>
      ) : null}

      {referencesCorpus ? (
        <ProductionReferenceCorpusView corpus={referencesCorpus} />
      ) : null}

      {synthesisDocument ? (
        <>
          <ProductionSynthesisView
            document={synthesisDocument}
            extraction={extractionDocument}
            metadata={artifact.metadata}
          />
          <TargetedEditorialRevisionPanel
            subjectId={subjectId}
            stage="synthesis"
            artifact={artifact}
          />
        </>
      ) : null}

      {synthesisDocument && renderedContent ? (
        <details className="artifact-rendered-preview">
          <summary>Aperçu Markdown (projection temporaire)</summary>
          <pre>{renderedContent}</pre>
        </details>
      ) : null}

      {stage !== "publication" &&
        stage !== "extraction" &&
        stage !== "relevance_projection" &&
        stage !== "editorial_enrichment" &&
        !referencesCorpus &&
        !synthesisDocument &&
        renderedContent && (
          <div className="artifact-content">
            <div className="rendered-markdown">
              <pre>{renderedContent}</pre>
            </div>
          </div>
        )}

      {stage !== "publication" &&
        stage !== "extraction" &&
        stage !== "relevance_projection" &&
        stage !== "editorial_enrichment" &&
        !referencesCorpus &&
        !synthesisDocument &&
        artifact.canonical_content && (
          <div className="artifact-canonical">
            <details>
              <summary>Contenu canonique</summary>
              <pre>{JSON.stringify(artifact.canonical_content, null, 2)}</pre>
            </details>
          </div>
        )}

      {!renderedContent && !artifact.canonical_content && (
        <p>Aucun contenu à afficher.</p>
      )}

      <div className="artifact-meta">
        <p>ID de l'artifact : {artifact.artifact_id}</p>
      </div>
    </section>
  );
}
