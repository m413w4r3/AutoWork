import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  getReferencesArtifact,
  getExtractionArtifact,
  getRelevanceProjectionArtifact,
  getSynthesisArtifact,
  getEditorialEnrichmentArtifact,
  getPublicationArtifact,
  getPublicationArtifactPreview,
  getPublicationPreviewPdf,
  isProductionExtractionV1,
  isProductionSynthesisV1,
  type ArtifactResponse,
  type PublicationArtifactPreview,
  type ProductionExtractionEvidenceV1,
  type ProductionExtractionOmissionReasonV1,
  type ProductionExtractionReuseStateV1,
  type ProductionExtractionV1,
  type ProductionSourceExtractionV1,
  type ProductionSynthesisEvidenceKindV1,
  type ProductionSynthesisEvidenceRefV1,
  type ProductionSynthesisParagraphV1,
  type ProductionSynthesisTimelineEntryV1,
  type ProductionSynthesisV1,
  type PublicationDocument,
  type PublicationDiagramV1,
  type PublicationEvidenceRefV1,
  type PublicationSemanticRoleV1,
  type PublicationSourceFigureV1,
  type PublicationTableV1,
  type ExtractionDocumentV2,
  type ExtractionItemV2,
} from "../api/production";

interface ProductionArtifactViewProps {
  subjectId: string;
  stage:
    | "references"
    | "extraction"
    | "relevance_projection"
    | "synthesis"
    | "publication";
  onClose?: () => void;
}

const STAGE_LABELS: Record<string, string> = {
  references: "Références",
  extraction: "Extraction CTI",
  relevance_projection: "Périmètre des preuves",
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
    case "relevance_projection":
      return getRelevanceProjectionArtifact;
    case "synthesis":
      return getSynthesisArtifact;
    case "publication":
      return getPublicationArtifact;
    default:
      throw new Error(`Unknown stage: ${stage}`);
  }
}

const SEMANTIC_ROLES = new Set<PublicationSemanticRoleV1>([
  "text",
  "actor",
  "campaign",
  "malware",
  "tool",
  "product",
  "english_term",
  "technical",
  "technical_literal",
  "ioc",
  "path",
  "command",
  "protocol_field",
  "source",
  "proof",
]);

function isSemanticText(value: unknown): boolean {
  return (
    isRecord(value) &&
    value.schema_version === "1" &&
    typeof value.policy_version === "string" &&
    Array.isArray(value.paragraphs) &&
    value.paragraphs.every(
      (paragraph) =>
        isRecord(paragraph) &&
        typeof paragraph.anchor === "string" &&
        Array.isArray(paragraph.spans) &&
        paragraph.spans.every(
          (span) =>
            isRecord(span) &&
            typeof span.role === "string" &&
            SEMANTIC_ROLES.has(span.role as PublicationSemanticRoleV1) &&
            typeof span.text === "string",
        ),
    )
  );
}

function isPublicationDocument(value: unknown): value is PublicationDocument {
  if (!isRecord(value)) return false;
  const common =
    typeof value.title === "string" &&
    typeof value.subject_id === "string" &&
    Array.isArray(value.lead) &&
    Array.isArray(value.sections) &&
    Array.isArray(value.timeline) &&
    Array.isArray(value.indicators) &&
    Array.isArray(value.sources) &&
    Array.isArray(value.uncertainties) &&
    Array.isArray(value.tables) &&
    Array.isArray(value.diagrams) &&
    Array.isArray(value.figures);
  if (!common) return false;
  return (
    value.schema_version === "4" ||
    (value.schema_version === "5" && isSemanticText(value.rich_text))
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

const REUSE_STATE_LABELS: Record<ProductionExtractionReuseStateV1, string> = {
  fresh: "Calculée",
  reused: "Réutilisée",
  content_duplicate: "Contenu identique à une autre source",
};

const OMISSION_REASON_LABELS: Record<
  ProductionExtractionOmissionReasonV1,
  string
> = {
  reference_not_eligible: "Non éligible dans le corpus REFERENCES",
  source_extraction_failed: "Extraction en échec",
};

type SourcesById = ReadonlyMap<string, ProductionSourceExtractionV1>;

function ProductionExtractionSourceCard({
  source,
}: {
  source: ProductionSourceExtractionV1;
}) {
  return (
    <li>
      <article className="extraction-source">
        <h4>
          <a href={source.canonical_url}>{source.canonical_url}</a>
        </h4>
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
            <dd title={source.content_sha256}>
              {source.content_sha256.slice(0, 12)}…
            </dd>
          </div>
          <div>
            <dt>Statut</dt>
            <dd>{REUSE_STATE_LABELS[source.reuse_state]}</dd>
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

function SourceList({
  sources,
  empty,
}: {
  sources: ProductionSourceExtractionV1[];
  empty: string;
}) {
  if (sources.length === 0) return <p>{empty}</p>;
  return (
    <ul className="extraction-sources">
      {sources.map((source) => (
        <ProductionExtractionSourceCard
          key={source.source_document_id}
          source={source}
        />
      ))}
    </ul>
  );
}

/** The publications behind one element: an equivalent element may be
 * published by several documents, and every one of them stays named. */
function ExtractionProvenance({
  documentIds,
  sourcesById,
}: {
  documentIds: string[];
  sourcesById: SourcesById;
}) {
  return (
    <p className="extraction-provenance">
      Source{documentIds.length > 1 ? "s" : ""} :{" "}
      {documentIds.map((documentId, index) => {
        const source = sourcesById.get(documentId);
        return (
          <span key={documentId}>
            {index > 0 ? " · " : null}
            {source ? (
              <a href={source.canonical_url}>{source.canonical_url}</a>
            ) : (
              documentId
            )}
          </span>
        );
      })}
    </p>
  );
}

function ExtractionEvidence({
  item,
  sourcesById,
}: {
  item: ProductionExtractionEvidenceV1;
  sourcesById: SourcesById;
}) {
  return (
    <>
      {item.context ? <p>{item.context}</p> : null}
      <blockquote>{item.evidence_quote}</blockquote>
      <ExtractionProvenance
        documentIds={item.source_document_ids}
        sourcesById={sourcesById}
      />
    </>
  );
}

function ProductionExtractionPreview({
  document,
}: {
  document: ProductionExtractionV1;
}) {
  const sourcesById: SourcesById = new Map(
    document.sources.map((source) => [source.source_document_id, source]),
  );
  const withSource = <T,>(
    pick: (source: ProductionSourceExtractionV1) => T[],
  ) =>
    document.sources.flatMap((source) =>
      pick(source).map((item, index) => ({ item, source, index })),
    );
  const events = withSource((source) => source.events);
  const facts = withSource((source) => source.facts);
  const indicators = withSource((source) => source.indicators);
  const rules = withSource((source) => source.rules);
  const uncertainties = withSource((source) => source.uncertainties);

  return (
    <article className="extraction-preview extraction-preview--canonical">
      <section className="extraction-section">
        <h3>Sources FULL</h3>
        <SourceList
          sources={document.sources.filter(
            (source) => source.profile === "full",
          )}
          empty="Aucune source FULL."
        />
      </section>

      <section className="extraction-section">
        <h3>Sources IOC_RULES</h3>
        <SourceList
          sources={document.sources.filter(
            (source) => source.profile === "ioc_rules",
          )}
          empty="Aucune source IOC_RULES."
        />
      </section>

      <section className="extraction-section">
        <h3>Sources réutilisées</h3>
        {document.sources.some((source) => source.reuse_state !== "fresh") ? (
          <ul>
            {document.sources
              .filter((source) => source.reuse_state !== "fresh")
              .map((source) => (
                <li key={source.source_document_id}>
                  <a href={source.canonical_url}>{source.canonical_url}</a> ·{" "}
                  {REUSE_STATE_LABELS[source.reuse_state]}
                </li>
              ))}
          </ul>
        ) : (
          <p>Aucune source réutilisée : toutes ont été calculées.</p>
        )}
      </section>

      <section className="extraction-section">
        <h3>Sources omises / en erreur</h3>
        {document.omitted_sources.length > 0 ? (
          <ul>
            {document.omitted_sources.map((omission) => (
              <li key={omission.canonical_url}>
                <a href={omission.canonical_url}>{omission.canonical_url}</a>
                <p>
                  {REFERENCE_TIER_LABELS[omission.tier]} ·{" "}
                  {OMISSION_REASON_LABELS[omission.reason]} ·{" "}
                  {omission.collection_state}
                </p>
                {omission.error_code ? (
                  <p>
                    Code : <code>{omission.error_code}</code>
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p>Aucune source omise ou en erreur.</p>
        )}
      </section>

      <section className="extraction-section">
        <h3>Chronologie</h3>
        {events.length > 0 ? (
          <ul>
            {events.map(({ item, source, index }) => (
              <li key={`${source.source_document_id}-event-${index}`}>
                <p>
                  {item.event_date || item.date_text ? (
                    <strong>{item.event_date ?? item.date_text} — </strong>
                  ) : null}
                  {item.text}
                </p>
                <ExtractionEvidence item={item} sourcesById={sourcesById} />
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
            {facts.map(({ item, source, index }) => (
              <li key={`${source.source_document_id}-fact-${index}`}>
                <strong>{item.category} : </strong>
                {item.value}
                {item.attack_id ? <code> {item.attack_id}</code> : null}
                <ExtractionEvidence item={item} sourcesById={sourcesById} />
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
            {indicators.map(({ item, source, index }) => (
              <li key={`${source.source_document_id}-indicator-${index}`}>
                <strong>{item.artifact_type} : </strong>
                <code>{item.value}</code>
                <span>
                  {" "}
                  ·{" "}
                  {item.indicator_status === "confirmed_ioc"
                    ? "IOC confirmé"
                    : "Contextuel"}
                </span>
                <ExtractionEvidence item={item} sourcesById={sourcesById} />
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
            {rules.map(({ item, source, index }) => (
              <li key={`${source.source_document_id}-rule-${index}`}>
                <p>
                  <strong>{item.name ?? item.rule_type}</strong> ·{" "}
                  {item.rule_type}
                </p>
                <pre>{item.body}</pre>
                <ExtractionEvidence item={item} sourcesById={sourcesById} />
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
            {uncertainties.map(({ item, source, index }) => (
              <li key={`${source.source_document_id}-uncertainty-${index}`}>
                {item}
                <ExtractionProvenance
                  documentIds={[source.source_document_id]}
                  sourcesById={sourcesById}
                />
              </li>
            ))}
          </ul>
        ) : (
          <p>Aucune incertitude signalée.</p>
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

const SYNTHESIS_MODE_LABELS: Record<string, string> = {
  fresh: "Rédaction initiale",
  revise_previous: "Révision de la synthèse précédente",
  reuse_exact: "Réutilisation d’une synthèse identique",
};

const SYNTHESIS_EVIDENCE_KIND_LABELS: Record<
  ProductionSynthesisEvidenceKindV1,
  string
> = {
  fact: "fait",
  event: "événement",
  indicator: "indicateur",
  rule: "règle",
};

/** Generation mode is artifact metadata, never a canonical synthesis field. */
function synthesisModeLabel(metadata: Record<string, unknown>): string {
  const mode = metadata.mode;
  if (typeof mode !== "string" || mode.trim() === "") return "Non renseigné";
  return SYNTHESIS_MODE_LABELS[mode] ?? mode;
}

function synthesisDateLabel(entry: ProductionSynthesisTimelineEntryV1): string {
  if (entry.event_date) {
    return new Intl.DateTimeFormat("fr-FR", { dateStyle: "long" }).format(
      new Date(`${entry.event_date}T00:00:00`),
    );
  }
  return entry.date_text ?? "Date non précisée";
}

/**
 * Resolve one document identity against the canonical extraction. Unknown
 * identities keep their exact value available instead of being dropped.
 */
function SynthesisSource({
  documentId,
  sourcesById,
}: {
  documentId: string;
  sourcesById: SourcesById;
}) {
  const source = sourcesById.get(documentId);
  if (source) {
    return <a href={source.canonical_url}>{source.canonical_url}</a>;
  }
  return (
    <span
      className="synthesis-unknown-source"
      data-source-document-id={documentId}
      title={documentId}
    >
      Document source indisponible : {documentId}
    </span>
  );
}

function SynthesisSources({
  documentIds,
  sourcesById,
}: {
  documentIds: readonly string[];
  sourcesById: SourcesById;
}) {
  return (
    <ul className="synthesis-sources">
      {documentIds.map((documentId) => (
        <li key={documentId}>
          <SynthesisSource documentId={documentId} sourcesById={sourcesById} />
        </li>
      ))}
    </ul>
  );
}

/** Evidence count plus the exact documents, deduplicated for presentation. */
function SynthesisEvidence({
  evidenceRefs,
  sourcesById,
}: {
  evidenceRefs: ProductionSynthesisEvidenceRefV1[];
  sourcesById: SourcesById;
}) {
  const documents = new Map<string, ProductionSynthesisEvidenceKindV1[]>();
  for (const ref of evidenceRefs) {
    const kinds = documents.get(ref.source_document_id) ?? [];
    if (!kinds.includes(ref.kind)) kinds.push(ref.kind);
    documents.set(ref.source_document_id, kinds);
  }
  return (
    <div className="synthesis-evidence">
      <p className="synthesis-evidence__count">
        {evidenceRefs.length} preuve{evidenceRefs.length > 1 ? "s" : ""}
      </p>
      <ul className="synthesis-evidence__documents">
        {[...documents.entries()].map(([documentId, kinds]) => (
          <li key={documentId}>
            <SynthesisSource
              documentId={documentId}
              sourcesById={sourcesById}
            />
            <span className="synthesis-evidence__kinds">
              {" "}
              (
              {kinds
                .map((kind) => SYNTHESIS_EVIDENCE_KIND_LABELS[kind])
                .join(", ")}
              )
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function SynthesisParagraph({
  paragraph,
  sourcesById,
}: {
  paragraph: ProductionSynthesisParagraphV1;
  sourcesById: SourcesById;
}) {
  return (
    <div className="synthesis-paragraph">
      <p>{paragraph.text}</p>
      <SynthesisEvidence
        evidenceRefs={paragraph.evidence_refs}
        sourcesById={sourcesById}
      />
    </div>
  );
}

function ProductionSynthesisView({
  document,
  extraction,
  metadata,
}: {
  document: ProductionSynthesisV1;
  extraction: ProductionExtractionV1 | null;
  metadata: Record<string, unknown>;
}) {
  const sourcesById: SourcesById = new Map(
    (extraction?.sources ?? []).map((source) => [
      source.source_document_id,
      source,
    ]),
  );
  return (
    <article className="synthesis-preview synthesis-preview--canonical">
      <h3>{document.title}</h3>
      <dl className="synthesis-meta">
        <div>
          <dt>Langue de publication</dt>
          <dd>{document.publication_language}</dd>
        </div>
        <div>
          <dt>Mode de génération</dt>
          <dd>{synthesisModeLabel(metadata)}</dd>
        </div>
        <div>
          <dt>Politique de synthèse</dt>
          <dd>{document.synthesis_policy_version}</dd>
        </div>
      </dl>
      {extraction === null ? (
        <p className="synthesis-meta__notice">
          Extraction canonique indisponible : les documents sources ne peuvent
          pas être résolus.
        </p>
      ) : null}

      {document.lead.length > 0 ? (
        <section className="synthesis-section">
          <h4>Lead</h4>
          {document.lead.map((paragraph, index) => (
            <SynthesisParagraph
              key={`lead-${index}`}
              paragraph={paragraph}
              sourcesById={sourcesById}
            />
          ))}
        </section>
      ) : null}

      {document.sections.map((section, index) => (
        <section className="synthesis-section" key={`${section.kind}-${index}`}>
          {section.paragraphs.map((paragraph, paragraphIndex) => (
            <SynthesisParagraph
              key={`${section.kind}-${index}-${paragraphIndex}`}
              paragraph={paragraph}
              sourcesById={sourcesById}
            />
          ))}
        </section>
      ))}

      <section className="synthesis-section">
        <h4>Chronologie</h4>
        {document.timeline.length > 0 ? (
          <ul className="synthesis-timeline">
            {document.timeline.map((entry, index) => {
              const dateLabel = synthesisDateLabel(entry);
              return (
                <li key={`timeline-${index}`}>
                  <strong className="semantic-date">{dateLabel} : </strong>
                  {entry.text}
                  <SynthesisEvidence
                    evidenceRefs={entry.evidence_refs}
                    sourcesById={sourcesById}
                  />
                </li>
              );
            })}
          </ul>
        ) : (
          <p>Aucun événement de chronologie.</p>
        )}
      </section>

      <section className="synthesis-section">
        <h4>Incertitudes</h4>
        {document.uncertainties.length > 0 ? (
          <ul>
            {document.uncertainties.map((uncertainty, index) => (
              <li key={`uncertainty-${index}`}>
                {uncertainty.text}
                <SynthesisSources
                  documentIds={[...new Set(uncertainty.source_document_ids)]}
                  sourcesById={sourcesById}
                />
              </li>
            ))}
          </ul>
        ) : (
          <p>Aucune incertitude signalée.</p>
        )}
      </section>

      <section className="synthesis-section">
        <h4>Warnings</h4>
        {document.warnings.length > 0 ? (
          <ul>
            {document.warnings.map((warning, index) => (
              <li key={`warning-${index}`}>{warning}</li>
            ))}
          </ul>
        ) : (
          <p>Aucun warning.</p>
        )}
      </section>
    </article>
  );
}

function semanticContent(
  document: PublicationDocument,
  text: string,
  anchor: string,
) {
  if (document.schema_version !== "5") return text;
  const paragraph = document.rich_text.paragraphs.find(
    (item) => item.anchor === anchor,
  );
  if (
    !paragraph ||
    paragraph.spans.map((span) => span.text).join("") !== text
  ) {
    return text;
  }
  return paragraph.spans.map((span, index) => {
    const key = `${anchor}-${index}`;
    switch (span.role) {
      case "actor":
      case "campaign":
      case "malware":
      case "tool":
      case "product":
        return (
          <strong
            className={`semantic-role semantic-role--${span.role}`}
            key={key}
          >
            {span.text}
          </strong>
        );
      case "english_term":
        return (
          <em className="semantic-role semantic-role--english-term" key={key}>
            {span.text}
          </em>
        );
      case "technical":
        return (
          <span className="semantic-role semantic-role--technical" key={key}>
            {span.text}
          </span>
        );
      case "technical_literal":
      case "ioc":
      case "path":
      case "command":
      case "protocol_field":
        return (
          <code
            className={`semantic-role semantic-role--${span.role}`}
            key={key}
          >
            {span.text}
          </code>
        );
      case "source":
      case "proof":
        return (
          <span
            className={`semantic-role semantic-role--${span.role}`}
            key={key}
          >
            {span.text}
          </span>
        );
      case "text":
        return <span key={key}>{span.text}</span>;
    }
  });
}

export function PublicationDocumentView({
  document,
}: {
  document: PublicationDocument;
}) {
  const [selectedPassage, setSelectedPassage] = useState<{
    text: string;
    evidenceRefs: PublicationEvidenceRefV1[];
  } | null>(null);
  const sources = new Map(
    document.sources.map((source) => [source.source_document_id, source]),
  );
  const provenance = (sourceIds: string[]) => (
    <span className="publication-preview__provenance">
      {Array.from(new Set(sourceIds)).map((sourceId, index) => {
        const source = sources.get(sourceId);
        return source ? (
          <span key={sourceId}>
            {index > 0 ? " · " : " "}
            <a href={source.canonical_url} rel="noreferrer" target="_blank">
              {source.title || source.publisher || source.canonical_url}
            </a>
          </span>
        ) : null;
      })}
    </span>
  );
  const paragraph = (
    item: { text: string; evidence_refs: PublicationEvidenceRefV1[] },
    key: string,
    anchor: string,
  ) => (
    <div className="publication-preview__passage" key={key}>
      <p>
        {semanticContent(document, item.text, anchor)}
        {provenance(item.evidence_refs.map((ref) => ref.source_document_id))}
      </p>
      <button
        className="publication-preview__lineage-trigger"
        onClick={() =>
          setSelectedPassage({
            text: item.text,
            evidenceRefs: item.evidence_refs,
          })
        }
        type="button"
      >
        Voir les sources et preuves
      </button>
    </div>
  );
  const enrichmentsAt = (kind: string, sectionIndex: number | null) => (
    <>
      <PublicationTablesView
        document={document}
        tables={document.tables.filter(
          (item) =>
            item.placement.kind === kind &&
            item.placement.section_index === sectionIndex,
        )}
      />
      <PublicationDiagramsView
        document={document}
        subjectId={document.subject_id}
        diagrams={document.diagrams.filter(
          (item) =>
            item.placement.kind === kind &&
            item.placement.section_index === sectionIndex,
        )}
      />
      <PublicationFiguresView
        document={document}
        subjectId={document.subject_id}
        figures={document.figures.filter(
          (item) =>
            item.placement.kind === kind &&
            item.placement.section_index === sectionIndex,
        )}
      />
    </>
  );
  return (
    <div className="publication-preview__layout">
      <article className="publication-preview">
        <h3>{semanticContent(document, document.title, "title")}</h3>
        <section aria-label="RÉFÉRENCES">
          <h4>RÉFÉRENCES</h4>
          {document.timeline.length > 0 ? (
            <div>
              <h5>Chronologie</h5>
              {document.timeline.map((item, index) => {
                const anchor = `timeline:${String(index + 1).padStart(4, "0")}`;
                return (
                  <div
                    className="publication-preview__passage"
                    key={`timeline-${index}`}
                  >
                    <p>
                      {item.event_date || item.date_text ? (
                        <strong>{item.date_text || item.event_date} : </strong>
                      ) : null}
                      {semanticContent(document, item.text, anchor)}
                      {provenance(
                        item.evidence_refs.map((ref) => ref.source_document_id),
                      )}
                    </p>
                    <button
                      className="publication-preview__lineage-trigger"
                      onClick={() =>
                        setSelectedPassage({
                          text: item.text,
                          evidenceRefs: item.evidence_refs,
                        })
                      }
                      type="button"
                    >
                      Voir les sources et preuves
                    </button>
                  </div>
                );
              })}
            </div>
          ) : null}
          {enrichmentsAt("after_timeline", null)}
          {document.sources.length > 0 ? (
            <section>
              <h5>Sources complémentaires</h5>
              <ul>
                {document.sources.map((source) => (
                  <li key={source.source_document_id}>
                    <a
                      href={source.canonical_url}
                      rel="noreferrer"
                      target="_blank"
                    >
                      {source.title || source.canonical_url}
                    </a>
                    {source.publisher ? ` — ${source.publisher}` : ""}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </section>
        <section aria-label="SYNTHÈSE">
          <h4>SYNTHÈSE</h4>
          {document.lead.map((item, index) =>
            paragraph(
              item,
              `lead-${index}`,
              `lead:${String(index + 1).padStart(4, "0")}`,
            ),
          )}
          {enrichmentsAt("after_lead", null)}
          {document.sections.map((section, index) => (
            <div key={`${section.kind}-${index}`}>
              {section.paragraphs.map((item, paragraphIndex) =>
                paragraph(
                  item,
                  `${index}-${paragraphIndex}`,
                  `section:${index}:paragraph:${String(paragraphIndex + 1).padStart(4, "0")}`,
                ),
              )}
              {enrichmentsAt("after_section", index)}
            </div>
          ))}
          {enrichmentsAt("end", null)}
        </section>
        {document.indicators.length > 0 && (
          <section aria-label="ANNEXE TECHNIQUE — INDICATEURS">
            <h4>ANNEXE TECHNIQUE — INDICATEURS</h4>
            {document.indicators.map((group) => (
              <div key={group.artifact_type}>
                <h5>
                  {IOC_LABELS[group.artifact_type] || group.artifact_type}
                </h5>
                <ul>
                  {group.indicators.map((item) => (
                    <li key={`${item.artifact_type}-${item.normalized_value}`}>
                      <code>{item.normalized_value}</code>
                      {provenance(item.source_document_ids)}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </section>
        )}
      </article>
      {selectedPassage ? (
        <aside
          aria-label="Sources et preuves du passage"
          className="publication-lineage-panel"
        >
          <div className="publication-lineage-panel__header">
            <h4>Sources et preuves</h4>
            <button
              aria-label="Fermer le panneau des preuves"
              className="button button--secondary"
              onClick={() => setSelectedPassage(null)}
              type="button"
            >
              Fermer
            </button>
          </div>
          <blockquote>{selectedPassage.text}</blockquote>
          <ul>
            {selectedPassage.evidenceRefs.map((ref) => {
              const source = sources.get(ref.source_document_id);
              return (
                <li
                  key={`${ref.source_document_id}-${ref.kind}-${ref.evidence_key}`}
                >
                  <p>
                    {source ? (
                      <a
                        href={source.canonical_url}
                        rel="noreferrer"
                        target="_blank"
                      >
                        {source.title ||
                          source.publisher ||
                          source.canonical_url}
                      </a>
                    ) : (
                      <span>Source {ref.source_document_id}</span>
                    )}
                    {" · "}
                    {ref.kind}
                  </p>
                  <code>{ref.evidence_key}</code>
                </li>
              );
            })}
          </ul>
        </aside>
      ) : null}
    </div>
  );
}

function PublicationDiagnosticsPanel({
  diagnostics,
  document,
  editorialEnrichment,
}: {
  diagnostics: unknown;
  document: PublicationDocument;
  editorialEnrichment: ArtifactResponse | undefined;
}) {
  const mediaAssets = [
    ...document.diagrams.map((diagram) => ({
      asset_id: diagram.asset_id,
      key: diagram.key,
      kind: diagram.kind,
      mime_type: "image/svg+xml",
      placement: diagram.placement,
    })),
    ...document.figures.map((figure) => ({
      asset_id: figure.asset_id,
      key: figure.key,
      kind: "source_figure",
      sha256: figure.sha256,
      mime_type: figure.mime_type,
      byte_size: figure.byte_size,
      source_document_id: figure.source_document_id,
      source_url: figure.source_url,
      provenance: figure.provenance,
      locator: figure.locator,
      placement: figure.placement,
    })),
  ];
  const serialized = JSON.stringify(
    {
      diagnostics: diagnostics ?? {},
      editorial_enrichment: editorialEnrichment
        ? {
            artifact_id: editorialEnrichment.artifact_id,
            version: editorialEnrichment.version,
            metadata: editorialEnrichment.metadata,
            canonical_content: editorialEnrichment.canonical_content,
          }
        : null,
      media_assets: mediaAssets,
    },
    null,
    2,
  );
  const diagnosticsValue = diagnostics ?? {};
  const hasDiagnostics =
    typeof diagnosticsValue === "object" &&
    diagnosticsValue !== null &&
    Object.keys(diagnosticsValue).length > 0;
  return (
    <section
      className="publication-diagnostics"
      aria-label="Diagnostics de publication"
    >
      <h3>Diagnostics techniques</h3>
      {!hasDiagnostics && mediaAssets.length === 0 ? (
        <p>Aucun diagnostic technique.</p>
      ) : (
        <pre>{serialized}</pre>
      )}
    </section>
  );
}

function recordValue(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function PublicationTablesView({
  document,
  tables,
}: {
  document: PublicationDocument;
  tables: PublicationTableV1[];
}) {
  if (tables.length === 0) return null;
  return (
    <section>
      <h4>Tableaux</h4>
      {tables.map((table) => (
        <article key={table.key}>
          <h5>
            {semanticContent(document, table.title, `table:${table.key}:title`)}
          </h5>
          {table.caption && (
            <p>
              {semanticContent(
                document,
                table.caption,
                `table:${table.key}:caption`,
              )}
            </p>
          )}
          <table>
            <thead>
              <tr>
                {table.columns.map((column, columnIndex) => (
                  <th key={column.key}>
                    {semanticContent(
                      document,
                      column.label,
                      `table:${table.key}:column:${String(columnIndex + 1).padStart(4, "0")}`,
                    )}
                  </th>
                ))}
                <th>Preuves</th>
              </tr>
            </thead>
            <tbody>
              {table.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.cells.map((cell, cellIndex) => (
                    <td key={cellIndex}>
                      {semanticContent(
                        document,
                        cell,
                        `table:${table.key}:row:${String(rowIndex + 1).padStart(4, "0")}:cell:${String(cellIndex + 1).padStart(4, "0")}`,
                      )}
                    </td>
                  ))}
                  <td>{row.evidence_refs.length} preuves</td>
                </tr>
              ))}
            </tbody>
          </table>
        </article>
      ))}
    </section>
  );
}

function PublicationDiagramsView({
  document,
  subjectId,
  diagrams,
}: {
  document: PublicationDocument;
  subjectId: string;
  diagrams: PublicationDiagramV1[];
}) {
  if (diagrams.length === 0) return null;
  return (
    <section>
      <h4>Diagrammes</h4>
      {diagrams.map((diagram) => (
        <article key={diagram.key}>
          <h5>
            {semanticContent(
              document,
              diagram.title,
              `diagram:${diagram.key}:title`,
            )}
          </h5>
          <figure>
            <img
              alt={diagram.title}
              loading="lazy"
              src={`/api/subjects/${encodeURIComponent(subjectId)}/publication/assets/${encodeURIComponent(diagram.asset_id)}`}
            />
            {diagram.caption ? (
              <figcaption>
                {semanticContent(
                  document,
                  diagram.caption,
                  `diagram:${diagram.key}:caption`,
                )}
              </figcaption>
            ) : null}
          </figure>
        </article>
      ))}
    </section>
  );
}

function PublicationFiguresView({
  document,
  subjectId,
  figures,
}: {
  document: PublicationDocument;
  subjectId: string;
  figures: PublicationSourceFigureV1[];
}) {
  if (figures.length === 0) return null;
  return (
    <section>
      <h4>Figures source</h4>
      {figures.map((figure) => (
        <article key={figure.key}>
          <figure>
            <img
              alt={figure.caption || "Figure issue de la source"}
              loading="lazy"
              src={`/api/subjects/${encodeURIComponent(subjectId)}/publication/assets/${encodeURIComponent(figure.asset_id)}`}
            />
            {figure.caption ? (
              <figcaption>
                {semanticContent(
                  document,
                  figure.caption,
                  `figure:${figure.key}:caption`,
                )}
              </figcaption>
            ) : null}
          </figure>
        </article>
      ))}
    </section>
  );
}

const PUBLICATION_PREVIEW_STATUS_LABELS: Record<string, string> = {
  IN_PROGRESS: "Rendu en cours",
  READY: "PDF prêt",
  FAILED: "Échec du rendu",
  STALE: "Obsolète",
};

function PublicationPdfPanel({
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

  const publicationDocument =
    stage === "publication" &&
    artifact &&
    isPublicationDocument(artifact.canonical_content)
      ? artifact.canonical_content
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
          <PublicationDocumentView document={publicationDocument} />
        </>
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
            Les décisions ambiguës restent visibles ici avec leur motif et les
            références de preuve qui les soutiennent.
          </p>
          <pre>{JSON.stringify(artifact.canonical_content, null, 2)}</pre>
        </section>
      ) : null}

      {referencesCorpus ? (
        <ProductionReferenceCorpusView corpus={referencesCorpus} />
      ) : null}

      {synthesisDocument ? (
        <ProductionSynthesisView
          document={synthesisDocument}
          extraction={extractionDocument}
          metadata={artifact.metadata}
        />
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
