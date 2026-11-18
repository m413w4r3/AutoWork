import type {
  ProductionExtractionEvidenceV1,
  ProductionExtractionOmissionReasonV1,
  ProductionExtractionV1,
  ProductionSourceExtractionV1,
} from "../../api/production";
import {
  REFERENCE_TIER_LABELS,
  REUSE_STATE_LABELS,
} from "./productionStageLabels";
import { ProductionExtractionSourceList } from "./ProductionExtractionSourceList";

const OMISSION_REASON_LABELS: Record<
  ProductionExtractionOmissionReasonV1,
  string
> = {
  reference_not_eligible: "Non éligible dans le corpus REFERENCES",
  source_extraction_failed: "Extraction en échec",
};
type SourcesById = ReadonlyMap<string, ProductionSourceExtractionV1>;

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

export function ProductionExtractionPreview({
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
        <ProductionExtractionSourceList
          sources={document.sources.filter(
            (source) => source.profile === "full",
          )}
          empty="Aucune source FULL."
        />
      </section>

      <section className="extraction-section">
        <h3>Sources IOC_RULES</h3>
        <ProductionExtractionSourceList
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
