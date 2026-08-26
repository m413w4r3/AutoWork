import { useQuery } from "@tanstack/react-query";
import {
  getReferencesArtifact,
  getExtractionArtifact,
  getSynthesisArtifact,
  getBriefArtifact,
  type ArtifactResponse,
  type BriefDocumentV1,
  type ExtractionDocumentV2,
  type ExtractionItemV2,
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

function isExtractionDocument(value: unknown): value is ExtractionDocumentV2 {
  return (
    typeof value === "object" &&
    value !== null &&
    "schema_version" in value &&
    value.schema_version === "2" &&
    "items" in value &&
    Array.isArray(value.items)
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
  email: "Adresses e-mail",
  hash: "Hachages",
};

const NETWORK_IOC_TYPES = new Set(["domain", "ip", "url", "email", "hash"]);
const FILE_CVE_TYPES = new Set(["filename", "filepath", "cve"]);
const RULE_TYPES = new Set(["yara_rule", "sigma_rule", "suricata_rule"]);

const TYPE_LABELS: Record<string, string> = {
  domain: "domaine",
  ip: "adresse IP",
  url: "URL",
  email: "adresse e-mail",
  hash: "hachage",
  filename: "fichier",
  filepath: "chemin de fichier",
  cve: "CVE",
  yara_rule: "règle YARA",
  sigma_rule: "règle Sigma",
  suricata_rule: "règle Suricata",
};

const ITEM_STATUS_LABELS: Record<string, string> = {
  confirmed_ioc: "IOC confirmé",
  contextual: "contextuel",
  excluded: "exclu",
  not_applicable: "hors périmètre",
};

interface GroupedExtractionItem extends ExtractionItemV2 {
  evidence_quotes: string[];
}

function unique(values: string[]): string[] {
  return [...new Set(values.filter(Boolean))];
}

function groupExtractionItems(
  items: ExtractionItemV2[],
): GroupedExtractionItem[] {
  const grouped = new Map<string, GroupedExtractionItem>();
  for (const item of items.filter(
    (entry) => entry.display_policy !== "hidden",
  )) {
    const identity = `${item.artifact_type ?? item.semantic_type}:${item.normalized_value ?? item.value.toLocaleLowerCase()}`;
    const existing = grouped.get(identity);
    if (!existing) {
      grouped.set(identity, {
        ...item,
        evidence_quotes: item.evidence_quote ? [item.evidence_quote] : [],
      });
      continue;
    }
    const sameStatus = existing.indicator_status === item.indicator_status;
    grouped.set(identity, {
      ...existing,
      indicator_status: sameStatus ? existing.indicator_status : "contextual",
      source_ids: unique([...existing.source_ids, ...item.source_ids]),
      evidence_quotes: unique([
        ...existing.evidence_quotes,
        ...(item.evidence_quote ? [item.evidence_quote] : []),
      ]),
    });
  }
  return [...grouped.values()];
}

function itemType(item: ExtractionItemV2): string {
  return item.artifact_type ?? item.semantic_type ?? item.category;
}

function EvidenceItem({ item }: { item: GroupedExtractionItem }) {
  const type = itemType(item);
  return (
    <li className="extraction-item">
      <code>{item.value}</code>
      <p className="extraction-item__context">{item.context}</p>
      <dl>
        <div>
          <dt>Type</dt>
          <dd>{TYPE_LABELS[type] ?? type}</dd>
        </div>
        <div>
          <dt>Statut</dt>
          <dd>
            {ITEM_STATUS_LABELS[item.indicator_status] ?? item.indicator_status}
          </dd>
        </div>
        <div>
          <dt>S#</dt>
          <dd>{item.source_ids.join(", ") || "—"}</dd>
        </div>
        <div>
          <dt>Sources</dt>
          <dd>{item.source_ids.length}</dd>
        </div>
      </dl>
      {item.evidence_quotes.map((quote) => (
        <blockquote key={quote}>{quote}</blockquote>
      ))}
    </li>
  );
}

function ExtractionSection({
  title,
  items,
}: {
  title: string;
  items: GroupedExtractionItem[];
}) {
  if (items.length === 0) return null;
  return (
    <section className="extraction-section">
      <h3>{title}</h3>
      <ul className="extraction-items">
        {items.map((item) => (
          <EvidenceItem
            key={`${itemType(item)}:${item.normalized_value ?? item.value}`}
            item={item}
          />
        ))}
      </ul>
    </section>
  );
}

function ExtractionPreview({ document }: { document: ExtractionDocumentV2 }) {
  const items = groupExtractionItems(document.items);
  const confirmedIocs = items.filter(
    (item) =>
      item.indicator_status === "confirmed_ioc" &&
      NETWORK_IOC_TYPES.has(item.artifact_type ?? ""),
  );
  const filesAndCves = items.filter((item) =>
    FILE_CVE_TYPES.has(item.artifact_type ?? ""),
  );
  const rules = items.filter((item) =>
    RULE_TYPES.has(item.artifact_type ?? ""),
  );
  const contextual = items.filter(
    (item) =>
      !confirmedIocs.includes(item) &&
      !filesAndCves.includes(item) &&
      !rules.includes(item),
  );

  return (
    <article className="extraction-preview">
      <ExtractionSection title="IOC confirmés" items={confirmedIocs} />
      <ExtractionSection title="Éléments contextuels" items={contextual} />
      <ExtractionSection title="Fichiers / CVE" items={filesAndCves} />
      <ExtractionSection title="Règles de détection" items={rules} />
      {document.uncertainties.length > 0 && (
        <section className="extraction-section">
          <h3>Points à vérifier</h3>
          <ul>
            {document.uncertainties.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </section>
      )}
    </article>
  );
}

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

      {stage !== "extraction" &&
        artifact.metadata &&
        Object.keys(artifact.metadata).length > 0 && (
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

      {stage === "extraction" &&
        isExtractionDocument(artifact.canonical_content) && (
          <ExtractionPreview document={artifact.canonical_content} />
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

      {stage !== "brief" &&
        stage !== "extraction" &&
        artifact.rendered_content && (
          <div className="artifact-content">
            <div className="rendered-markdown">
              <pre>{artifact.rendered_content}</pre>
            </div>
          </div>
        )}

      {stage !== "brief" &&
        stage !== "extraction" &&
        artifact.canonical_content && (
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
