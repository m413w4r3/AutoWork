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
import { insertionUnavailableReason } from "./insertionOperations";
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
import {
  visualCapabilityFlags,
  visualElementAnchorId,
  visualElementTitle,
  visualMutationUnavailableReason,
  visualOperationAvailability,
} from "./visualElementOperations";

export type InspectorTab =
  "discussion" | "modifier" | "visuels" | "sources" | "historique";

const TABS: { id: InspectorTab; label: string; compactLabel?: string }[] = [
  { id: "discussion", label: "Discussion Bridge", compactLabel: "Discussion" },
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

function excerpt(text: string, maxLength = 90): string {
  const normalized = text.trim();
  return normalized.length > maxLength
    ? `${normalized.slice(0, maxLength - 1).trimEnd()}…`
    : normalized;
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
  const anchorBlockId = visualElementAnchorId(block);
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
  const capabilityFlags = visualCapabilityFlags(snapshot);
  const insertionReason = block
    ? insertionUnavailableReason(block, {
        read_only: readOnly,
        can_add_enrichment: snapshot.capabilities.can_add_enrichment,
      })
    : "Sélectionnez un bloc pour préparer une insertion.";
  const visualMutationReason = readOnly
    ? null
    : visualMutationUnavailableReason(capabilityFlags);
  const visuals = documentVisuals(snapshot);
  const briefCapacity = briefFigureCapacity(snapshot);
  const briefCapacityNote = briefFigureCapacityNote(snapshot);
  const canInsertArchivedFigures = Boolean(
    block &&
    insertionUnavailableReason(block, {
      read_only: readOnly,
      can_add_enrichment: snapshot.capabilities.can_add_enrichment,
    }) === null &&
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
                  {tab.compactLabel ? (
                    <>
                      <span className="editorial-inspector-tab__label">
                        {tab.label}
                      </span>
                      <span
                        className="editorial-inspector-tab__compact"
                        aria-hidden="true"
                      >
                        {tab.compactLabel}
                      </span>
                    </>
                  ) : (
                    tab.label
                  )}
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
                discussionVisible={activeTab === "discussion"}
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
                  {visualMutationReason ? (
                    <p className="editorial-inspector__reason" role="note">
                      {visualMutationReason}
                    </p>
                  ) : null}
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
                        const caption = visualElementTitle(visual);
                        const elementKey = visual.element_key;
                        const availability = visualOperationAvailability(
                          visual,
                          capabilityFlags,
                        );
                        // The inspector keeps a local dialog fallback, so a
                        // missing workspace handler is not a capability gate.
                        const canReplace = Boolean(
                          availability.replace && elementKey,
                        );
                        const canRemove = Boolean(
                          availability.remove &&
                          elementKey &&
                          requestRemoveHandler,
                        );
                        const canCaption = Boolean(
                          availability.caption &&
                          elementKey &&
                          requestCaptionHandler,
                        );
                        const canMove = Boolean(
                          availability.move && elementKey && requestMoveHandler,
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
                    readOnly={visualContext.readOnly}
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
