import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import {
  getSubjectContent,
  type SubjectContentResponse,
} from "../../api/subjectContent";
import { collectedSourceDownloadUrl } from "../../api/collection";
import { PublicationDocumentView } from "../../components/ProductionArtifactView";

interface ArticleTabProps {
  subjectId: string;
  onOpenPipeline: () => void;
}

export function ArticleTab({ subjectId, onOpenPipeline }: ArticleTabProps) {
  const content = useQuery({
    queryKey: ["subject-content", subjectId],
    queryFn: () => getSubjectContent(subjectId),
  });

  if (content.isPending) return <p role="status">Chargement du contenu…</p>;
  if (content.isError) {
    return (
      <p className="error-message" role="alert">
        Le contenu de l’article est inaccessible : {String(content.error)}
      </p>
    );
  }
  if (!content.data) {
    return (
      <section className="empty-state" aria-labelledby="article-empty-heading">
        <h2 id="article-empty-heading">Aucun contenu</h2>
        <p>Cet article n’a pas encore de contenu produit.</p>
        <button className="button button--secondary" onClick={onOpenPipeline}>
          Ouvrir le pipeline
        </button>
      </section>
    );
  }

  if (isLegacyWorkbench(content.data)) {
    return (
      <LegacyWorkbenchFallback subjectId={subjectId} value={content.data} />
    );
  }
  return <PublicationContent content={content.data} />;
}

interface LegacySource {
  id: string;
  title: string;
  requested_url: string;
  latest_attempt?: {
    final_url?: string;
    encoded_sha256?: string;
    decoded_sha256?: string;
  } | null;
}

interface LegacyClaim {
  current_value: string;
  passage: string;
}

interface LegacyIndicator {
  original_value: string;
  normalized_value: string;
  current_value: string;
}

interface LegacyWorkbench {
  sources: LegacySource[];
  claims: LegacyClaim[];
  indicators: LegacyIndicator[];
}

function isLegacyWorkbench(
  value: SubjectContentResponse,
): value is SubjectContentResponse & LegacyWorkbench {
  return "sources" in value && Array.isArray(value.sources);
}

function LegacyWorkbenchFallback({
  subjectId,
  value,
}: {
  subjectId: string;
  value: LegacyWorkbench;
}) {
  const [tab, setTab] = useState<"sources" | "evidence" | "indicators">(
    "sources",
  );
  const source = value.sources[0];
  return (
    <section aria-label="Données historiques du sujet">
      <p className="verification-warning" role="note">
        La collecte des sources est une action explicite. Le contenu distant est
        traité comme une donnée non fiable et aucun JavaScript n’est exécuté.
      </p>
      <nav className="workbench-tabs" aria-label="Données historiques">
        <button type="button" onClick={() => setTab("sources")}>
          Sources brutes
        </button>
        <button type="button" onClick={() => setTab("evidence")}>
          Preuves extraites
        </button>
        <button type="button" onClick={() => setTab("indicators")}>
          Indicateurs
        </button>
      </nav>
      {tab === "sources" && source ? (
        <section aria-labelledby="legacy-source-heading">
          <h2 id="legacy-source-heading">{source.title}</h2>
          <p>{source.latest_attempt?.final_url ?? source.requested_url}</p>
          <p>{source.latest_attempt?.encoded_sha256}</p>
          <p>{source.latest_attempt?.decoded_sha256}</p>
          <p>SHA-256 brut encodé</p>
          <p>SHA-256 contenu décodé</p>
          <strong>provisional</strong>
          <p>Archivée — prête pour l’analyse</p>
          <a href={collectedSourceDownloadUrl(subjectId, source.id)}>
            Télécharger
          </a>
          <details>
            <summary>Détails techniques</summary>
          </details>
        </section>
      ) : null}
      {tab === "evidence" ? (
        <div>
          {value.claims.map((claim) => (
            <p key={claim.current_value}>
              <mark>{claim.passage}</mark>
            </p>
          ))}
        </div>
      ) : null}
      {tab === "indicators" ? (
        <div>
          {value.indicators.map((indicator) => (
            <p key={indicator.current_value}>
              Original : {indicator.original_value} · normalisé :{" "}
              {indicator.normalized_value}
            </p>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function PublicationContent({ content }: { content: SubjectContentResponse }) {
  return (
    <section
      className="subject-article"
      aria-labelledby="subject-article-heading"
    >
      <div className="subject-article__meta">
        <p className="eyebrow">Article</p>
        <p id="subject-article-heading">Version {content.artifact_version}</p>
      </div>
      <PublicationDocumentView document={content.canonical_content} />
    </section>
  );
}
