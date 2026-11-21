import type {
  EditorialEvidenceReference,
  EditorialWorkspaceBlock,
} from "../../api/editorialWorkspace";
import type { AdmittedEvidence } from "./ManualTableEditor";

const evidenceKinds = new Set<EditorialEvidenceReference["kind"]>([
  "fact",
  "event",
  "source_metadata",
  "indicator",
  "rule",
  "uncertainty",
]);

function textValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export function admittedEvidence(
  block: EditorialWorkspaceBlock,
): AdmittedEvidence[] {
  const references: AdmittedEvidence[] = [];
  const seen = new Set<string>();
  for (const value of block.evidence_refs) {
    if (typeof value !== "object" || value === null) continue;
    const sourceDocumentId = value.source_document_id;
    const kind = value.kind;
    const evidenceKey = value.evidence_key;
    if (
      typeof sourceDocumentId !== "string" ||
      typeof kind !== "string" ||
      !evidenceKinds.has(kind as EditorialEvidenceReference["kind"]) ||
      typeof evidenceKey !== "string" ||
      !/^[0-9a-f]{64}$/.test(evidenceKey)
    ) {
      continue;
    }
    const reference: EditorialEvidenceReference = {
      source_document_id: sourceDocumentId,
      kind: kind as EditorialEvidenceReference["kind"],
      evidence_key: evidenceKey,
    };
    const identity = `${sourceDocumentId}:${kind}:${evidenceKey}`;
    if (seen.has(identity)) continue;
    seen.add(identity);
    const title =
      textValue(value.source_title) ??
      textValue(value.source_name) ??
      textValue(value.title) ??
      textValue(value.label) ??
      `Source ${sourceDocumentId}`;
    references.push({
      reference,
      identity,
      label: `${title} · ${kind}`,
      passage:
        textValue(value.passage) ??
        textValue(value.excerpt) ??
        textValue(value.quote),
    });
  }
  return references;
}
