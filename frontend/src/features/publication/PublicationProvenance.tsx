import type {
  PublicationDocument,
  PublicationEvidenceRefV1,
} from "../../api/production";
import { PublicationSemanticContent } from "./PublicationSemanticContent";

export type PublicationSourceMap = ReadonlyMap<
  string,
  PublicationDocument["sources"][number]
>;

export interface PublicationPassage {
  text: string;
  evidenceRefs: PublicationEvidenceRefV1[];
}

export function PublicationProvenance({
  sourceIds,
  sources,
}: {
  sourceIds: string[];
  sources: PublicationSourceMap;
}) {
  return (
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
}

export function PublicationParagraph({
  document,
  item,
  sources,
  onSelectPassage,
  itemKey,
  anchor,
}: {
  document: PublicationDocument;
  item: { text: string; evidence_refs: PublicationEvidenceRefV1[] };
  sources: PublicationSourceMap;
  onSelectPassage: (passage: PublicationPassage) => void;
  itemKey: string;
  anchor: string;
}) {
  return (
    <div className="publication-preview__passage" key={itemKey}>
      <p>
        <PublicationSemanticContent
          document={document}
          text={item.text}
          anchor={anchor}
        />
        <PublicationProvenance
          sourceIds={item.evidence_refs.map((ref) => ref.source_document_id)}
          sources={sources}
        />
      </p>
      <button
        className="publication-preview__lineage-trigger"
        onClick={() =>
          onSelectPassage({ text: item.text, evidenceRefs: item.evidence_refs })
        }
        type="button"
      >
        Voir les sources et preuves
      </button>
    </div>
  );
}

export function PublicationLineagePanel({
  passage,
  sources,
  onClose,
}: {
  passage: PublicationPassage;
  sources: PublicationSourceMap;
  onClose: () => void;
}) {
  return (
    <aside
      aria-label="Sources et preuves du passage"
      className="publication-lineage-panel"
    >
      <div className="publication-lineage-panel__header">
        <h4>Sources et preuves</h4>
        <button
          aria-label="Fermer le panneau des preuves"
          className="button button--secondary"
          onClick={onClose}
          type="button"
        >
          Fermer
        </button>
      </div>
      <blockquote>{passage.text}</blockquote>
      <ul>
        {passage.evidenceRefs.map((ref) => {
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
                    {source.title || source.publisher || source.canonical_url}
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
  );
}
