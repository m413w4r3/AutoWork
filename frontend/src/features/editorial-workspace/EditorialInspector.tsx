import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import type {
  EditorialFigureCatalogItem,
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { EditorialConversation } from "./EditorialConversation";
import { EditorialHistory } from "./EditorialHistory";
import { FigureInsertionPanel } from "./FigureInsertionPanel";
import { MoveElementPanel } from "./MoveElementMode";
import { ReplaceFigureDialog } from "./ReplaceFigureDialog";
import { SourceFigureGallery } from "./SourceFigureGallery";
import { VisualInsertionComposer } from "./VisualInsertionComposer";
import { BLOCK_KIND_LABELS } from "./blockKindLabels";
import {
  isInsertionAnchor,
  supportsInsertionAfter,
} from "./insertionOperations";
import { manualEditUnavailableReason } from "./manualEditOperation";
import {
  briefFigureCapacity,
  briefFigureCapacityNote,
  figureSelectionLabel,
  resolveReplaceFigureTarget,
  type ReplaceFigureTarget,
  type VisualActionCallbacks,
} from "./replaceFigureContract";
import { useVisualWorkspaceContext } from "./visualWorkspaceContext";

export type InspectorTab =
  "discussion" | "modifier" | "visuels" | "sources" | "historique";

const TABS: { id: InspectorTab; label: string }[] = [
  { id: "discussion", label: "Discussion Bridge" },
  { id: "modifier", label: "Modifier" },
  { id: "visuels", label: "Visuels" },
  { id: "sources", label: "Sources" },
  { id: "historique", label: "Historique" },
];

const VISUAL_BLOCK_KINDS = new Set<EditorialWorkspaceBlock["kind"]>([
  "table",
  "diagram",
  "chart",
  "figure",
]);

interface EditorialInspectorProps extends VisualActionCallbacks {
  subjectId: string;
  snapshot: EditorialWorkspaceSnapshot;
  block: EditorialWorkspaceBlock | null;
  sectionTitle: string | null;
  readOnly: boolean;
  isEditing: boolean;
  localDraftText: string;
  activeTab: InspectorTab;
  focusComposerRequest?: number;
  proposalPrompt?: string | null;
  onProposalPromptApplied?: () => void;
  onLocalDraftChange: (text: string) => void;
  onOpenFigureGallery: () => void;
  onStartEditing: (blockId: string) => void;
  onRefresh: () => void;
  onBackToDocument: () => void;
  onChangeTarget: () => void;
  onTabChange: (tab: InspectorTab) => void;
  onOpenHistoryThread: (occurrenceId: string) => void;
  onPendingProposalsChange?: (pending: boolean) => void;
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

function insertionUnavailableReason({
  block,
  readOnly,
  canAddEnrichment,
}: {
  block: EditorialWorkspaceBlock;
  readOnly: boolean;
  canAddEnrichment: boolean;
}): string | null {
  if (readOnly) {
    return "L’atelier est en lecture seule : aucune insertion ne peut être préparée.";
  }
  if (!canAddEnrichment) {
    return "La préparation d’insertions n’est pas activée pour cet atelier.";
  }
  if (!isInsertionAnchor(block)) {
    return "Seuls un paragraphe ou le chapeau peuvent recevoir une insertion après eux.";
  }
  if (!supportsInsertionAfter(block)) {
    return "Aucune insertion après ce bloc n’est autorisée par l’instantané serveur.";
  }
  return null;
}

function excerpt(text: string, maxLength = 90): string {
  const normalized = text.trim();
  return normalized.length > maxLength
    ? `${normalized.slice(0, maxLength - 1).trimEnd()}…`
    : normalized;
}

function visualCaption(block: EditorialWorkspaceBlock): string {
  if (block.kind === "figure") {
    return (
      block.figure?.caption ?? block.caption ?? block.text ?? "Sans légende"
    );
  }
  if (block.kind === "chart") {
    return block.chart?.title ?? block.caption ?? "Sans titre";
  }
  if (block.kind === "table") {
    return block.table?.title ?? block.caption ?? "Sans titre";
  }
  if (block.kind === "diagram") {
    return block.diagram?.title ?? block.caption ?? "Sans titre";
  }
  return block.text ?? "Sans légende";
}

function visualAnchorBlockId(block: EditorialWorkspaceBlock): string | null {
  switch (block.kind) {
    case "figure":
      return block.figure?.anchor_block_id ?? null;
    case "chart":
      return block.chart?.anchor_block_id ?? null;
    case "table":
      return block.table?.anchor_block_id ?? null;
    case "diagram":
      return block.diagram?.anchor_block_id ?? null;
    default:
      return null;
  }
}

function visualPlacement(
  block: EditorialWorkspaceBlock,
):
  | "after_paragraph"
  | "after_lead"
  | "after_timeline"
  | "after_section"
  | "end"
  | null {
  switch (block.kind) {
    case "figure":
      return block.figure?.placement ?? null;
    case "table":
      return block.table?.placement ?? null;
    case "diagram":
      return block.diagram?.placement ?? null;
    default:
      return null;
  }
}

function visualPositionLabel(
  block: EditorialWorkspaceBlock,
  snapshot: EditorialWorkspaceSnapshot,
): string {
  const anchorBlockId = visualAnchorBlockId(block);
  if (anchorBlockId) {
    const anchor = snapshot.blocks.find(
      (candidate) => candidate.block_id === anchorBlockId,
    );
    return anchor?.text?.trim()
      ? `après « ${excerpt(anchor.text)} »`
      : "après le paragraphe d’ancrage";
  }
  switch (visualPlacement(block)) {
    case "after_lead":
      return "après le chapeau";
    case "after_section":
      return "après la section";
    case "after_timeline":
      return "après la chronologie";
    case "end":
      return "en fin de document";
    default:
      return "emplacement non résolu";
  }
}

function documentVisuals(
  snapshot: EditorialWorkspaceSnapshot,
): EditorialWorkspaceBlock[] {
  return snapshot.blocks
    .filter((block) => VISUAL_BLOCK_KINDS.has(block.kind))
    .sort((left, right) => left.position - right.position);
}

export function EditorialInspector({
  subjectId,
  snapshot,
  block,
  sectionTitle,
  readOnly,
  isEditing,
  localDraftText,
  activeTab,
  focusComposerRequest,
  proposalPrompt = null,
  onProposalPromptApplied,
  onLocalDraftChange,
  onOpenFigureGallery,
  onStartEditing,
  onRefresh,
  onBackToDocument,
  onChangeTarget,
  onTabChange,
  onOpenHistoryThread,
  onPendingProposalsChange,
  onSelectBlock,
  onRequestReplace,
  onRequestRemove,
  onRequestCaption,
  onRequestMove,
}: EditorialInspectorProps) {
  const visualContext = useVisualWorkspaceContext();
  const workspaceActions = visualContext?.visualActions ?? null;
  // The workspace provider supplies the single implementation of every visual
  // action. Standalone renders (unit tests, legacy routes) keep the props.
  const selectBlock = onSelectBlock ?? workspaceActions?.selectBlock;
  const requestReplaceHandler =
    onRequestReplace ?? workspaceActions?.requestReplace;
  const requestRemoveHandler =
    onRequestRemove ?? workspaceActions?.requestRemove;
  const requestCaptionHandler =
    onRequestCaption ?? workspaceActions?.requestCaption;
  const requestMoveHandler = onRequestMove ?? workspaceActions?.requestMove;
  const visualMove = visualContext?.visualMove ?? null;
  const moveElement = visualMove
    ? (snapshot.blocks.find(
        (candidate) => candidate.block_id === visualMove.elementBlockId,
      ) ?? null)
    : null;
  const tabRefs = useRef(new Map<InspectorTab, HTMLButtonElement>());
  const [draftEditorBlockId, setDraftEditorBlockId] = useState<string | null>(
    null,
  );
  const [replaceTarget, setReplaceTarget] =
    useState<ReplaceFigureTarget | null>(null);
  const [archivedGalleryOpen, setArchivedGalleryOpen] = useState(false);
  const [archivedSelection, setArchivedSelection] = useState<
    EditorialFigureCatalogItem[]
  >([]);
  const [seededSelection, setSeededSelection] = useState<
    EditorialFigureCatalogItem[] | null
  >(null);
  const editReason = manualEditUnavailableReason({
    block,
    readOnly,
    manualEditEnabled: snapshot.capabilities.manual_edit,
  });
  const insertionReason = block
    ? insertionUnavailableReason({
        block,
        readOnly,
        canAddEnrichment: snapshot.capabilities.can_add_enrichment,
      })
    : "Sélectionnez un bloc pour préparer une insertion.";
  const visuals = documentVisuals(snapshot);
  const briefCapacity = briefFigureCapacity(snapshot);
  const briefCapacityNote = briefFigureCapacityNote(snapshot);
  const canInsertArchivedFigures = Boolean(
    block &&
    !readOnly &&
    snapshot.capabilities.can_add_enrichment &&
    isInsertionAnchor(block) &&
    block.allowed_operations.includes("create_source_figure"),
  );

  useEffect(() => {
    setArchivedSelection([]);
    setSeededSelection(null);
  }, [block?.block_id]);

  useEffect(() => {
    if (!focusComposerRequest || activeTab !== "discussion") return;
    const composer = document.getElementById("editorial-message-composer");
    if (!(composer instanceof HTMLElement)) return;
    composer.focus({ preventScroll: true });
    composer.scrollIntoView?.({ block: "nearest" });
  }, [activeTab, focusComposerRequest]);

  const handleTabKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    if (
      event.key !== "ArrowRight" &&
      event.key !== "ArrowLeft" &&
      event.key !== "Home" &&
      event.key !== "End"
    ) {
      return;
    }
    event.preventDefault();
    const nextIndex =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? TABS.length - 1
          : event.key === "ArrowRight"
            ? (index + 1) % TABS.length
            : (index - 1 + TABS.length) % TABS.length;
    const next = TABS[nextIndex];
    if (!next) return;
    onTabChange(next.id);
    tabRefs.current.get(next.id)?.focus({ preventScroll: true });
  };

  const closeHistoryTab = () => {
    onTabChange("discussion");
    window.requestAnimationFrame(() =>
      tabRefs.current.get("discussion")?.focus({ preventScroll: true }),
    );
  };

  const requestReplace = (visualBlock: EditorialWorkspaceBlock) => {
    const elementKey = visualBlock.element_key;
    if (!elementKey) return;
    if (requestReplaceHandler) {
      requestReplaceHandler(elementKey);
      return;
    }
    const target = resolveReplaceFigureTarget(snapshot, elementKey);
    if (target) setReplaceTarget(target);
  };

  const panelId = (tab: InspectorTab) => `editorial-inspector-panel-${tab}`;
  const tabId = (tab: InspectorTab) => `editorial-inspector-tab-${tab}`;

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
            <div
              className="editorial-inspector-tabs"
              role="tablist"
              aria-label="Panneaux de l’inspecteur"
            >
              {TABS.map((tab, index) => (
                <button
                  key={tab.id}
                  ref={(element) => {
                    if (element) tabRefs.current.set(tab.id, element);
                    else tabRefs.current.delete(tab.id);
                  }}
                  id={tabId(tab.id)}
                  className="editorial-inspector-tab"
                  type="button"
                  role="tab"
                  aria-selected={activeTab === tab.id}
                  aria-controls={panelId(tab.id)}
                  tabIndex={activeTab === tab.id ? 0 : -1}
                  onClick={() => onTabChange(tab.id)}
                  onKeyDown={(event) => handleTabKeyDown(event, index)}
                >
                  {tab.label}
                </button>
              ))}
            </div>
            <div
              className="editorial-inspector-panel"
              role="tabpanel"
              id={panelId("discussion")}
              aria-labelledby={tabId("discussion")}
              tabIndex={0}
              hidden={activeTab !== "discussion"}
            >
              <EditorialConversation
                subjectId={subjectId}
                occurrenceId={block.block_id}
                snapshotId={snapshot.snapshot_id}
                targetLabel={block.text ?? BLOCK_KIND_LABELS[block.kind]}
                allowedOperations={block.allowed_operations}
                proposalPrompt={proposalPrompt}
                onProposalPromptApplied={onProposalPromptApplied}
                onChangeTarget={onChangeTarget}
                onPendingProposalsChange={onPendingProposalsChange}
              />
            </div>

            <div
              className="editorial-inspector-panel"
              role="tabpanel"
              id={panelId("modifier")}
              aria-labelledby={tabId("modifier")}
              tabIndex={0}
              hidden={activeTab !== "modifier"}
            >
              <section
                className="editorial-inspector__modifier"
                aria-label="Modification directe"
              >
                {isEditing ? (
                  <p role="status">
                    L’édition de ce bloc est ouverte dans le document.
                  </p>
                ) : editReason ? (
                  <p className="editorial-inspector__reason">{editReason}</p>
                ) : (
                  <button
                    className="button button--secondary"
                    type="button"
                    onClick={() => onStartEditing(block.block_id)}
                  >
                    Modifier le texte
                  </button>
                )}
                {!readOnly ? (
                  localDraftText.length > 0 ||
                  draftEditorBlockId === block.block_id ? (
                    <label className="editorial-local-draft">
                      <span>Brouillon local</span>
                      <textarea
                        className="editorial-local-draft__input"
                        aria-label="Brouillon local"
                        value={localDraftText}
                        onChange={(event) =>
                          onLocalDraftChange(event.currentTarget.value)
                        }
                        rows={3}
                      />
                      <span className="editorial-local-draft__privacy">
                        Conservé en mémoire dans cet écran jusqu’à sa fermeture.
                      </span>
                    </label>
                  ) : (
                    <button
                      className="editorial-local-draft__toggle"
                      type="button"
                      onClick={() => setDraftEditorBlockId(block.block_id)}
                    >
                      Écrire un brouillon local
                    </button>
                  )
                ) : null}
              </section>
            </div>

            <div
              className="editorial-inspector-panel"
              role="tabpanel"
              id={panelId("visuels")}
              aria-labelledby={tabId("visuels")}
              tabIndex={0}
              hidden={activeTab !== "visuels"}
            >
              <section
                className="editorial-visuals"
                aria-label="Visuels du document"
              >
                <section
                  className="editorial-visuals__document"
                  aria-labelledby="editorial-visuals-document-heading"
                >
                  <h3 id="editorial-visuals-document-heading">
                    Éléments visuels du document
                  </h3>
                  {visuals.length === 0 ? (
                    <p className="editorial-visuals__empty">
                      Ce document ne contient encore aucun élément visuel.
                    </p>
                  ) : (
                    <ol
                      className="editorial-visuals__list"
                      aria-label="Éléments visuels du document"
                    >
                      {visuals.map((visual) => {
                        const caption = visualCaption(visual);
                        const elementKey = visual.element_key;
                        const operations = visual.allowed_operations;
                        const canReplace = Boolean(
                          !readOnly &&
                          visual.kind === "figure" &&
                          elementKey &&
                          operations.includes("replace_source_figure"),
                        );
                        const canRemove = Boolean(
                          !readOnly &&
                          elementKey &&
                          requestRemoveHandler &&
                          operations.some((operation) =>
                            operation.startsWith("delete_"),
                          ),
                        );
                        const canCaption = Boolean(
                          !readOnly &&
                          elementKey &&
                          requestCaptionHandler &&
                          operations.includes("update_visual_caption"),
                        );
                        const canMove = Boolean(
                          !readOnly &&
                          elementKey &&
                          requestMoveHandler &&
                          operations.includes("move_enrichment_element"),
                        );
                        return (
                          <li
                            key={visual.block_id}
                            className="editorial-visuals__item"
                            data-kind={visual.kind}
                          >
                            {selectBlock ? (
                              <button
                                className="editorial-visuals__target"
                                type="button"
                                aria-label={`Sélectionner ${BLOCK_KIND_LABELS[visual.kind]} : ${caption}`}
                                onClick={() => selectBlock(visual.block_id)}
                              >
                                <span className="editorial-visuals__kind">
                                  {BLOCK_KIND_LABELS[visual.kind]}
                                </span>
                                <span className="editorial-visuals__caption">
                                  {caption}
                                </span>
                              </button>
                            ) : (
                              <div className="editorial-visuals__target">
                                <span className="editorial-visuals__kind">
                                  {BLOCK_KIND_LABELS[visual.kind]}
                                </span>
                                <span className="editorial-visuals__caption">
                                  {caption}
                                </span>
                              </div>
                            )}
                            <p className="editorial-visuals__position">
                              {visualPositionLabel(visual, snapshot)}
                            </p>
                            {canReplace ||
                            canRemove ||
                            canCaption ||
                            canMove ? (
                              <div className="editorial-visuals__actions">
                                {canReplace ? (
                                  <button
                                    className="button button--secondary"
                                    type="button"
                                    onClick={() => requestReplace(visual)}
                                  >
                                    Remplacer
                                  </button>
                                ) : null}
                                {canCaption ? (
                                  <button
                                    className="button button--secondary"
                                    type="button"
                                    onClick={() =>
                                      requestCaptionHandler?.(elementKey ?? "")
                                    }
                                  >
                                    Modifier la légende
                                  </button>
                                ) : null}
                                {canMove ? (
                                  <button
                                    className="button button--secondary"
                                    type="button"
                                    onClick={() =>
                                      requestMoveHandler?.(elementKey ?? "")
                                    }
                                  >
                                    Déplacer
                                  </button>
                                ) : null}
                                {canRemove ? (
                                  <button
                                    className="button button--secondary"
                                    type="button"
                                    onClick={() =>
                                      requestRemoveHandler?.(elementKey ?? "")
                                    }
                                  >
                                    Retirer
                                  </button>
                                ) : null}
                              </div>
                            ) : null}
                          </li>
                        );
                      })}
                    </ol>
                  )}
                </section>

                {visualContext && visualMove && moveElement ? (
                  <MoveElementPanel
                    snapshot={snapshot}
                    element={moveElement}
                    draftAnchorId={visualMove.draftAnchorId}
                    saving={visualMove.saving}
                    error={visualMove.error}
                    onChooseAnchor={visualContext.chooseMoveAnchor}
                    onConfirm={visualContext.confirmMove}
                    onCancel={visualContext.cancelMove}
                  />
                ) : null}

                <section
                  className="editorial-visuals__archive"
                  aria-labelledby="editorial-visuals-archive-heading"
                >
                  <h4 id="editorial-visuals-archive-heading">
                    Images archivées
                  </h4>
                  <p className="editorial-visuals__archive-note">
                    Consultez les figures conservées dans les sources. Aucun
                    téléchargement n’est proposé.
                  </p>
                  <button
                    className="button button--secondary"
                    type="button"
                    aria-haspopup="dialog"
                    onClick={() => setArchivedGalleryOpen(true)}
                  >
                    Ouvrir la galerie des images archivées
                  </button>
                  {archivedSelection.length > 0 ? (
                    <div className="editorial-visuals__selection" role="status">
                      <p>{figureSelectionLabel(archivedSelection.length)}</p>
                      {canInsertArchivedFigures ? (
                        <button
                          className="button"
                          type="button"
                          onClick={() =>
                            setSeededSelection([...archivedSelection])
                          }
                        >
                          Préparer l’intégration des images sélectionnées
                        </button>
                      ) : (
                        <p>
                          Sélectionnez un paragraphe d’ancrage autorisé pour
                          intégrer ces images.
                        </p>
                      )}
                    </div>
                  ) : null}
                </section>

                {seededSelection ? (
                  <section
                    className="editorial-visuals__insertion"
                    aria-label="Intégration des images sélectionnées"
                  >
                    <FigureInsertionPanel
                      subjectId={subjectId}
                      snapshot={snapshot}
                      block={block}
                      canCreateFigure={canInsertArchivedFigures}
                      onClose={() => setSeededSelection(null)}
                      onRefresh={onRefresh}
                      initialSelectedFigures={seededSelection}
                      initialGalleryOpen={false}
                    />
                  </section>
                ) : null}

                <section
                  className="editorial-visuals__insertion-entry"
                  aria-label="Ajouter après ce bloc"
                >
                  <h4>Ajouter après ce bloc</h4>
                  {insertionReason ? (
                    <p className="editorial-inspector__reason">
                      {insertionReason}
                    </p>
                  ) : (
                    <VisualInsertionComposer
                      key={block.block_id}
                      subjectId={subjectId}
                      snapshot={snapshot}
                      block={block}
                      readOnly={readOnly}
                      onRefresh={onRefresh}
                    />
                  )}
                </section>
              </section>
            </div>

            <div
              className="editorial-inspector-panel"
              role="tabpanel"
              id={panelId("sources")}
              aria-labelledby={tabId("sources")}
              tabIndex={0}
              hidden={activeTab !== "sources"}
            >
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
            </div>

            <div
              className="editorial-inspector-panel"
              role="tabpanel"
              id={panelId("historique")}
              aria-labelledby={tabId("historique")}
              tabIndex={0}
              hidden={activeTab !== "historique"}
            >
              {activeTab === "historique" ? (
                <div className="editorial-inspector__history">
                  <EditorialHistory
                    subjectId={subjectId}
                    autoFocusHeading={false}
                    onClose={closeHistoryTab}
                    onOpenThread={onOpenHistoryThread}
                  />
                </div>
              ) : null}
            </div>
          </>
        ) : (
          <p>Sélectionnez un paragraphe pour afficher son contexte exact.</p>
        )}
      </div>

      {archivedGalleryOpen ? (
        <SourceFigureGallery
          subjectId={subjectId}
          onClose={() => setArchivedGalleryOpen(false)}
          selectedFigures={archivedSelection}
          onSelectionChange={setArchivedSelection}
          selectionLimit={briefCapacity ?? undefined}
          capacityNote={briefCapacityNote ?? undefined}
        />
      ) : null}

      {replaceTarget ? (
        <ReplaceFigureDialog
          subjectId={subjectId}
          snapshot={snapshot}
          target={replaceTarget}
          onClose={() => setReplaceTarget(null)}
          onRefresh={onRefresh}
        />
      ) : null}
    </aside>
  );
}
