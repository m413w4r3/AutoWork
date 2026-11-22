import { useState } from "react";
import type { KeyboardEvent } from "react";

import {
  editorialFigureThumbnailUrl,
  type EditorialEnrichmentElementRef,
  type EditorialWorkspaceBlock,
  type EditorialWorkspaceFigure,
  type EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { BLOCK_KIND_LABELS } from "./blockKindLabels";
import { CaptionEditor } from "./CaptionEditor";
import { InlineTextEditor } from "./InlineTextEditor";
import type { InlineTextEditorProps } from "./InlineTextEditor";
import {
  INSERTION_OPERATIONS,
  supportsInsertionAfter,
} from "./insertionOperations";
import { ManualBarChartPreview } from "./ManualBarChartPreview";
import { manualEditOperationFor } from "./manualEditOperation";
import { MoveProvisionalPreview, MoveModeBanner } from "./MoveElementMode";
import { ReorderElements } from "./ReorderElements";
import { VisualActionBar } from "./VisualActionBar";
import {
  isVisualElementBlock,
  visualElementAnchorId,
  visualElementIdentity,
  visualElementsSharingAnchor,
} from "./visualElementOperations";
import { VisualInsertionComposer } from "./VisualInsertionComposer";

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

export interface DocumentMoveState {
  elementBlockId: string;
  draftAnchorId: string | null;
  error: string | null;
  saving: boolean;
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
  readOnly?: boolean;
  onStartEditing?: (blockId: string) => void;
  onDiscuss?: (blockId: string) => void;
  onOpenSources?: (blockId: string) => void;
  onOpenHistory?: (blockId: string) => void;
  onAskBridge?: (instruction: string) => void;
  onRefresh?: () => void;
  captionEditingBlockId?: string | null;
  moveState?: DocumentMoveState | null;
  reorderBlockId?: string | null;
  visualSaving?: boolean;
  visualError?: string | null;
  onStartCaption?: (blockId: string) => void;
  onSaveCaption?: (blockId: string, caption: string | null) => Promise<void>;
  onCancelCaption?: () => void;
  onStartMove?: (blockId: string) => void;
  onChooseMoveAnchor?: (anchorBlockId: string) => void;
  onConfirmMove?: () => void;
  onCancelMove?: () => void;
  onStartReorder?: (blockId: string) => void;
  onSaveReorder?: (
    blockId: string,
    order: EditorialEnrichmentElementRef[],
  ) => Promise<void>;
  onCancelReorder?: () => void;
  onRequestRemove?: (blockId: string) => void;
  onRequestReplace?: (blockId: string) => void;
  onAskBridgeVisual?: (blockId: string) => void;
}

function blockLabel(block: EditorialWorkspaceBlock): string {
  const text = block.text?.trim();
  const excerpt =
    text && text.length > 60 ? `${text.slice(0, 57).trimEnd()}…` : text;
  return excerpt
    ? `${BLOCK_KIND_LABELS[block.kind]} : ${excerpt}`
    : BLOCK_KIND_LABELS[block.kind];
}

function BlockActionBar({
  snapshot,
  block,
  readOnly,
  onStartEditing,
  onDiscuss,
  onOpenSources,
  onOpenHistory,
  onAskBridge,
  onRefresh,
}: {
  snapshot: EditorialWorkspaceSnapshot;
  block: EditorialWorkspaceBlock;
  readOnly: boolean;
  onStartEditing?: (blockId: string) => void;
  onDiscuss?: (blockId: string) => void;
  onOpenSources?: (blockId: string) => void;
  onOpenHistory?: (blockId: string) => void;
  onAskBridge?: (instruction: string) => void;
  onRefresh?: () => void;
}) {
  const operation = manualEditOperationFor(block);
  const canModify = Boolean(
    !readOnly &&
    snapshot.capabilities.manual_edit &&
    operation &&
    onStartEditing,
  );
  const canDiscuss = Boolean(
    !readOnly &&
    snapshot.capabilities.can_discuss &&
    block.text !== null &&
    onDiscuss,
  );
  const canInsertAfter = Boolean(
    !readOnly &&
    snapshot.capabilities.can_add_enrichment &&
    supportsInsertionAfter(block),
  );
  const canViewSources = Boolean(
    block.evidence_refs.length > 0 && onOpenSources,
  );
  const consumed = new Set<string>([
    ...(operation ? [operation] : []),
    ...(canInsertAfter ? INSERTION_OPERATIONS : []),
  ]);
  const unsupportedOperations = block.allowed_operations.filter(
    (candidate) =>
      !consumed.has(candidate) &&
      candidate !== "discuss" &&
      candidate !== "sources",
  );
  const canOpenMore =
    !readOnly && Boolean(onOpenHistory || unsupportedOperations.length > 0);

  if (
    !canModify &&
    !canDiscuss &&
    !canInsertAfter &&
    !canViewSources &&
    !canOpenMore
  ) {
    return null;
  }

  return (
    <div
      className="editorial-block-actions editorial-block-actions--block"
      role="toolbar"
      aria-label={`Actions du bloc : ${blockLabel(block)}`}
    >
      {canModify ? (
        <button
          className="button button--secondary"
          type="button"
          onClick={() => onStartEditing?.(block.block_id)}
        >
          Modifier
        </button>
      ) : null}
      {canDiscuss ? (
        <button
          className="button button--secondary"
          type="button"
          onClick={() => onDiscuss?.(block.block_id)}
        >
          Discuter
        </button>
      ) : null}
      {canInsertAfter ? (
        <VisualInsertionComposer
          key={block.block_id}
          subjectId={snapshot.subject_id}
          snapshot={snapshot}
          block={block}
          readOnly={readOnly}
          onAskBridge={onAskBridge}
          onRefresh={onRefresh}
        />
      ) : null}
      {canViewSources ? (
        <button
          className="button button--secondary"
          type="button"
          onClick={() => onOpenSources?.(block.block_id)}
        >
          Sources
        </button>
      ) : null}
      {canOpenMore ? (
        <details className="editorial-block-actions__more">
          <summary aria-label={`Autres actions pour ${blockLabel(block)}`}>
            ···
          </summary>
          {onOpenHistory ? (
            <button
              className="button button--secondary"
              type="button"
              onClick={() => onOpenHistory(block.block_id)}
            >
              Historique
            </button>
          ) : null}
          {unsupportedOperations.length > 0 ? (
            <p>
              Certaines opérations déclarées par le serveur ne sont pas exposées
              par cet atelier. Aucune action canonique correspondante n’est
              disponible ici.
            </p>
          ) : null}
        </details>
      ) : null}
    </div>
  );
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
  readOnly = false,
  onStartEditing,
  onDiscuss,
  onOpenSources,
  onOpenHistory,
  onAskBridge,
  onRefresh,
  captionEditingBlockId = null,
  moveState = null,
  reorderBlockId = null,
  visualSaving = false,
  visualError = null,
  onStartCaption,
  onSaveCaption,
  onCancelCaption,
  onStartMove,
  onChooseMoveAnchor,
  onConfirmMove,
  onCancelMove,
  onStartReorder,
  onSaveReorder,
  onCancelReorder,
  onRequestRemove,
  onRequestReplace,
  onAskBridgeVisual,
}: EditorialDocumentProps) {
  const [hoveredBlockId, setHoveredBlockId] = useState<string | null>(null);
  const [focusedBlockId, setFocusedBlockId] = useState<string | null>(null);
  const actionBlockId =
    hoveredBlockId ?? focusedBlockId ?? selectedBlockId ?? null;
  const moveElement = moveState
    ? (snapshot.blocks.find(
        (block) => block.block_id === moveState.elementBlockId,
      ) ?? null)
    : null;
  const moveCurrentAnchorId = moveElement
    ? visualElementAnchorId(moveElement)
    : null;
  const moveDraftAnchor =
    moveState?.draftAnchorId !== null && moveState?.draftAnchorId !== undefined
      ? (snapshot.blocks.find(
          (block) => block.block_id === moveState.draftAnchorId,
        ) ?? null)
      : null;
  const moveCurrentAnchor =
    moveCurrentAnchorId !== null
      ? (snapshot.blocks.find(
          (block) => block.block_id === moveCurrentAnchorId,
        ) ?? null)
      : null;

  return (
    <article className="editorial-document" aria-label="Document éditorial">
      {moveState && moveElement ? (
        <MoveModeBanner
          element={moveElement}
          currentAnchorText={moveCurrentAnchor?.text ?? null}
          draftAnchor={moveDraftAnchor}
          error={moveState.error}
          saving={moveState.saving}
          onCancel={() => onCancelMove?.()}
        />
      ) : null}
      {snapshot.blocks.map((block) => {
        const selected = selectedBlockId === block.block_id;
        const editing = editingBlockId === block.block_id;
        const editingCaption = captionEditingBlockId === block.block_id;
        const visual = isVisualElementBlock(block);
        const identity = visualElementIdentity(block);
        const hasDraft = Object.prototype.hasOwnProperty.call(
          drafts,
          block.block_id,
        );
        const draft = drafts[block.block_id];
        const staleDraft = staleDraftIds.has(block.block_id);
        const isMoveAnchorCandidate = Boolean(
          moveState &&
          !moveState.saving &&
          (block.kind === "paragraph" || block.kind === "lead_paragraph") &&
          block.revision_status === "ready",
        );
        const isCurrentMoveAnchor =
          isMoveAnchorCandidate && block.block_id === moveCurrentAnchorId;
        const reorderAnchorId =
          visual && reorderBlockId === block.block_id
            ? visualElementAnchorId(block)
            : null;
        return (
          <div
            key={block.block_id}
            className="editorial-document__block"
            role="group"
            aria-label={`${BLOCK_KIND_LABELS[block.kind]}${block.text ? ` : ${block.text}` : ""}`}
            aria-keyshortcuts="Enter Space"
            aria-current={selected ? "true" : undefined}
            aria-disabled={isCurrentMoveAnchor ? "true" : undefined}
            tabIndex={0}
            data-block-id={block.block_id}
            data-selected={selected}
            data-kind={block.kind}
            data-visual={visual || undefined}
            data-element-kind={identity?.kind}
            data-element-key={identity?.key}
            data-move-target={isMoveAnchorCandidate || undefined}
            onMouseEnter={() => setHoveredBlockId(block.block_id)}
            onMouseLeave={() =>
              setHoveredBlockId((current) =>
                current === block.block_id ? null : current,
              )
            }
            onFocus={() => setFocusedBlockId(block.block_id)}
            onBlur={(event) => {
              const nextFocus = event.relatedTarget;
              if (
                nextFocus instanceof Node &&
                event.currentTarget.contains(nextFocus)
              ) {
                return;
              }
              setFocusedBlockId((current) =>
                current === block.block_id ? null : current,
              );
            }}
            onClick={(event) => {
              if (
                event.target instanceof HTMLElement &&
                event.target.closest("a, button, textarea, input, select")
              ) {
                return;
              }
              if (isMoveAnchorCandidate) {
                onChooseMoveAnchor?.(block.block_id);
                return;
              }
              onSelectBlock(block.block_id);
            }}
            onKeyDown={(event: KeyboardEvent<HTMLDivElement>) => {
              if (event.target !== event.currentTarget) return;
              if (event.key !== "Enter" && event.key !== " ") return;
              event.preventDefault();
              if (isMoveAnchorCandidate) {
                onChooseMoveAnchor?.(block.block_id);
                return;
              }
              onSelectBlock(block.block_id);
            }}
          >
            {editingCaption && visual ? (
              <CaptionEditor
                block={block}
                onSave={(caption) =>
                  onSaveCaption
                    ? onSaveCaption(block.block_id, caption)
                    : Promise.reject(
                        new Error(
                          "L’enregistrement de la légende est indisponible.",
                        ),
                      )
                }
                onCancel={() => onCancelCaption?.()}
              />
            ) : editing ? (
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
            {isMoveAnchorCandidate ? (
              <p className="visual-move-target-hint">
                {isCurrentMoveAnchor
                  ? "Emplacement actuel du visuel — choisissez un autre paragraphe."
                  : moveState?.draftAnchorId === block.block_id
                    ? "Emplacement prévu pour le visuel."
                    : "Choisir comme emplacement (Entrée)."}
              </p>
            ) : null}
            {moveState &&
            moveElement &&
            moveState.draftAnchorId === block.block_id ? (
              <MoveProvisionalPreview
                element={moveElement}
                anchor={block}
                saving={moveState.saving}
                samePosition={block.block_id === moveCurrentAnchorId}
                error={moveState.error}
                onConfirm={() => onConfirmMove?.()}
                onCancel={() => onCancelMove?.()}
              />
            ) : null}
            {visual && reorderAnchorId && onSaveReorder ? (
              <ReorderElements
                anchorBlockId={reorderAnchorId}
                elements={visualElementsSharingAnchor(
                  snapshot,
                  reorderAnchorId,
                )}
                saving={visualSaving}
                error={visualError}
                onSave={(order) => void onSaveReorder(block.block_id, order)}
                onCancel={() => onCancelReorder?.()}
              />
            ) : null}
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
            {!editing &&
            !editingCaption &&
            !moveState &&
            reorderBlockId !== block.block_id &&
            visual &&
            actionBlockId === block.block_id ? (
              <VisualActionBar
                block={block}
                readOnly={readOnly}
                canDiscuss={snapshot.capabilities.can_discuss}
                onEditCaption={onStartCaption}
                onAskBridge={onAskBridgeVisual}
                onStartMove={onStartMove}
                onStartReorder={onStartReorder}
                onRequestReplace={onRequestReplace}
                onRequestRemove={onRequestRemove}
                onOpenSources={onOpenSources}
                onOpenHistory={onOpenHistory}
              />
            ) : null}
            {!editing &&
            !editingCaption &&
            !moveState &&
            reorderBlockId !== block.block_id &&
            !visual &&
            actionBlockId === block.block_id ? (
              <BlockActionBar
                snapshot={snapshot}
                block={block}
                readOnly={readOnly}
                onStartEditing={onStartEditing}
                onDiscuss={onDiscuss}
                onOpenSources={onOpenSources}
                onOpenHistory={onOpenHistory}
                onAskBridge={onAskBridge}
                onRefresh={onRefresh}
              />
            ) : null}
          </div>
        );
      })}
    </article>
  );
}
