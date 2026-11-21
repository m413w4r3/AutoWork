import { useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import type {
  EditorialDocumentOutlineNode,
  EditorialWorkspaceBlock,
} from "../../api/editorialWorkspace";
import { OUTLINE_BLOCK_KIND_LABELS } from "./blockKindLabels";

interface EditorialOutlineProps {
  blocks: EditorialWorkspaceBlock[];
  outline: EditorialDocumentOutlineNode[];
  selectedBlockId: string | null;
  activeSectionId: string | null;
  drafts: Readonly<Record<string, string>>;
  staleDraftIds: ReadonlySet<string>;
  onSelectBlock: (blockId: string) => void;
  onResumeDraft: (blockId: string) => void;
  onCancelDraft: (blockId: string) => void;
}

interface OutlineItem {
  block: EditorialWorkspaceBlock;
  label: string;
  depth: number;
}

function matchingItems(
  blocks: EditorialWorkspaceBlock[],
  outline: EditorialDocumentOutlineNode[],
  search: string,
): OutlineItem[] {
  const blockById = new Map(blocks.map((block) => [block.block_id, block]));
  const outlineById = new Map(outline.map((node) => [node.block_id, node]));
  const referencedIds = new Set(outline.flatMap((node) => node.children));
  const childrenByParent = new Map<string, EditorialWorkspaceBlock[]>();
  for (const block of blocks) {
    if (block.parent_block_id === null) continue;
    const children = childrenByParent.get(block.parent_block_id) ?? [];
    children.push(block);
    childrenByParent.set(block.parent_block_id, children);
  }

  const normalizedSearch = search.trim().toLocaleLowerCase();
  const visited = new Set<string>();
  const visit = (blockId: string, depth: number): OutlineItem[] => {
    if (visited.has(blockId)) return [];
    visited.add(blockId);
    const block = blockById.get(blockId);
    if (!block) return [];

    const node = outlineById.get(blockId);
    const childIds = node?.children.length
      ? node.children
      : (childrenByParent.get(blockId) ?? []).map((child) => child.block_id);
    const descendants = childIds.flatMap((childId) =>
      visit(childId, depth + 1),
    );
    const label =
      node?.label.trim() ||
      block.text?.trim() ||
      OUTLINE_BLOCK_KIND_LABELS[block.kind];
    const matches =
      normalizedSearch.length === 0 ||
      label.toLocaleLowerCase().includes(normalizedSearch) ||
      (block.text?.toLocaleLowerCase().includes(normalizedSearch) ?? false);

    if (!matches && descendants.length === 0) return [];
    return [{ block, label, depth }, ...descendants];
  };

  const preamble = blocks.filter(
    (block) => block.kind === "title" || block.kind === "lead_paragraph",
  );
  const roots = outline.filter((node) => !referencedIds.has(node.block_id));
  const result = [
    ...preamble.flatMap((block) => visit(block.block_id, 0)),
    ...(roots.length ? roots : outline).flatMap((node) =>
      visit(node.block_id, 0),
    ),
  ];
  for (const block of blocks) {
    if (!visited.has(block.block_id)) result.push(...visit(block.block_id, 0));
  }
  return result;
}

export function EditorialOutline({
  blocks,
  outline,
  selectedBlockId,
  activeSectionId,
  drafts,
  staleDraftIds,
  onSelectBlock,
  onResumeDraft,
  onCancelDraft,
}: EditorialOutlineProps) {
  const [search, setSearch] = useState("");
  const [recoveredDraftId, setRecoveredDraftId] = useState<string | null>(null);
  const itemRefs = useRef(new Map<string, HTMLButtonElement>());
  const blockIds = useMemo(
    () => new Set(blocks.map((block) => block.block_id)),
    [blocks],
  );
  const missingDraftIds = useMemo(
    () => Object.keys(drafts).filter((blockId) => !blockIds.has(blockId)),
    [blockIds, drafts],
  );
  const items = useMemo(
    () => matchingItems(blocks, outline, search),
    [blocks, outline, search],
  );

  const handleArrow = (
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    event.preventDefault();
    const nextIndex =
      event.key === "ArrowDown"
        ? Math.min(index + 1, items.length - 1)
        : Math.max(index - 1, 0);
    if (nextIndex === index) return;
    const next = items[nextIndex];
    if (!next) return;
    itemRefs.current.get(next.block.block_id)?.focus();
    onSelectBlock(next.block.block_id);
  };

  return (
    <nav className="editorial-outline" aria-label="Plan éditorial">
      <h2>Plan</h2>
      <label className="editorial-outline__search">
        Rechercher dans le plan
        <input
          type="search"
          value={search}
          onChange={(event) => setSearch(event.currentTarget.value)}
        />
      </label>
      <div className="editorial-outline__tree" role="tree">
        {items.map((item, index) => {
          const selected = item.block.block_id === selectedBlockId;
          const hasDraft = Object.prototype.hasOwnProperty.call(
            drafts,
            item.block.block_id,
          );
          const staleDraft = staleDraftIds.has(item.block.block_id);
          return (
            <div
              key={item.block.block_id}
              className="editorial-outline__row"
              role="none"
            >
              <button
                ref={(element) => {
                  if (element)
                    itemRefs.current.set(item.block.block_id, element);
                  else itemRefs.current.delete(item.block.block_id);
                }}
                type="button"
                role="treeitem"
                aria-level={item.depth + 1}
                aria-selected={selected}
                aria-current={
                  item.block.block_id === activeSectionId
                    ? "location"
                    : undefined
                }
                tabIndex={
                  selected || (!selectedBlockId && index === 0) ? 0 : -1
                }
                className="editorial-outline__item"
                data-selected={selected}
                data-active-section={item.block.block_id === activeSectionId}
                data-depth={item.depth}
                onClick={() => onSelectBlock(item.block.block_id)}
                onKeyDown={(event) => {
                  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                    handleArrow(event, index);
                  }
                }}
              >
                <span className="editorial-outline__kind">
                  {OUTLINE_BLOCK_KIND_LABELS[item.block.kind]}
                </span>
                <span>{item.label}</span>
                {hasDraft ? (
                  <span className="editorial-local-draft-label">
                    Brouillon local
                  </span>
                ) : null}
                {staleDraft ? (
                  <span
                    className="editorial-local-draft-stale"
                    aria-live="polite"
                  >
                    La version du document a changé depuis ce brouillon
                  </span>
                ) : null}
              </button>
              {hasDraft ? (
                <div className="editorial-outline__draft-actions">
                  <button
                    className="editorial-outline__draft-resume"
                    type="button"
                    aria-label={`Reprendre le brouillon : ${item.label}`}
                    onClick={() => onResumeDraft(item.block.block_id)}
                  >
                    Reprendre le brouillon
                  </button>
                  <button
                    className="editorial-outline__draft-cancel"
                    type="button"
                    aria-label={`Annuler le brouillon : ${item.label}`}
                    onClick={() => {
                      onCancelDraft(item.block.block_id);
                      window.requestAnimationFrame(() =>
                        itemRefs.current
                          .get(item.block.block_id)
                          ?.focus({ preventScroll: true }),
                      );
                    }}
                  >
                    Annuler
                  </button>
                </div>
              ) : null}
            </div>
          );
        })}
        {items.length === 0 ? <p>Aucun résultat dans le plan.</p> : null}
      </div>
      {missingDraftIds.length > 0 ? (
        <section
          className="editorial-outline__missing-drafts"
          aria-label="Brouillons de blocs introuvables"
        >
          <h3>Brouillons conservés</h3>
          {missingDraftIds.map((blockId) => (
            <div className="editorial-outline__missing-draft" key={blockId}>
              <p>Bloc introuvable</p>
              {staleDraftIds.has(blockId) ? (
                <p className="editorial-local-draft-stale" aria-live="polite">
                  La version du document a changé depuis ce brouillon
                </p>
              ) : null}
              <div className="editorial-outline__draft-actions">
                <button
                  type="button"
                  className="editorial-outline__draft-resume"
                  onClick={() => setRecoveredDraftId(blockId)}
                >
                  Reprendre le brouillon
                </button>
                <button
                  type="button"
                  className="editorial-outline__draft-cancel"
                  onClick={() => {
                    setRecoveredDraftId(null);
                    onCancelDraft(blockId);
                  }}
                >
                  Annuler
                </button>
              </div>
              {recoveredDraftId === blockId ? (
                <label className="editorial-outline__recovered-draft">
                  Texte récupéré du brouillon
                  <textarea readOnly value={drafts[blockId] ?? ""} rows={6} />
                </label>
              ) : null}
            </div>
          ))}
        </section>
      ) : null}
    </nav>
  );
}
