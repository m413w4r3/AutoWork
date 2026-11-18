import type { PublicationDocument } from "../../api/production";
import {
  publicationLeadAnchor,
  publicationParagraphFingerprint,
  publicationReferenceAnchor,
  publicationReferenceDateLabel,
  publicationSectionParagraphAnchor,
  publicationTimelineAnchor,
  publicationTimelineDateLabel,
} from "./publicationDocumentHelpers";
import { PublicationEnrichmentsAt } from "./PublicationEnrichmentsAt";
import { PublicationSemanticContent } from "./PublicationSemanticContent";
import {
  PublicationParagraph,
  PublicationProvenance,
  type PublicationPassage,
  type PublicationSourceMap,
} from "./PublicationProvenance";

const IOC_LABELS: Record<string, string> = {
  ip: "Adresses IP",
  domain: "Noms de domaine",
  url: "URL",
  email: "Adresses e-mail",
  hash: "Hachages",
};

export function PublicationReferencesSection({
  document,
  sources,
  onSelectPassage,
}: {
  document: PublicationDocument;
  sources: PublicationSourceMap;
  onSelectPassage: (passage: PublicationPassage) => void;
}) {
  const publicationReferences =
    document.schema_version === "5" ? document.references : [];
  const hasReferenceEnrichments = [
    ...document.tables,
    ...document.diagrams,
    ...document.figures,
  ].some((item) => item.placement.kind === "after_timeline");
  const hasReferences =
    publicationReferences.length > 0 ||
    document.timeline.length > 0 ||
    hasReferenceEnrichments;
  if (!hasReferences) return null;

  return (
    <section aria-label="RÉFÉRENCES">
      <h4>RÉFÉRENCES</h4>
      {document.timeline.map((item, index) => {
        const dateLabel = publicationTimelineDateLabel(item);
        return (
          <div
            className="publication-preview__passage"
            key={`timeline-${index}`}
          >
            <p>
              {dateLabel ? <strong>{dateLabel} : </strong> : null}
              <PublicationSemanticContent
                document={document}
                text={item.text}
                anchor={publicationTimelineAnchor(index)}
              />
              <PublicationProvenance
                sourceIds={item.evidence_refs.map(
                  (ref) => ref.source_document_id,
                )}
                sources={sources}
              />
            </p>
            <button
              className="publication-preview__lineage-trigger"
              onClick={() =>
                onSelectPassage({
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
      {publicationReferences.map((item, index) => {
        const source = sources.get(item.source_document_id);
        const sourceName = source
          ? source.title || source.publisher || source.canonical_url
          : `Source ${item.source_document_id}`;
        return (
          <div
            className="publication-preview__passage"
            key={`reference-${item.source_document_id}`}
          >
            <p>
              <strong>
                {publicationReferenceDateLabel(source?.published_at ?? null)}{" "}
                :{" "}
              </strong>
              <PublicationSemanticContent
                document={document}
                text={item.text}
                anchor={publicationReferenceAnchor(index)}
              />{" "}
              {source ? (
                <a href={source.canonical_url} rel="noreferrer" target="_blank">
                  {sourceName}
                </a>
              ) : null}
            </p>
            {item.evidence_refs.length > 0 ? (
              <button
                className="publication-preview__lineage-trigger"
                onClick={() =>
                  onSelectPassage({
                    text: item.text,
                    evidenceRefs: item.evidence_refs,
                  })
                }
                type="button"
              >
                Voir les sources et preuves
              </button>
            ) : null}
          </div>
        );
      })}
      <PublicationEnrichmentsAt
        document={document}
        kind="after_timeline"
        sectionIndex={null}
      />
    </section>
  );
}

export function PublicationSynthesisSection({
  document,
  sources,
  onSelectPassage,
}: {
  document: PublicationDocument;
  sources: PublicationSourceMap;
  onSelectPassage: (passage: PublicationPassage) => void;
}) {
  const leadFingerprints = new Set(
    document.lead.map((item) => publicationParagraphFingerprint(item.text)),
  );
  return (
    <section aria-label="SYNTHÈSE">
      <h4>SYNTHÈSE</h4>
      {document.lead.map((item, index) => (
        <PublicationParagraph
          document={document}
          item={item}
          sources={sources}
          onSelectPassage={onSelectPassage}
          itemKey={`lead-${index}`}
          anchor={publicationLeadAnchor(index)}
          key={`lead-${index}`}
        />
      ))}
      <PublicationEnrichmentsAt
        document={document}
        kind="after_lead"
        sectionIndex={null}
      />
      {document.sections.map((section, index) => (
        <div key={`${section.kind}-${index}`}>
          {section.heading.trim() === "" ? null : <h5>{section.heading}</h5>}
          {section.paragraphs.map((item, paragraphIndex) =>
            leadFingerprints.has(
              publicationParagraphFingerprint(item.text),
            ) ? null : (
              <PublicationParagraph
                document={document}
                item={item}
                sources={sources}
                onSelectPassage={onSelectPassage}
                itemKey={`${index}-${paragraphIndex}`}
                anchor={publicationSectionParagraphAnchor(
                  index,
                  paragraphIndex,
                )}
                key={`${index}-${paragraphIndex}`}
              />
            ),
          )}
          <PublicationEnrichmentsAt
            document={document}
            kind="after_section"
            sectionIndex={index}
          />
        </div>
      ))}
      <PublicationEnrichmentsAt
        document={document}
        kind="end"
        sectionIndex={null}
      />
    </section>
  );
}

export function PublicationIndicatorsSection({
  document,
  publicationKind,
  sources,
}: {
  document: PublicationDocument;
  publicationKind?: "article" | "brief";
  sources: PublicationSourceMap;
}) {
  const originalIndicators =
    document.schema_version === "5" ? document.original_indicators : [];
  if (
    publicationKind === "brief" ||
    (document.indicators.length === 0 && originalIndicators.length === 0)
  ) {
    return null;
  }
  const title =
    document.schema_version === "5"
      ? `ANNEXE TECHNIQUE — IOC (${document.indicators.reduce((count, group) => count + group.indicators.length, 0)})`
      : "ANNEXE TECHNIQUE — INDICATEURS";
  return (
    <section aria-label={title}>
      <h4>{title}</h4>
      {document.indicators.map((group) => (
        <div key={group.artifact_type}>
          <h5>
            {IOC_LABELS[group.artifact_type] || group.artifact_type}
            {document.schema_version === "5"
              ? ` (${group.indicators.length})`
              : ""}
          </h5>
          <ul>
            {group.indicators.map((item) => (
              <li key={`${item.artifact_type}-${item.normalized_value}`}>
                <code>{item.normalized_value}</code>
                <PublicationProvenance
                  sourceIds={item.source_document_ids}
                  sources={sources}
                />
              </li>
            ))}
          </ul>
        </div>
      ))}
      {originalIndicators.length > 0 ? (
        <div>
          <h5>
            IOC originaux (
            {originalIndicators.reduce(
              (count, group) => count + group.indicators.length,
              0,
            )}
            )
          </h5>
          <p>
            Le lien avec le sujet n’est pas démontré. Sources :{" "}
            {Array.from(
              new Set(
                originalIndicators.flatMap((group) =>
                  group.indicators.flatMap((item) =>
                    item.source_document_ids.map((sourceId) => {
                      const source = sources.get(sourceId);
                      return source
                        ? source.publisher ||
                            source.title ||
                            source.canonical_url
                        : sourceId;
                    }),
                  ),
                ),
              ),
            ).join(", ")}
            .
          </p>
          {originalIndicators.map((group) => (
            <div key={`original-${group.artifact_type}`}>
              <h6>
                {IOC_LABELS[group.artifact_type] || group.artifact_type} (
                {group.indicators.length})
              </h6>
              <ul>
                {group.indicators.map((item) => (
                  <li key={`${item.artifact_type}-${item.normalized_value}`}>
                    <code>{item.normalized_value}</code>
                    <PublicationProvenance
                      sourceIds={item.source_document_ids}
                      sources={sources}
                    />
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}
