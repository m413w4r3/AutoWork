import type {
  PublicationDocument,
  PublicationDocumentArtifactWithV8,
} from "../../api/production";
import { isRecord } from "./publicationValueGuards";

export function publicationDocumentBody(
  document: PublicationDocumentArtifactWithV8,
): PublicationDocument {
  if (document.schema_version === "8" || document.schema_version === "9") {
    return document.base_publication.base_publication.base_publication;
  }
  if (document.schema_version === "7") {
    return document.base_publication.base_publication;
  }
  if (document.schema_version === "6") return document.base_publication;
  return document;
}

export function recordValue(value: unknown): Record<string, unknown> | null {
  if (!isRecord(value) || Array.isArray(value)) return null;
  return value;
}

type ProjectionEvidenceReference = {
  source_document_id: string;
  kind: string;
  evidence_key: string;
};

function isProjectionEvidenceReference(
  value: unknown,
): value is ProjectionEvidenceReference {
  return (
    isRecord(value) &&
    typeof value.source_document_id === "string" &&
    typeof value.kind === "string" &&
    typeof value.evidence_key === "string"
  );
}

function evidenceReferenceAt(
  refs: unknown[],
  index: unknown,
): ProjectionEvidenceReference | null {
  if (typeof index !== "number" || !Number.isInteger(index) || index < 0) {
    return null;
  }
  const ref = refs[index];
  return isProjectionEvidenceReference(ref) ? ref : null;
}

function evidenceReferencesAt(
  refs: unknown[],
  indexes: unknown,
): ProjectionEvidenceReference[] | null {
  if (!Array.isArray(indexes)) return null;
  const resolved = indexes.map((index) => evidenceReferenceAt(refs, index));
  return resolved.some((ref) => ref === null)
    ? null
    : resolved.filter(
        (ref): ref is ProjectionEvidenceReference => ref !== null,
      );
}

export function relevanceProjectionForDisplay(value: unknown): unknown {
  if (
    !isRecord(value) ||
    value.schema_version !== 3 ||
    !Array.isArray(value.extraction_evidence_refs) ||
    !Array.isArray(value.classifications) ||
    !Array.isArray(value.source_pair_relations)
  ) {
    return value;
  }

  const refs = value.extraction_evidence_refs as unknown[];
  let valid = true;
  const classifications = (value.classifications as unknown[]).map(
    (item: unknown) => {
      if (!isRecord(item)) {
        valid = false;
        return item;
      }
      const evidenceRef = evidenceReferenceAt(refs, item.evidence_ref_index);
      const supportingRefs = evidenceReferencesAt(
        refs,
        item.supporting_evidence_ref_indexes,
      );
      if (evidenceRef === null || supportingRefs === null) {
        valid = false;
        return item;
      }
      const expanded = { ...item };
      delete expanded.evidence_ref_index;
      delete expanded.supporting_evidence_ref_indexes;
      return {
        ...expanded,
        evidence_ref: evidenceRef,
        supporting_evidence_refs: supportingRefs,
      };
    },
  );
  const sourcePairRelations = (value.source_pair_relations as unknown[]).map(
    (item: unknown) => {
      if (!isRecord(item)) {
        valid = false;
        return item;
      }
      const supportingRefs = evidenceReferencesAt(
        refs,
        item.supporting_evidence_ref_indexes,
      );
      if (supportingRefs === null) {
        valid = false;
        return item;
      }
      const expanded = { ...item };
      delete expanded.supporting_evidence_ref_indexes;
      return { ...expanded, supporting_evidence_refs: supportingRefs };
    },
  );

  return valid
    ? {
        ...value,
        classifications,
        source_pair_relations: sourcePairRelations,
      }
    : value;
}
