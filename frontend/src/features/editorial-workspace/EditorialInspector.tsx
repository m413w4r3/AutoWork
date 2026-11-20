import type {
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { EditorialConversation } from "./EditorialConversation";
import { BLOCK_KIND_LABELS } from "./blockKindLabels";

interface EditorialInspectorProps {
  subjectId: string;
  snapshot: EditorialWorkspaceSnapshot;
  block: EditorialWorkspaceBlock | null;
  sectionTitle: string | null;
  readOnly: boolean;
  localDraftText: string;
  onLocalDraftChange: (text: string) => void;
  onOpenSources: () => void;
  onOpenFigureGallery: () => void;
  onStartEditing: (blockId: string) => void;
  onBackToDocument: () => void;
  onChangeTarget: () => void;
}

function firstText(
  reference: Record<string, unknown>,
  keys: readonly string[],
): string | null {
  for (const key of keys) {
    const value = reference[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

function EvidenceReference({
  reference,
  index,
}: {
  reference: Record<string, unknown>;
  index: number;
}) {
  const label = firstText(reference, [
    "source_title",
    "source_name",
    "title",
    "label",
  ]);
  const passage = firstText(reference, ["passage", "excerpt", "quote", "text"]);
  return (
    <li className="editorial-evidence__item">
      <strong>{label ?? `Source ${index + 1}`}</strong>
      {passage ? <p>{passage}</p> : null}
    </li>
  );
}

function BlockActionToolbar({
  snapshot,
  block,
  readOnly,
  onOpenSources,
  onStartEditing,
}: {
  snapshot: EditorialWorkspaceSnapshot;
  block: EditorialWorkspaceBlock;
  readOnly: boolean;
  onOpenSources: () => void;
  onStartEditing: (blockId: string) => void;
}) {
  const operations = new Set(block.allowed_operations);
  const canModify =
    !readOnly &&
    snapshot.capabilities.manual_edit &&
    block.allowed_operations.some(
      (operation) => operation.startsWith("rewrite_") || operation === "modify",
    );
  const canDiscuss =
    !readOnly && snapshot.capabilities.can_discuss && operations.has("discuss");
  const canInsertAfter =
    !readOnly &&
    snapshot.capabilities.can_add_enrichment &&
    operations.has("insert_after");
  // Snapshot authorization controls evidence reads; allowed_operations only
  // gates writes. The server returns refs only when they are safe to expose.
  const canViewSources = block.evidence_refs.length > 0;
  const knownOperations = new Set([
    "rewrite",
    "modify",
    "discuss",
    "insert_after",
    "sources",
    "read_evidence",
  ]);
  const hasAdditionalOperations =
    !readOnly &&
    block.allowed_operations.some(
      (operation) => !knownOperations.has(operation),
    );

  return (
    <div className="editorial-block-actions" aria-label="Actions du bloc">
      {canModify ? (
        <button
          className="button button--secondary"
          type="button"
          onClick={() => onStartEditing(block.block_id)}
        >
          Modifier
        </button>
      ) : null}
      {canDiscuss ? (
        <button
          className="button button--secondary"
          type="button"
          disabled
          aria-label="Discuter. La discussion sera disponible dans une prochaine étape."
          title="La discussion sera disponible dans une prochaine étape."
        >
          Discuter
        </button>
      ) : null}
      {canInsertAfter ? (
        <button
          className="button button--secondary"
          type="button"
          disabled
          aria-label="Ajouter après. L’insertion sera disponible dans une prochaine étape."
          title="L’insertion sera disponible dans une prochaine étape."
        >
          Ajouter après
        </button>
      ) : null}
      {canViewSources ? (
        <button
          className="button button--secondary"
          type="button"
          onClick={onOpenSources}
        >
          Sources
        </button>
      ) : null}
      {hasAdditionalOperations ? (
        <details className="editorial-block-actions__more">
          <summary>Autres actions du bloc</summary>
          <p>
            Les autres opérations déclarées ne sont pas disponibles dans cette
            étape.
          </p>
        </details>
      ) : null}
    </div>
  );
}

export function EditorialInspector({
  subjectId,
  snapshot,
  block,
  sectionTitle,
  readOnly,
  localDraftText,
  onLocalDraftChange,
  onOpenSources,
  onOpenFigureGallery,
  onStartEditing,
  onBackToDocument,
  onChangeTarget,
}: EditorialInspectorProps) {
  return (
    <aside className="editorial-inspector" aria-label="Inspecteur éditorial">
      <button
        className="editorial-inspector__back"
        type="button"
        onClick={onBackToDocument}
      >
        ‹ Retour au document
      </button>
      <div className="editorial-inspector__content">
        <h2>Contexte du bloc</h2>
        {readOnly ? (
          <p className="editorial-readonly-note" role="note">
            Le type de publication est indéterminé. L’atelier reste en lecture
            seule.
          </p>
        ) : null}
        {block ? (
          <>
            <p className="editorial-inspector__target-kind">
              {BLOCK_KIND_LABELS[block.kind]}
            </p>
            <p className="editorial-inspector__section">
              Section : {sectionTitle ?? "Document"}
            </p>
            <p className="editorial-inspector__selected-text">{block.text}</p>
            <label className="editorial-local-draft">
              <span>Brouillon local</span>
              <textarea
                className="editorial-local-draft__input"
                aria-label="Brouillon local"
                value={localDraftText}
                onChange={(event) =>
                  onLocalDraftChange(event.currentTarget.value)
                }
                rows={5}
              />
              <span className="editorial-local-draft__privacy">
                Conservé en mémoire dans cet écran jusqu’à sa fermeture.
              </span>
            </label>
            <BlockActionToolbar
              snapshot={snapshot}
              block={block}
              readOnly={readOnly}
              onOpenSources={onOpenSources}
              onStartEditing={onStartEditing}
            />
            <EditorialConversation
              subjectId={subjectId}
              occurrenceId={block.block_id}
              snapshotId={snapshot.snapshot_id}
              targetLabel={block.text ?? BLOCK_KIND_LABELS[block.kind]}
              allowedOperations={block.allowed_operations}
              onChangeTarget={onChangeTarget}
            />
            <section
              className="editorial-evidence"
              aria-labelledby="editorial-evidence-heading"
            >
              <h3 id="editorial-evidence-heading">Sources et preuves</h3>
              <button
                className="button button--secondary"
                type="button"
                onClick={onOpenFigureGallery}
                aria-haspopup="dialog"
              >
                Images sources
              </button>
              {block.evidence_refs.length > 0 ? (
                <ul>
                  {block.evidence_refs.map((reference, index) => (
                    <EvidenceReference
                      key={`${block.block_id}-evidence-${index}`}
                      reference={reference}
                      index={index}
                    />
                  ))}
                </ul>
              ) : (
                <p>Aucune source associée à ce bloc.</p>
              )}
            </section>
          </>
        ) : (
          <p>Sélectionnez un paragraphe pour afficher son contexte exact.</p>
        )}
      </div>
    </aside>
  );
}
