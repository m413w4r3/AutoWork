import { useState } from "react";

import {
  editorialFigureThumbnailUrl,
  type EditorialWorkspaceFigure,
  type EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { BLOCK_KIND_LABELS } from "./blockKindLabels";
import { InlineTextEditor } from "./InlineTextEditor";
import type { InlineTextEditorProps } from "./InlineTextEditor";
import { ManualBarChartPreview } from "./ManualBarChartPreview";

function EditorialSourceFigure({
  subjectId,
  figure,
  allowedOperations,
}: {
  subjectId: string;
  figure: EditorialWorkspaceFigure;
  allowedOperations: string[];
}) {
  const [imageUnavailable, setImageUnavailable] = useState(false);
  return (
    <figure
      className="editorial-document__source-figure"
      aria-label={figure.caption}
    >
      {figure.catalog_figure_id && !imageUnavailable ? (
        <img
          src={editorialFigureThumbnailUrl(subjectId, figure.catalog_figure_id)}
          alt={figure.caption}
          loading="lazy"
          onError={() => setImageUnavailable(true)}
        />
      ) : (
        <p className="editorial-document__figure-unavailable" role="status">
          Aperçu indisponible
        </p>
      )}
      <figcaption>{figure.caption}</figcaption>
      {figure.source ? (
        <p className="editorial-document__figure-source">{figure.source}</p>
      ) : null}
      <p className="editorial-document__figure-provenance">
        {figure.provenance}
      </p>
      {figure.width && figure.height ? (
        <p className="editorial-document__figure-dimensions">
          {figure.width} × {figure.height} px · {figure.mime_type}
        </p>
      ) : null}
      <details className="editorial-document__figure-details">
        <summary>Provenance et détails de l’archive</summary>
        <p>Source · {figure.source_url}</p>
        {figure.page ? <p>Page · {figure.page}</p> : null}
        {figure.section ? <p>Section · {figure.section}</p> : null}
        <p>
          Type · {figure.mime_type} · {figure.byte_size} octets
        </p>
        <p>SHA-256 · {figure.sha256}</p>
        {!allowedOperations.includes("delete_source_figure") &&
        !allowedOperations.includes("replace_source_figure") ? (
          <p>
            Retrait et remplacement indisponibles : aucune opération n’est
            déclarée par l’instantané serveur.
          </p>
        ) : null}
      </details>
    </figure>
  );
}

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

function blockContent(
  subjectId: string,
  block: EditorialWorkspaceSnapshot["blocks"][number],
) {
  const className = "editorial-document__text";
  if (block.kind === "figure" && block.figure?.status === "included") {
    return (
      <EditorialSourceFigure
        subjectId={subjectId}
        figure={block.figure}
        allowedOperations={block.allowed_operations}
      />
    );
  }
  if (block.kind === "chart" && block.chart) {
    return (
      <section
        className="editorial-document__chart"
        aria-label={block.chart.title}
      >
        <ManualBarChartPreview
          title={block.chart.title}
          caption={block.chart.caption}
          legend={block.chart.legend}
          categoryAxisLabel={block.chart.category_axis_label}
          valueAxisLabel={block.chart.value_axis_label}
          unit={block.chart.unit}
          points={block.chart.points.map((point) => ({
            label: point.label,
            value: point.value,
            evidence: point.evidence_refs.map((reference) => ({
              label: `Source ${reference.source_document_id} · ${reference.kind}`,
              passage: null,
              detail: `${reference.source_document_id} · ${reference.kind} · ${reference.evidence_key}`,
            })),
          }))}
        />
      </section>
    );
  }
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
              blockContent(snapshot.subject_id, block)
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
