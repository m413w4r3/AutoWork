import type { EditorialWorkspaceSnapshot } from "../../api/editorialWorkspace";
import { BLOCK_KIND_LABELS } from "./blockKindLabels";
import { InlineTextEditor } from "./InlineTextEditor";
import type { InlineTextEditorProps } from "./InlineTextEditor";

interface EditorialDocumentProps {
  snapshot: EditorialWorkspaceSnapshot;
  selectedBlockId: string | null;
  drafts: Readonly<Record<string, string>>;
  staleDraftIds: ReadonlySet<string>;
  editingBlockId: string | null;
  onSelectBlock: (blockId: string) => void;
  onDraftChange: (blockId: string, value: string) => void;
  onResumeDraft: (blockId: string) => void;
  onCancelDraft: (blockId: string) => void;
  onSave?: InlineTextEditorProps["onSave"];
}

function blockContent(block: EditorialWorkspaceSnapshot["blocks"][number]) {
  const className = "editorial-document__text";
  if (block.kind === "table" && block.table) {
    return (
      <section
        className="editorial-document__table"
        aria-label={block.table.title}
      >
        <h3>{block.table.title}</h3>
        <div className="editorial-visual-insertion__table-scroll">
          <table aria-label={block.table.title}>
            <caption>{block.table.caption || block.table.title}</caption>
            <thead>
              <tr>
                {block.table.columns.map((column) => (
                  <th key={column.key} scope="col">
                    {column.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.table.rows.map((row, rowIndex) => (
                <tr key={`${block.table?.key}-row-${rowIndex}`}>
                  {row.cells.map((cell, cellIndex) => (
                    <td
                      key={`${block.table?.key}-row-${rowIndex}-cell-${cellIndex}`}
                    >
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {block.table.rows.map((row, rowIndex) =>
          row.evidence_refs.length > 0 ? (
            <details
              className="editorial-document__table-evidence"
              key={`${block.table?.key}-evidence-${rowIndex}`}
            >
              <summary>Preuves de la ligne {rowIndex + 1}</summary>
              <ul>
                {row.evidence_refs.map((reference, referenceIndex) => (
                  <li key={`${reference.evidence_key}-${referenceIndex}`}>
                    Source {reference.source_document_id} · {reference.kind} ·{" "}
                    <code>{reference.evidence_key}</code>
                  </li>
                ))}
              </ul>
            </details>
          ) : null,
        )}
      </section>
    );
  }
  if (block.kind === "title")
    return <h2 className={className}>{block.text}</h2>;
  if (block.kind === "section_heading") {
    return <h3 className={className}>{block.text}</h3>;
  }
  if (block.kind === "lead_paragraph") {
    return (
      <p className={`${className} editorial-document__lead`}>{block.text}</p>
    );
  }
  if (block.kind === "paragraph")
    return <p className={className}>{block.text}</p>;
  return (
    <div className={`${className} editorial-document__structured`}>
      <span>{BLOCK_KIND_LABELS[block.kind]}</span>
      {block.text ? <p>{block.text}</p> : null}
    </div>
  );
}

export function EditorialDocument({
  snapshot,
  selectedBlockId,
  drafts,
  staleDraftIds,
  editingBlockId,
  onSelectBlock,
  onDraftChange,
  onResumeDraft,
  onCancelDraft,
  onSave,
}: EditorialDocumentProps) {
  return (
    <article className="editorial-document" aria-label="Document éditorial">
      {snapshot.blocks.map((block) => {
        const selected = selectedBlockId === block.block_id;
        const editing = editingBlockId === block.block_id;
        const hasDraft = Object.prototype.hasOwnProperty.call(
          drafts,
          block.block_id,
        );
        const draft = drafts[block.block_id];
        const staleDraft = staleDraftIds.has(block.block_id);
        return (
          <div
            key={block.block_id}
            className="editorial-document__block"
            tabIndex={-1}
            data-block-id={block.block_id}
            data-selected={selected}
            data-kind={block.kind}
            onClick={(event) => {
              if (
                event.target instanceof HTMLElement &&
                event.target.closest("a, button, textarea, input, select")
              ) {
                return;
              }
              onSelectBlock(block.block_id);
            }}
          >
            {editing ? (
              <InlineTextEditor
                blockId={block.block_id}
                value={draft ?? block.text ?? ""}
                hasLocalDraft={hasDraft}
                staleDraft={staleDraft}
                onChange={(value) => onDraftChange(block.block_id, value)}
                onCancel={() => onCancelDraft(block.block_id)}
                onSave={onSave}
              />
            ) : (
              blockContent(block)
            )}
            {hasDraft && !editing ? (
              <div className="editorial-document__draft-actions">
                <span className="editorial-local-draft-label">
                  Brouillon local
                </span>
                {staleDraft ? (
                  <span className="editorial-local-draft-stale">
                    La version du document a changé depuis ce brouillon
                  </span>
                ) : null}
                <button
                  className="button button--secondary"
                  type="button"
                  onClick={() => onResumeDraft(block.block_id)}
                >
                  Reprendre le brouillon
                </button>
              </div>
            ) : null}
            {selected ? (
              <span
                className="editorial-document__selected-status"
                role="status"
              >
                Bloc sélectionné
              </span>
            ) : null}
          </div>
        );
      })}
    </article>
  );
}
