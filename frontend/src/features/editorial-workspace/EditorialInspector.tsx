import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import type {
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { EditorialConversation } from "./EditorialConversation";
import { EditorialHistory } from "./EditorialHistory";
import { VisualInsertionComposer } from "./VisualInsertionComposer";
import { BLOCK_KIND_LABELS } from "./blockKindLabels";
import {
  isInsertionAnchor,
  supportsInsertionAfter,
} from "./insertionOperations";
import { manualEditUnavailableReason } from "./manualEditOperation";

export type InspectorTab =
  "discussion" | "modifier" | "visuels" | "sources" | "historique";

const TABS: { id: InspectorTab; label: string }[] = [
  { id: "discussion", label: "Discussion Bridge" },
  { id: "modifier", label: "Modifier" },
  { id: "visuels", label: "Visuels" },
  { id: "sources", label: "Sources" },
  { id: "historique", label: "Historique" },
];

interface EditorialInspectorProps {
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
}: EditorialInspectorProps) {
  const tabRefs = useRef(new Map<InspectorTab, HTMLButtonElement>());
  const [draftEditorBlockId, setDraftEditorBlockId] = useState<string | null>(
    null,
  );
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
                className="editorial-inspector__visuels"
                aria-label="Insertions visuelles"
              >
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
    </aside>
  );
}
