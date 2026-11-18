import type {
  ExtractionDocumentV2,
  PublicationDocumentArtifactWithV8,
  PublicationSemanticRoleV1,
  ProductionExtractionTierV1,
  ProductionSourceExtractionV1,
} from "../../api/production";
import { isRecord } from "./publicationValueGuards";

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

interface ProductionReferenceSource {
  canonical_url: string;
  title: string | null;
  tier: ProductionExtractionTierV1;
  role: string;
  kind: ProductionSourceExtractionV1["kind"];
  publisher: string | null;
  published_at: string | null;
  collection_state: string;
  eligible_for_extraction: boolean;
}

export interface ProductionReferenceCorpus {
  schema_version: 1;
  sources: ProductionReferenceSource[];
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

export function isProductionReferenceCorpus(
  value: unknown,
): value is ProductionReferenceCorpus {
  return (
    isRecord(value) &&
    value.schema_version === 1 &&
    Array.isArray(value.sources) &&
    value.sources.every(isProductionReferenceSource)
  );
}

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

function isPublicationEvidenceRef(value: unknown): boolean {
  return (
    isRecord(value) &&
    typeof value.source_document_id === "string" &&
    (value.kind === "fact" ||
      value.kind === "event" ||
      value.kind === "indicator" ||
      value.kind === "rule") &&
    typeof value.evidence_key === "string"
  );
}

function isPublicationEvidenceRefArray(value: unknown): boolean {
  return (
    Array.isArray(value) &&
    value.length > 0 &&
    value.every(isPublicationEvidenceRef)
  );
}

function isArticleOverviewField(value: unknown): boolean {
  return (
    isRecord(value) &&
    typeof value.text === "string" &&
    isPublicationEvidenceRefArray(value.evidence_refs)
  );
}

function isArticleOverview(value: unknown): boolean {
  return (
    value === null ||
    (isRecord(value) &&
      ["victimology", "arsenal", "main_objective"].every(
        (field) =>
          field in value &&
          (value[field] === null || isArticleOverviewField(value[field])),
      ))
  );
}

function isArticleTimelineKeyEvent(value: unknown): boolean {
  return (
    isRecord(value) &&
    typeof value.event_date === "string" &&
    typeof value.text === "string" &&
    isPublicationEvidenceRefArray(value.evidence_refs)
  );
}

function isArticleIocValue(value: unknown): boolean {
  return (
    isRecord(value) &&
    typeof value.value === "string" &&
    typeof value.normalized_value === "string" &&
    Array.isArray(value.source_document_ids) &&
    value.source_document_ids.length > 0 &&
    value.source_document_ids.every(
      (sourceId) => typeof sourceId === "string",
    ) &&
    isPublicationEvidenceRefArray(value.evidence_refs)
  );
}

function isArticleFormat(
  value: unknown,
  publicationKind: "article" | "brief",
): boolean {
  if (publicationKind === "brief") return value === null;
  if (!isRecord(value) || !isArticleOverview(value.overview)) return false;
  if (
    !Array.isArray(value.timeline_key_events) ||
    value.timeline_key_events.length === 1 ||
    value.timeline_key_events.length > 8 ||
    !value.timeline_key_events.every(isArticleTimelineKeyEvent) ||
    !isRecord(value.ioc_categories)
  ) {
    return false;
  }
  const iocCategories = value.ioc_categories;
  return ["ips", "urls", "domains", "files"].every((category) => {
    const categoryValues = iocCategories[category];
    return (
      Array.isArray(categoryValues) &&
      categoryValues.every((item: unknown) => isArticleIocValue(item))
    );
  });
}

export function isPublicationDocument(
  value: unknown,
): value is PublicationDocumentArtifactWithV8 {
  if (!isRecord(value)) return false;
  if (value.schema_version === "8") {
    const base = value.base_publication;
    return (
      isRecord(base) &&
      base.schema_version === "7" &&
      isPublicationDocument(base) &&
      (base.publication_kind === "article" ||
        base.publication_kind === "brief") &&
      isArticleFormat(value.article_format, base.publication_kind)
    );
  }
  if (value.schema_version === "7") {
    return (
      (value.publication_kind === "article" ||
        value.publication_kind === "brief") &&
      typeof value.routing_decision_hash === "string" &&
      typeof value.routing_policy_version === "string" &&
      isPublicationDocument(value.base_publication) &&
      value.base_publication.schema_version === "6"
    );
  }
  if (value.schema_version === "6") {
    return (
      isRecord(value.provenance) &&
      typeof value.provenance.base_publication_artifact_id === "string" &&
      typeof value.provenance.base_publication_sha256 === "string" &&
      isPublicationDocument(value.base_publication) &&
      value.base_publication.schema_version === "5"
    );
  }
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
    (value.schema_version === "5" &&
      isSemanticText(value.rich_text) &&
      Array.isArray(value.references) &&
      value.references.every(
        (reference) =>
          isRecord(reference) &&
          typeof reference.source_document_id === "string" &&
          typeof reference.text === "string" &&
          Array.isArray(reference.evidence_refs),
      ) &&
      Array.isArray(value.original_indicators))
  );
}

export function isExtractionDocument(
  value: unknown,
): value is ExtractionDocumentV2 {
  return (
    typeof value === "object" &&
    value !== null &&
    "schema_version" in value &&
    value.schema_version === "2" &&
    "items" in value &&
    Array.isArray(value.items)
  );
}
