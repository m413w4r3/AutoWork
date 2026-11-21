import { useCallback, useEffect, useRef, useState } from "react";

import type {
  EditorialCompositionStatusView,
  EditorialPdfStatusView,
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { Link } from "../../routing";
import { editorialReadOnlyReasonText } from "../publication/publicationReadOnly";
import { EditorialDocument } from "./EditorialDocument";
import type { EditorialDraftController } from "./EditorialDrafts";
import type { InlineTextEditorProps } from "./InlineTextEditor";
import { EditorialInspector } from "./EditorialInspector";
import { EditorialOutline } from "./EditorialOutline";
import { EditorialServerDraftStatus } from "./EditorialServerDraftStatus";
import { SourceFigureGallery } from "./SourceFigureGallery";
import { EditorialTopBar } from "./EditorialTopBar";
import { EditorialHistory } from "./EditorialHistory";

interface EditorialWorkspaceLayoutProps {
  subjectId: string;
  snapshot: EditorialWorkspaceSnapshot;
  pdfStatus?: EditorialPdfStatusView;
  pdfStatusUnavailable?: boolean;
  pdfRegenerationPending?: boolean;
  pdfRegenerationError?: string | null;
  compositionStatus?: EditorialCompositionStatusView;
  localDrafts: EditorialDraftController;
  selectedBlockId: string | null;
  localDraftText: string;
  onSelectBlock: (blockId: string) => void;
  onLocalDraftChange: (blockId: string, text: string) => void;
  onRefresh: () => void;
  onRefreshPdfStatus?: () => void;
  onRegeneratePdf?: () => void;
  onSave?: InlineTextEditorProps["onSave"];
}

type OpenPanel = "outline" | "inspector" | null;
const LOCAL_DRAFT_NAVIGATION_WARNING =
  "Quitter l’atelier ? Le brouillon local en mémoire sera perdu.";

function selectedSection(
  selected: EditorialWorkspaceBlock | null,
  blocks: EditorialWorkspaceBlock[],
): EditorialWorkspaceBlock | null {
  if (!selected) return null;
  const byId = new Map(blocks.map((block) => [block.block_id, block]));
  const visited = new Set<string>();
  let current: EditorialWorkspaceBlock | undefined = selected;
  while (current && !visited.has(current.block_id)) {
    visited.add(current.block_id);
    if (current.kind === "section_heading") return current;
    current = current.parent_block_id
      ? byId.get(current.parent_block_id)
      : undefined;
  }
  return null;
}

export function EditorialWorkspaceLayout({
  subjectId,
  snapshot,
  pdfStatus,
  pdfStatusUnavailable,
  pdfRegenerationPending,
  pdfRegenerationError,
  compositionStatus,
  localDrafts,
  selectedBlockId,
  localDraftText,
  onSelectBlock,
  onLocalDraftChange,
  onRefresh,
  onRefreshPdfStatus,
  onRegeneratePdf,
  onSave,
}: EditorialWorkspaceLayoutProps) {
  const [openPanel, setOpenPanel] = useState<OpenPanel>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [technicalOpen, setTechnicalOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [figureGalleryOpen, setFigureGalleryOpen] = useState(false);
  const [editingBlockId, setEditingBlockId] = useState<string | null>(null);
  const overlayOpenerRef = useRef<HTMLElement | null>(null);
  const historyOpenerRef = useRef<HTMLButtonElement | null>(null);
  const leaveConfirmedRef = useRef(false);
  const linkedThreadHandledRef = useRef(false);
  // The snapshot's `blocks` array is already the server-resolved document
  // order. `enrichments` is a companion projection of those same block IDs.
  const ordered = snapshot.blocks;
  const selectedBlock =
    ordered.find((block) => block.block_id === selectedBlockId) ?? null;
  const readOnlyReason =
    snapshot.capabilities.read_only_reason ??
    (snapshot.publication_kind === null ? "publication_kind_unknown" : null);
  const readOnly = readOnlyReason !== null;
  const stale = snapshot.blocks.some(
    (block) => block.revision_status === "stale",
  );
  const activeSection = selectedSection(selectedBlock, ordered);
  const sectionTitle = activeSection?.text ?? null;
  const activeSectionId = activeSection?.block_id ?? null;
  const workspacePath = `/subjects/${encodeURIComponent(subjectId)}/editor`;

  useEffect(() => {
    if (linkedThreadHandledRef.current) return;
    const occurrenceId = new URLSearchParams(window.location.search).get(
      "thread",
    );
    if (
      !occurrenceId ||
      !ordered.some((block) => block.block_id === occurrenceId)
    ) {
      return;
    }
    linkedThreadHandledRef.current = true;
    onSelectBlock(occurrenceId);
    setOpenPanel("inspector");
  }, [onSelectBlock, ordered]);

  const canEditBlock = (block: EditorialWorkspaceBlock | null) =>
    Boolean(
      block &&
      block.text !== null &&
      !readOnly &&
      snapshot.capabilities.manual_edit &&
      block.allowed_operations.some(
        (operation) =>
          operation.startsWith("rewrite_") || operation === "modify",
      ),
    );

  const selectBlock = (blockId: string) => {
    if (blockId !== selectedBlockId) setEditingBlockId(null);
    onSelectBlock(blockId);
  };

  const startEditing = (blockId: string) => {
    const block = ordered.find((candidate) => candidate.block_id === blockId);
    if (!canEditBlock(block ?? null)) return;
    setEditingBlockId(blockId);
    onSelectBlock(blockId);
  };

  const resumeDraft = (blockId: string) => {
    if (!Object.prototype.hasOwnProperty.call(localDrafts.drafts, blockId)) {
      return;
    }
    const block = ordered.find((candidate) => candidate.block_id === blockId);
    if (!block) return;
    if (!localDrafts.staleDraftIds.has(blockId) && !canEditBlock(block)) return;

    setEditingBlockId(blockId);
    onSelectBlock(blockId);
    setOpenPanel(null);
    setMenuOpen(false);
    overlayOpenerRef.current = null;
    window.requestAnimationFrame(() => {
      const documentBlock = Array.from(
        document.querySelectorAll<HTMLElement>(".editorial-document__block"),
      ).find((candidate) => candidate.dataset.blockId === blockId);
      documentBlock
        ?.querySelector<HTMLTextAreaElement>(
          ".editorial-inline-editor textarea",
        )
        ?.focus();
    });
  };

  const cancelDraft = (blockId: string) => {
    localDrafts.clearDraft(blockId);
    if (editingBlockId === blockId) setEditingBlockId(null);
  };

  const updateDraft = (blockId: string, value: string) => {
    const block = ordered.find((candidate) => candidate.block_id === blockId);
    if (!block) return;
    localDrafts.updateDraft(blockId, value, block.text ?? "");
  };

  const saveDraft = async (blockId: string, value: string) => {
    if (!onSave) return;
    await onSave(blockId, value);
    cancelDraft(blockId);
  };

  const allowNavigationAway = () => {
    if (!localDrafts.hasDrafts) return true;
    const allowed = window.confirm(LOCAL_DRAFT_NAVIGATION_WARNING);
    if (allowed) leaveConfirmedRef.current = true;
    return allowed;
  };

  const closeHistory = useCallback(() => {
    setHistoryOpen(false);
    const opener = historyOpenerRef.current;
    historyOpenerRef.current = null;
    window.requestAnimationFrame(() => opener?.focus({ preventScroll: true }));
  }, []);

  useEffect(() => {
    if (!localDrafts.hasDrafts) return;
    const warnOnClose = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnOnClose);
    return () => window.removeEventListener("beforeunload", warnOnClose);
  }, [localDrafts.hasDrafts]);

  useEffect(() => {
    if (!localDrafts.hasDrafts) return;
    const warnOnHistoryNavigation = () => {
      if (leaveConfirmedRef.current) {
        leaveConfirmedRef.current = false;
        return;
      }
      if (window.location.pathname === workspacePath) return;
      if (window.confirm(LOCAL_DRAFT_NAVIGATION_WARNING)) return;
      window.history.pushState({}, "", workspacePath);
      window.dispatchEvent(new PopStateEvent("popstate"));
    };
    window.addEventListener("popstate", warnOnHistoryNavigation);
    return () =>
      window.removeEventListener("popstate", warnOnHistoryNavigation);
  }, [localDrafts.hasDrafts, workspacePath]);

  useEffect(() => {
    if (
      editingBlockId &&
      !ordered.some((block) => block.block_id === editingBlockId)
    ) {
      setEditingBlockId(null);
    }
  }, [editingBlockId, ordered]);

  useEffect(() => {
    const closeTransientUi = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (historyOpen) {
        event.preventDefault();
        closeHistory();
        return;
      }
      setOpenPanel(null);
      setMenuOpen(false);
      setTechnicalOpen(false);
      const opener = overlayOpenerRef.current;
      overlayOpenerRef.current = null;
      window.requestAnimationFrame(() => opener?.focus());
      const target = event.target;
      if (target instanceof HTMLElement) {
        const details = target.closest("details");
        if (details instanceof HTMLDetailsElement) details.open = false;
      }
    };
    window.addEventListener("keydown", closeTransientUi);
    return () => window.removeEventListener("keydown", closeTransientUi);
  }, [closeHistory, historyOpen]);

  const selectFromOutline = (blockId: string) => {
    selectBlock(blockId);
    setOpenPanel(null);
    setMenuOpen(false);
    overlayOpenerRef.current = null;
  };

  const showSources = () => {
    const compactViewport =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(max-width: 1023px)").matches;
    const activeElement = document.activeElement;
    overlayOpenerRef.current =
      compactViewport && activeElement instanceof HTMLElement
        ? activeElement
        : null;
    setOpenPanel(compactViewport ? "inspector" : null);
    window.requestAnimationFrame(() => {
      const evidenceHeading = document.getElementById(
        "editorial-evidence-heading",
      );
      if (typeof evidenceHeading?.scrollIntoView === "function") {
        evidenceHeading.scrollIntoView({ block: "nearest" });
      }
    });
  };

  const closeActiveOverlay = () => {
    const opener = overlayOpenerRef.current;
    overlayOpenerRef.current = null;
    setOpenPanel(null);
    setMenuOpen(false);
    window.requestAnimationFrame(() => opener?.focus());
  };

  const returnToTargetChoice = () => {
    const mobileViewport =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(max-width: 767px)").matches;
    setOpenPanel(null);
    setMenuOpen(false);
    overlayOpenerRef.current = null;
    window.requestAnimationFrame(() => {
      const selectedDocumentBlock = document.querySelector<HTMLElement>(
        ".editorial-document__block[data-selected='true']",
      );
      const selectedOutlineItem = document.querySelector<HTMLElement>(
        ".editorial-outline__item[data-selected='true']",
      );
      const focusTarget = mobileViewport
        ? selectedDocumentBlock
        : (selectedOutlineItem ?? selectedDocumentBlock);
      focusTarget?.focus({ preventScroll: true });
    });
  };

  const toggleMenu = (button: HTMLButtonElement) => {
    if (menuOpen) {
      overlayOpenerRef.current = null;
      setMenuOpen(false);
      return;
    }
    overlayOpenerRef.current = button;
    setOpenPanel(null);
    setMenuOpen(true);
  };

  const openTechnicalDetails = () => {
    setTechnicalOpen(true);
    setMenuOpen(false);
    window.requestAnimationFrame(() => {
      document
        .querySelector<HTMLElement>(".editorial-technical-details summary")
        ?.focus();
    });
  };

  const openHistory = (button: HTMLButtonElement) => {
    historyOpenerRef.current = button;
    overlayOpenerRef.current = null;
    setHistoryOpen(true);
    setMenuOpen(false);
  };

  const openHistoryThread = (occurrenceId: string) => {
    historyOpenerRef.current = null;
    setHistoryOpen(false);
    selectBlock(occurrenceId);
    setOpenPanel("inspector");
    setMenuOpen(false);
  };

  return (
    <section
      className="editorial-workspace"
      aria-labelledby="editorial-workspace-heading"
    >
      <EditorialTopBar
        subjectId={subjectId}
        snapshot={snapshot}
        pdfStatus={pdfStatus}
        pdfStatusUnavailable={pdfStatusUnavailable}
        pdfRegenerationPending={pdfRegenerationPending}
        pdfRegenerationError={pdfRegenerationError}
        compositionStatus={compositionStatus}
        stale={stale}
        hasLocalDrafts={localDrafts.hasDrafts}
        hasStaleLocalDrafts={localDrafts.hasStaleDrafts}
        menuOpen={menuOpen}
        technicalOpen={technicalOpen}
        onToggleMenu={toggleMenu}
        onOpenTechnicalDetails={openTechnicalDetails}
        onTechnicalOpenChange={setTechnicalOpen}
        onRefresh={onRefresh}
        onRefreshPdfStatus={onRefreshPdfStatus}
        onRegeneratePdf={onRegeneratePdf}
        onOpenHistory={openHistory}
        onNavigateAway={allowNavigationAway}
      />

      {historyOpen ? (
        <EditorialHistory
          subjectId={subjectId}
          onClose={closeHistory}
          onOpenThread={openHistoryThread}
        />
      ) : null}

      <EditorialServerDraftStatus
        subjectId={subjectId}
        occurrenceId={selectedBlock?.block_id ?? null}
        snapshotId={snapshot.snapshot_id}
        localDraftText={localDraftText}
        onRecover={(text) => {
          if (selectedBlock) onLocalDraftChange(selectedBlock.block_id, text);
        }}
      />

      {readOnly ? (
        <div className="editorial-readonly-banner" role="note">
          <strong>
            {readOnlyReason === "historical_version"
              ? "Version historique — lecture seule"
              : "Atelier en lecture seule"}
          </strong>
          <p>{editorialReadOnlyReasonText(readOnlyReason)}</p>
          {readOnlyReason === "historical_version" ? (
            <Link to={workspacePath}>Revenir à la version courante</Link>
          ) : null}
        </div>
      ) : null}
      <div className="editorial-workspace__controls">
        <button
          className="button button--secondary editorial-workspace__outline-toggle"
          type="button"
          aria-controls="editorial-workspace-outline-panel"
          aria-expanded={openPanel === "outline"}
          onClick={(event) => {
            if (openPanel === "outline") {
              overlayOpenerRef.current = null;
              setOpenPanel(null);
              return;
            }
            overlayOpenerRef.current = event.currentTarget;
            setMenuOpen(false);
            setOpenPanel("outline");
          }}
        >
          Plan
        </button>
        <button
          className="button button--secondary editorial-workspace__inspector-toggle"
          type="button"
          aria-controls="editorial-workspace-inspector-panel"
          aria-expanded={openPanel === "inspector"}
          onClick={(event) => {
            if (openPanel === "inspector") {
              overlayOpenerRef.current = null;
              setOpenPanel(null);
              return;
            }
            overlayOpenerRef.current = event.currentTarget;
            setMenuOpen(false);
            setOpenPanel("inspector");
          }}
        >
          Inspecteur
        </button>
      </div>

      {openPanel ? (
        <button
          className="editorial-workspace__scrim"
          type="button"
          aria-label="Fermer le panneau"
          onClick={closeActiveOverlay}
        />
      ) : null}
      <div className="editorial-workspace__grid">
        <div
          id="editorial-workspace-outline-panel"
          className="editorial-workspace__outline-column"
          data-panel-open={openPanel === "outline"}
        >
          <EditorialOutline
            blocks={snapshot.blocks}
            outline={snapshot.document_outline}
            selectedBlockId={selectedBlockId}
            activeSectionId={activeSectionId}
            drafts={localDrafts.drafts}
            staleDraftIds={localDrafts.staleDraftIds}
            onSelectBlock={selectFromOutline}
            onResumeDraft={resumeDraft}
            onCancelDraft={cancelDraft}
          />
        </div>
        <div className="editorial-document-column">
          <EditorialDocument
            snapshot={snapshot}
            selectedBlockId={selectedBlockId}
            drafts={localDrafts.drafts}
            staleDraftIds={localDrafts.staleDraftIds}
            editingBlockId={editingBlockId}
            onSelectBlock={selectBlock}
            onDraftChange={updateDraft}
            onResumeDraft={resumeDraft}
            onCancelDraft={cancelDraft}
            onSave={onSave ? saveDraft : undefined}
          />
        </div>
        <div
          id="editorial-workspace-inspector-panel"
          className="editorial-workspace__inspector-column"
          data-panel-open={openPanel === "inspector"}
        >
          <EditorialInspector
            subjectId={snapshot.subject_id}
            snapshot={snapshot}
            block={selectedBlock}
            sectionTitle={sectionTitle}
            readOnly={readOnly}
            isEditing={editingBlockId === selectedBlock?.block_id}
            localDraftText={localDraftText}
            onLocalDraftChange={(text) => {
              if (selectedBlock)
                onLocalDraftChange(selectedBlock.block_id, text);
            }}
            onStartEditing={startEditing}
            onRefresh={onRefresh}
            onOpenSources={showSources}
            onOpenFigureGallery={() => setFigureGalleryOpen(true)}
            onBackToDocument={closeActiveOverlay}
            onChangeTarget={returnToTargetChoice}
          />
        </div>
      </div>
      {figureGalleryOpen ? (
        <SourceFigureGallery
          subjectId={subjectId}
          onClose={() => setFigureGalleryOpen(false)}
        />
      ) : null}
    </section>
  );
}
