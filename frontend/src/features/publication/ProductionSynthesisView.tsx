import type {
  ProductionExtractionV1,
  ProductionSourceExtractionV1,
  ProductionSynthesisEvidenceKindV1,
  ProductionSynthesisEvidenceRefV1,
  ProductionSynthesisParagraphV1,
  ProductionSynthesisTimelineEntryV1,
  ProductionSynthesisV1,
} from "../../api/production";

type SourcesById = ReadonlyMap<string, ProductionSourceExtractionV1>;

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

export function ProductionSynthesisView({
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
          {section.heading.trim() === "" ? null : <h4>{section.heading}</h4>}
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
