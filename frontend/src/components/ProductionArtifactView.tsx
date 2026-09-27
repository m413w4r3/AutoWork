import { useQuery } from "@tanstack/react-query";
import {
  getReferencesArtifact,
  getExtractionArtifact,
  getSynthesisArtifact,
  getPublicationArtifact,
  isProductionExtractionV1,
  type ArtifactResponse,
  type ProductionExtractionArtifactV1,
  type ProductionExtractionEventV1,
  type ProductionExtractionFactV1,
  type ProductionExtractionRuleV1,
  type ProductionExtractionV1,
  type ProductionSourceExtractionV1,
  type PublicationDocument,
  type ExtractionDocumentV2,
  type ExtractionItemV2,
  type RichSpan,
} from "../api/production";

interface ProductionArtifactViewProps {
  subjectId: string;
  stage: "references" | "extraction" | "synthesis" | "publication";
  onClose?: () => void;
}

const STAGE_LABELS: Record<string, string> = {
  references: "Références",
  extraction: "Extraction CTI",
  synthesis: "Synthèse",
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
    case "synthesis":
      return getSynthesisArtifact;
    case "publication":
      return getPublicationArtifact;
    default:
      throw new Error(`Unknown stage: ${stage}`);
  }
}

function isPublicationDocument(value: unknown): value is PublicationDocument {
  return (
    typeof value === "object" &&
    value !== null &&
    "schema_version" in value &&
    (value.schema_version === "1" || value.schema_version === "2") &&
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

type ProductionReferenceTier = "core" | "supporting" | "technical";
type ProductionReferenceKind = "publication" | "technical_resource";

interface ProductionReferenceSource {
  canonical_url: string;
  title: string | null;
  tier: ProductionReferenceTier;
  role: string;
  kind: ProductionReferenceKind;
  publisher: string | null;
  published_at: string | null;
  collection_state: string;
  eligible_for_extraction: boolean;
}

interface ProductionReferenceCorpus {
  schema_version: 1;
  sources: ProductionReferenceSource[];
}

const REFERENCE_TIER_LABELS: Record<ProductionReferenceTier, string> = {
  core: "Core",
  supporting: "Référence complémentaire",
  technical: "Ressource technique",
};

const REFERENCE_ROLE_LABELS: Record<string, string> = {
  primary: "Primaire",
  independent: "Indépendante",
  relay: "Relais",
  aggregator: "Agrégateur",
  social: "Réseau social",
  unknown: "Inconnu",
};

const REFERENCE_KIND_LABELS: Record<ProductionReferenceKind, string> = {
  publication: "Publication",
  technical_resource: "Ressource technique",
};

const COLLECTION_STATE_LABELS: Record<string, string> = {
  archived: "Archivée",
  extracted: "Extraite",
  completed: "Terminée",
  unavailable: "Indisponible",
  blocked: "Bloquée",
  failed_retryable: "Échec — nouvel essai possible",
  failed_terminal: "Échec définitif",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isProductionReferenceSource(
  value: unknown,
): value is ProductionReferenceSource {
  return (
    isRecord(value) &&
    typeof value.canonical_url === "string" &&
    (typeof value.title === "string" || value.title === null) &&
    (value.tier === "core" ||
      value.tier === "supporting" ||
      value.tier === "technical") &&
    typeof value.role === "string" &&
    (value.kind === "publication" || value.kind === "technical_resource") &&
    (typeof value.publisher === "string" || value.publisher === null) &&
    (typeof value.published_at === "string" || value.published_at === null) &&
    typeof value.collection_state === "string" &&
    typeof value.eligible_for_extraction === "boolean"
  );
}

function isProductionReferenceCorpus(
  value: unknown,
): value is ProductionReferenceCorpus {
  return (
    isRecord(value) &&
    value.schema_version === 1 &&
    Array.isArray(value.sources) &&
    value.sources.every(isProductionReferenceSource)
  );
}

function ProductionReferenceCorpusView({
  corpus,
}: {
  corpus: ProductionReferenceCorpus;
}) {
  return (
    <section className="production-reference-corpus">
      <h3>Corpus de références</h3>
      {corpus.sources.length === 0 ? (
        <p>Aucune source dans le corpus.</p>
      ) : (
        <ul>
          {corpus.sources.map((source) => (
            <li key={source.canonical_url}>
              <article>
                <h4>{source.title ?? source.canonical_url}</h4>
                <dl>
                  <dt>Source</dt>
                  <dd>
                    <a href={source.canonical_url}>{source.canonical_url}</a>
                  </dd>
                  <dt>Provenance</dt>
                  <dd>{REFERENCE_TIER_LABELS[source.tier]}</dd>
                  <dt>Rôle</dt>
                  <dd>{REFERENCE_ROLE_LABELS[source.role] ?? source.role}</dd>
                  <dt>Type</dt>
                  <dd>{REFERENCE_KIND_LABELS[source.kind]}</dd>
                  <dt>Éditeur</dt>
                  <dd>{source.publisher ?? "Non renseigné"}</dd>
                  <dt>Date de publication</dt>
                  <dd>{source.published_at ?? "Non renseignée"}</dd>
                  <dt>État de collecte</dt>
                  <dd>
                    {COLLECTION_STATE_LABELS[source.collection_state] ??
                      source.collection_state}
                  </dd>
                  <dt>Éligibilité</dt>
                  <dd>
                    {source.eligible_for_extraction
                      ? "Éligible à l’extraction"
                      : "Non éligible à l’extraction"}
                  </dd>
                </dl>
              </article>
            </li>
          ))}
        </ul>
      )}
    </section>
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

function ProductionExtractionSourceCard({
  source,
}: {
  source: ProductionSourceExtractionV1;
}) {
  return (
    <li>
      <article className="extraction-source">
        <h4>{source.title?.trim() || source.canonical_url}</h4>
        <a href={source.canonical_url}>{source.canonical_url}</a>
        <dl>
          <div>
            <dt>Tier</dt>
            <dd>{REFERENCE_TIER_LABELS[source.tier]}</dd>
          </div>
          <div>
            <dt>Type de source</dt>
            <dd>{REFERENCE_KIND_LABELS[source.kind]}</dd>
          </div>
          <div>
            <dt>Rôle</dt>
            <dd>{REFERENCE_ROLE_LABELS[source.role] ?? source.role}</dd>
          </div>
          <div>
            <dt>Profil</dt>
            <dd>{source.profile === "full" ? "FULL" : "IOC_RULES"}</dd>
          </div>
          <div>
            <dt>SHA-256</dt>
            <dd>{source.content_sha256.slice(0, 12)}…</dd>
          </div>
          <div>
            <dt>Résultat</dt>
            <dd>
              {source.reuse_state === "reused" ? "Réutilisée" : "Calculée"}
            </dd>
          </div>
          <div>
            <dt>Checkpoint</dt>
            <dd>{source.checkpoint_id}</dd>
          </div>
          <div>
            <dt>Faits</dt>
            <dd>{source.facts.length}</dd>
          </div>
          <div>
            <dt>Événements</dt>
            <dd>{source.events.length}</dd>
          </div>
          <div>
            <dt>IOC / artefacts</dt>
            <dd>{source.indicators.length}</dd>
          </div>
          <div>
            <dt>Règles</dt>
            <dd>{source.rules.length}</dd>
          </div>
        </dl>
      </article>
    </li>
  );
}

function ExtractionSourceProvenance({
  source,
}: {
  source: ProductionSourceExtractionV1;
}) {
  return (
    <p className="extraction-provenance">
      Source : {source.source_document_id} ·{" "}
      <a href={source.canonical_url}>
        {source.title?.trim() || source.canonical_url}
      </a>
    </p>
  );
}

function ExtractionEvidence({
  item,
  source,
}: {
  item:
    | ProductionExtractionArtifactV1
    | ProductionExtractionEventV1
    | ProductionExtractionFactV1
    | ProductionExtractionRuleV1;
  source: ProductionSourceExtractionV1;
}) {
  return (
    <>
      <ExtractionSourceProvenance source={source} />
      <blockquote>{item.evidence_quote}</blockquote>
      <p className="extraction-evidence-basis">
        Base de preuve : {item.evidence_basis}
      </p>
    </>
  );
}

function ProductionExtractionPreview({
  document,
}: {
  document: ProductionExtractionV1;
}) {
  const fullSources = document.sources.filter(
    (source) => source.profile === "full",
  );
  const iocSources = document.sources.filter(
    (source) => source.profile === "ioc_rules",
  );
  const events = fullSources.flatMap((source) =>
    source.events.map((item) => ({ item, source })),
  );
  const facts = fullSources.flatMap((source) =>
    source.facts.map((item) => ({ item, source })),
  );
  const indicators = document.sources.flatMap((source) =>
    source.indicators.map((item) => ({ item, source })),
  );
  const rules = document.sources.flatMap((source) =>
    source.rules.map((item) => ({ item, source })),
  );
  const uncertainties = document.sources.flatMap((source) =>
    source.uncertainties.map((text, index) => ({ source, text, index })),
  );

  return (
    <article className="extraction-preview extraction-preview--canonical">
      <section className="extraction-section">
        <h3>Sources FULL</h3>
        {fullSources.length > 0 ? (
          <ul className="extraction-sources">
            {fullSources.map((source) => (
              <ProductionExtractionSourceCard
                key={source.source_document_id}
                source={source}
              />
            ))}
          </ul>
        ) : (
          <p>Aucune source FULL.</p>
        )}
      </section>

      <section className="extraction-section">
        <h3>Sources IOC_RULES</h3>
        {iocSources.length > 0 ? (
          <ul className="extraction-sources">
            {iocSources.map((source) => (
              <ProductionExtractionSourceCard
                key={source.source_document_id}
                source={source}
              />
            ))}
          </ul>
        ) : (
          <p>Aucune source IOC_RULES.</p>
        )}
      </section>

      <section className="extraction-section">
        <h3>Chronologie</h3>
        {events.length > 0 ? (
          <ul>
            {events.map(({ item, source }, index) => (
              <li key={`${source.source_document_id}-event-${index}`}>
                <p>
                  {item.event_date ? (
                    <strong>{item.event_date} — </strong>
                  ) : null}
                  {item.date_text ? <strong>{item.date_text} — </strong> : null}
                  {item.text}
                </p>
                {item.context ? <p>{item.context}</p> : null}
                <ExtractionEvidence item={item} source={source} />
              </li>
            ))}
          </ul>
        ) : (
          <p>Aucun événement extrait.</p>
        )}
      </section>

      <section className="extraction-section">
        <h3>Faits</h3>
        {facts.length > 0 ? (
          <ul>
            {facts.map(({ item, source }, index) => (
              <li key={`${source.source_document_id}-fact-${index}`}>
                <strong>{item.category} : </strong>
                {item.value}
                {item.context ? <p>{item.context}</p> : null}
                <ExtractionEvidence item={item} source={source} />
              </li>
            ))}
          </ul>
        ) : (
          <p>Aucun fait extrait.</p>
        )}
      </section>

      <section className="extraction-section">
        <h3>IOC / artefacts</h3>
        {indicators.length > 0 ? (
          <ul>
            {indicators.map(({ item, source }, index) => (
              <li key={`${source.source_document_id}-indicator-${index}`}>
                <strong>{item.artifact_type} : </strong>
                <code>{item.value}</code>
                {item.normalized_value &&
                  item.normalized_value !== item.value && (
                    <span> · valeur normalisée : {item.normalized_value}</span>
                  )}
                {item.context ? <p>{item.context}</p> : null}
                <ExtractionEvidence item={item} source={source} />
              </li>
            ))}
          </ul>
        ) : (
          <p>Aucun IOC ou artefact extrait.</p>
        )}
      </section>

      <section className="extraction-section">
        <h3>Règles publiées</h3>
        {rules.length > 0 ? (
          <ul>
            {rules.map(({ item, source }, index) => (
              <li key={`${source.source_document_id}-rule-${index}`}>
                <p>
                  <strong>{item.name ?? item.rule_type}</strong> ·{" "}
                  {item.rule_type}
                </p>
                <pre>{item.body}</pre>
                <ExtractionEvidence item={item} source={source} />
              </li>
            ))}
          </ul>
        ) : (
          <p>Aucune règle publiée extraite.</p>
        )}
      </section>

      <section className="extraction-section">
        <h3>Incertitudes</h3>
        {uncertainties.length > 0 ? (
          <ul>
            {uncertainties.map(({ source, text, index }) => (
              <li key={`${source.source_document_id}-uncertainty-${index}`}>
                {text}
                <ExtractionSourceProvenance source={source} />
              </li>
            ))}
          </ul>
        ) : (
          <p>Aucune incertitude signalée.</p>
        )}
      </section>

      <section className="extraction-section">
        <h3>Sources omises / en erreur</h3>
        {document.omitted_sources.length > 0 ? (
          <ul>
            {document.omitted_sources.map((omission, index) => (
              <li key={`${omission.canonical_url}-${index}`}>
                <h4>{omission.title?.trim() || omission.canonical_url}</h4>
                <a href={omission.canonical_url}>{omission.canonical_url}</a>
                <p>
                  {REFERENCE_TIER_LABELS[omission.tier]} ·{" "}
                  {omission.collection_state}
                </p>
                <p>{omission.reason}</p>
                {omission.source_document_id ? (
                  <p>Document : {omission.source_document_id}</p>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p>Aucune source omise ou en erreur.</p>
        )}
      </section>

      <section className="extraction-section">
        <h3>Warnings</h3>
        {document.warnings.length > 0 ? (
          <ul>
            {document.warnings.map((warning, index) => (
              <li key={`${warning}-${index}`}>{warning}</li>
            ))}
          </ul>
        ) : (
          <p>Aucun warning.</p>
        )}
      </section>
    </article>
  );
}

export function PublicationDocumentView({
  document,
}: {
  document: PublicationDocument;
}) {
  const visibleGroups = document.indicators.filter(
    (group) => IOC_LABELS[group.artifact_type] && group.values.length > 0,
  );
  return (
    <article className="publication-preview">
      <h3>{document.title}</h3>
      <div className="publication-preview__timeline">
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
        <section className="publication-preview__indicators">
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

  const referencesCorpus =
    stage === "references" &&
    isProductionReferenceCorpus(artifact.canonical_content)
      ? artifact.canonical_content
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
        artifact.metadata &&
        Object.keys(artifact.metadata).length > 0 && (
          <div className="artifact-metadata">
            <details>
              <summary>Métadonnées</summary>
              <pre>{JSON.stringify(artifact.metadata, null, 2)}</pre>
            </details>
          </div>
        )}

      {stage === "publication" &&
        isPublicationDocument(artifact.canonical_content) && (
          <PublicationDocumentView document={artifact.canonical_content} />
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

      {referencesCorpus ? (
        <ProductionReferenceCorpusView corpus={referencesCorpus} />
      ) : null}

      {stage === "publication" && artifact.rendered_content && (
        <p>
          <a
            className="button button--secondary"
            download={"publication-pandoc.md"}
            href={`data:text/markdown;charset=utf-8,${encodeURIComponent(artifact.rendered_content)}`}
          >
            Télécharger le Markdown Pandoc
          </a>
        </p>
      )}

      {stage !== "publication" &&
        stage !== "extraction" &&
        !referencesCorpus &&
        artifact.rendered_content && (
          <div className="artifact-content">
            <div className="rendered-markdown">
              <pre>{artifact.rendered_content}</pre>
            </div>
          </div>
        )}

      {stage !== "publication" &&
        stage !== "extraction" &&
        !referencesCorpus &&
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
