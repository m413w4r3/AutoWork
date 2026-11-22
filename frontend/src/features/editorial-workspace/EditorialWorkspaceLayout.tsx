import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../../api/editions";
import type {
  EditorialCompositionStatusView,
  EditorialEnrichmentElementRef,
  EditorialPdfStatusView,
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { Link } from "../../routing";
import { editorialReadOnlyReasonText } from "../publication/publicationReadOnly";
import { EditorialDocument } from "./EditorialDocument";
import type { DocumentMoveState } from "./EditorialDocument";
import type { EditorialDraftController } from "./EditorialDrafts";
import type { InlineTextEditorProps } from "./InlineTextEditor";
import { EditorialInspector } from "./EditorialInspector";
import type { InspectorTab } from "./EditorialInspector";
import { manualEditOperationFor } from "./manualEditOperation";
import { EditorialOutline } from "./EditorialOutline";
import { EditorialServerDraftStatus } from "./EditorialServerDraftStatus";
import { RemoveElementDialog } from "./RemoveElementDialog";
import { SourceFigureGallery } from "./SourceFigureGallery";
import { EditorialTopBar } from "./EditorialTopBar";
import { EditorialHistory } from "./EditorialHistory";
import { useManualVisualRevision } from "./useManualVisualRevision";
import { useReplaceFigureFlow } from "./useReplaceFigureFlow";
import {
  visualElementAnchorId,
  visualElementIdentity,
} from "./visualElementOperations";
import type { VisualElementIdentity } from "./visualElementOperations";
import { VisualWorkspaceProvider } from "./VisualWorkspaceContext";
import {
  anchorLabelFromSnapshot,
  type VisualWorkspaceActions,
} from "./visualWorkspaceContext";

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
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("discussion");
  const [focusComposerRequest, setFocusComposerRequest] = useState(0);
  const [bridgeProposalPrompt, setBridgeProposalPrompt] = useState<
    string | null
  >(null);
  const [manualSaving, setManualSaving] = useState(false);
  const [manualConflict, setManualConflict] = useState(false);
  const [pendingProposal, setPendingProposal] = useState(false);
  const [captionEditingBlockId, setCaptionEditingBlockId] = useState<
    string | null
  >(null);
  const [moveState, setMoveState] = useState<DocumentMoveState | null>(null);
  const [reorderBlockId, setReorderBlockId] = useState<string | null>(null);
  const [removeBlockId, setRemoveBlockId] = useState<string | null>(null);
  const [removeError, setRemoveError] = useState<string | null>(null);
  const [visualError, setVisualError] = useState<string | null>(null);
  const [visualAnnouncement, setVisualAnnouncement] = useState<{
    key: string;
    text: string;
  } | null>(null);
  const [selectionRestoreTick, setSelectionRestoreTick] = useState(0);
  const pendingElementSelectionRef = useRef<{
    identity: VisualElementIdentity;
    previousBlockId: string;
  } | null>(null);
  const manualVisual = useManualVisualRevision(subjectId);
  const visualSaving = manualVisual.isPending;
  const overlayOpenerRef = useRef<HTMLElement | null>(null);
  const historyOpenerRef = useRef<HTMLButtonElement | null>(null);
  const moveOpenerRef = useRef<HTMLElement | null>(null);
  const captionOpenerRef = useRef<HTMLElement | null>(null);
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
    // At >= 1024px the inspector is already a lateral column; opening the
    // "inspector" panel there would render a scrim over a non-overlay column
    // and make the document unclickable. Only the narrow-viewport drawer needs it.
    const inspectorIsDrawer =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(max-width: 1023px)").matches;
    if (inspectorIsDrawer) setOpenPanel("inspector");
  }, [onSelectBlock, ordered]);

  const canEditBlock = (block: EditorialWorkspaceBlock | null) =>
    Boolean(
      block &&
      !readOnly &&
      snapshot.capabilities.manual_edit &&
      manualEditOperationFor(block),
    );

  const selectBlock = (blockId: string) => {
    if (blockId !== selectedBlockId) {
      setEditingBlockId(null);
      setCaptionEditingBlockId(null);
      setMoveState(null);
      setReorderBlockId(null);
      setVisualError(null);
    }
    onSelectBlock(blockId);
  };

  const announceVisual = (text: string) => {
    setVisualAnnouncement({ key: crypto.randomUUID(), text });
  };

  const visualFailureMessage = (error: unknown, fallback: string) =>
    error instanceof Error ? error.message : fallback;

  const restoreElementSelection = useCallback(
    (
      kind: VisualElementIdentity["kind"],
      key: string,
      previousBlockId = "",
    ) => {
      pendingElementSelectionRef.current = {
        identity: { kind, key },
        previousBlockId,
      };
      // The snapshot may already be refreshed when a proposal card accepts:
      // bump the tick so the restore effect re-evaluates the current blocks.
      setSelectionRestoreTick((tick) => tick + 1);
    },
    [],
  );

  const replaceFlow = useReplaceFigureFlow(subjectId, snapshot, {
    onRefresh,
    onAccepted: (target) =>
      restoreElementSelection("figure", target.elementKey, target.blockId),
  });

  useEffect(() => {
    const pending = pendingElementSelectionRef.current;
    if (!pending) return;
    const match = ordered.find(
      (block) =>
        block.element_kind === pending.identity.kind &&
        block.element_key === pending.identity.key,
    );
    if (!match) return;
    // Before the refreshed snapshot arrives the old occurrence still matches;
    // selecting it would defeat the identity-based restore.
    if (match.block_id === pending.previousBlockId) return;
    pendingElementSelectionRef.current = null;
    onSelectBlock(match.block_id);
    setCaptionEditingBlockId(null);
    setMoveState(null);
    setReorderBlockId(null);
    setVisualError(null);
    window.requestAnimationFrame(() => {
      const element = document.querySelector<HTMLElement>(
        `.editorial-document__block[data-block-id="${CSS.escape(match.block_id)}"]`,
      );
      element?.focus({ preventScroll: true });
      element?.scrollIntoView?.({ block: "nearest" });
    });
  }, [onSelectBlock, ordered, selectionRestoreTick]);

  const startCaptionEditing = (blockId: string) => {
    if (readOnly) return;
    captionOpenerRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    selectBlock(blockId);
    setMoveState(null);
    setReorderBlockId(null);
    setVisualError(null);
    setCaptionEditingBlockId(blockId);
    window.requestAnimationFrame(() => {
      document
        .querySelector<HTMLElement>(
          `.editorial-document__block[data-block-id="${CSS.escape(blockId)}"]`,
        )
        ?.scrollIntoView?.({ block: "nearest" });
    });
  };

  const closeCaptionEditing = (blockId: string) => {
    setCaptionEditingBlockId(null);
    const opener = captionOpenerRef.current;
    captionOpenerRef.current = null;
    window.requestAnimationFrame(() => {
      if (opener?.isConnected) {
        opener.focus({ preventScroll: true });
        return;
      }
      document
        .querySelector<HTMLElement>(
          `.editorial-document__block[data-block-id="${CSS.escape(blockId)}"]`,
        )
        ?.focus({ preventScroll: true });
    });
  };

  const saveCaption = async (blockId: string, caption: string | null) => {
    const block = ordered.find((candidate) => candidate.block_id === blockId);
    const identity = block ? visualElementIdentity(block) : null;
    if (!block || !identity) {
      throw new Error("Ce visuel n’existe plus dans cette version.");
    }
    pendingElementSelectionRef.current = {
      identity,
      previousBlockId: block.block_id,
    };
    try {
      const result = await manualVisual.mutateAsync({
        occurrenceId: block.block_id,
        revision: {
          snapshot_id: snapshot.snapshot_id,
          operation: "update_visual_caption",
          element_kind: identity.kind,
          element_key: identity.key,
          caption,
        },
      });
      closeCaptionEditing(block.block_id);
      announceVisual(
        `Légende enregistrée · v${result.acceptance.artifact_version}`,
      );
    } catch (error) {
      pendingElementSelectionRef.current = null;
      throw error;
    }
  };

  const startMove = (
    blockId: string,
    focus: "document" | "inspector" = "document",
  ) => {
    if (readOnly) return;
    const block = ordered.find((candidate) => candidate.block_id === blockId);
    if (!block || !visualElementIdentity(block)) return;
    moveOpenerRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    selectBlock(blockId);
    setCaptionEditingBlockId(null);
    setReorderBlockId(null);
    setVisualError(null);
    setMoveState({
      elementBlockId: blockId,
      draftAnchorId: null,
      error: null,
      saving: false,
    });
    window.requestAnimationFrame(() => {
      const selector =
        focus === "inspector"
          ? ".visual-move-panel input[type='search']"
          : ".visual-move-banner";
      document
        .querySelector<HTMLElement>(selector)
        ?.focus({ preventScroll: true });
    });
  };

  const chooseMoveAnchor = (anchorBlockId: string) => {
    setMoveState((current) =>
      current
        ? {
            ...current,
            draftAnchorId: anchorBlockId,
            error:
              anchorBlockId === current.draftAnchorId ? current.error : null,
          }
        : current,
    );
  };

  const cancelMove = useCallback(() => {
    if (moveState?.saving) return;
    setMoveState(null);
    const opener = moveOpenerRef.current;
    moveOpenerRef.current = null;
    window.requestAnimationFrame(() => {
      if (opener?.isConnected) {
        opener.focus({ preventScroll: true });
        return;
      }
      document
        .querySelector<HTMLElement>(
          ".editorial-document__block[data-selected='true']",
        )
        ?.focus({ preventScroll: true });
    });
  }, [moveState]);

  const confirmMove = async () => {
    const current = moveState;
    if (!current || current.saving) return;
    const block = ordered.find(
      (candidate) => candidate.block_id === current.elementBlockId,
    );
    const identity = block ? visualElementIdentity(block) : null;
    if (!block || !identity) {
      setMoveState({
        ...current,
        error: "Ce visuel n’existe plus dans cette version.",
      });
      return;
    }
    if (!current.draftAnchorId) {
      setMoveState({
        ...current,
        error: "Choisissez un paragraphe de destination.",
      });
      return;
    }
    if (current.draftAnchorId === visualElementAnchorId(block)) {
      setMoveState({
        ...current,
        error:
          "Le visuel est déjà placé après ce paragraphe. Choisissez un autre paragraphe.",
      });
      return;
    }
    pendingElementSelectionRef.current = {
      identity,
      previousBlockId: block.block_id,
    };
    setMoveState({ ...current, saving: true, error: null });
    try {
      const result = await manualVisual.mutateAsync({
        occurrenceId: block.block_id,
        revision: {
          snapshot_id: snapshot.snapshot_id,
          operation: "move_enrichment_element",
          element_kind: identity.kind,
          element_key: identity.key,
          anchor_block_id: current.draftAnchorId,
        },
      });
      setMoveState(null);
      announceVisual(
        `Emplacement enregistré · v${result.acceptance.artifact_version}`,
      );
      window.requestAnimationFrame(() => {
        if (document.activeElement !== document.body) return;
        document
          .querySelector<HTMLElement>(
            `.editorial-document__block[data-block-id="${CSS.escape(block.block_id)}"]`,
          )
          ?.focus({ preventScroll: true });
      });
    } catch (error) {
      pendingElementSelectionRef.current = null;
      setMoveState({
        ...current,
        saving: false,
        error: visualFailureMessage(
          error,
          "L’emplacement n’a pas pu être enregistré.",
        ),
      });
    }
  };

  const startReorder = (blockId: string) => {
    if (readOnly) return;
    const block = ordered.find((candidate) => candidate.block_id === blockId);
    if (!block || !visualElementIdentity(block)) return;
    selectBlock(blockId);
    setCaptionEditingBlockId(null);
    setMoveState(null);
    setVisualError(null);
    setReorderBlockId(blockId);
  };

  const saveReorder = async (
    blockId: string,
    order: EditorialEnrichmentElementRef[],
  ) => {
    const block = ordered.find((candidate) => candidate.block_id === blockId);
    const identity = block ? visualElementIdentity(block) : null;
    const anchorBlockId = block ? visualElementAnchorId(block) : null;
    if (!block || !identity || !anchorBlockId) {
      setVisualError("Ce visuel n’a pas de paragraphe d’ancrage disponible.");
      return;
    }
    pendingElementSelectionRef.current = {
      identity,
      previousBlockId: block.block_id,
    };
    setVisualError(null);
    try {
      const result = await manualVisual.mutateAsync({
        occurrenceId: anchorBlockId,
        revision: {
          snapshot_id: snapshot.snapshot_id,
          operation: "reorder_enrichment_elements",
          anchor_block_id: anchorBlockId,
          order,
        },
      });
      setReorderBlockId(null);
      announceVisual(
        `Ordre enregistré · v${result.acceptance.artifact_version}`,
      );
    } catch (error) {
      pendingElementSelectionRef.current = null;
      setVisualError(
        visualFailureMessage(error, "L’ordre n’a pas pu être enregistré."),
      );
    }
  };

  const requestRemove = (blockId: string) => {
    if (readOnly) return;
    selectBlock(blockId);
    setRemoveError(null);
    setRemoveBlockId(blockId);
  };

  const confirmRemove = async () => {
    const block = removeBlockId
      ? ordered.find((candidate) => candidate.block_id === removeBlockId)
      : null;
    const identity = block ? visualElementIdentity(block) : null;
    if (!block || !identity) {
      setRemoveError("Ce visuel n’existe plus dans cette version.");
      return;
    }
    setRemoveError(null);
    try {
      const result = await manualVisual.mutateAsync({
        occurrenceId: block.block_id,
        revision: {
          snapshot_id: snapshot.snapshot_id,
          operation: "remove_enrichment_element",
          element_kind: identity.kind,
          element_key: identity.key,
        },
      });
      setRemoveBlockId(null);
      announceVisual(
        `Visuel retiré du document · v${result.acceptance.artifact_version}. Il reste dans les sources archivées et dans l’historique.`,
      );
    } catch (error) {
      setRemoveError(
        visualFailureMessage(error, "Le visuel n’a pas pu être retiré."),
      );
    }
  };

  const requestReplace = (blockId: string) => {
    if (readOnly) return;
    const block = ordered.find((candidate) => candidate.block_id === blockId);
    const identity = block ? visualElementIdentity(block) : null;
    if (!identity) return;
    replaceFlow.requestReplace(identity.key);
  };

  const blockByElementKey = (elementKey: string) =>
    ordered.find(
      (block) =>
        block.element_key === elementKey &&
        visualElementIdentity(block) !== null,
    ) ?? null;

  const visualActions: VisualWorkspaceActions = {
    selectBlock,
    requestReplace: (elementKey) => {
      const block = blockByElementKey(elementKey);
      if (block) requestReplace(block.block_id);
    },
    requestRemove: (elementKey) => {
      const block = blockByElementKey(elementKey);
      if (block) requestRemove(block.block_id);
    },
    requestCaption: (elementKey) => {
      const block = blockByElementKey(elementKey);
      if (block) startCaptionEditing(block.block_id);
    },
    requestMove: (elementKey) => {
      const block = blockByElementKey(elementKey);
      if (block) startMove(block.block_id, "inspector");
    },
  };

  const visualWorkspaceValue = {
    snapshot,
    readOnly,
    restoreElementSelection,
    anchorLabel: (blockId: string) =>
      anchorLabelFromSnapshot(snapshot, blockId),
    visualActions,
    visualMove: moveState,
    chooseMoveAnchor,
    confirmMove: () => void confirmMove(),
    cancelMove,
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
    setManualSaving(true);
    setManualConflict(false);
    try {
      await onSave(blockId, value);
      cancelDraft(blockId);
    } catch (error) {
      setManualConflict(error instanceof ApiError && error.status === 409);
      throw error;
    } finally {
      setManualSaving(false);
    }
  };

  const discussBlock = (blockId: string) => {
    selectBlock(blockId);
    setInspectorTab("discussion");
    setOpenPanel(null);
    setMenuOpen(false);
    setFocusComposerRequest((request) => request + 1);
  };

  const openSourcesForBlock = (blockId: string) => {
    selectBlock(blockId);
    setInspectorTab("sources");
    setOpenPanel(null);
    setMenuOpen(false);
    window.requestAnimationFrame(() => {
      const heading = document.getElementById("editorial-evidence-heading");
      if (typeof heading?.scrollIntoView === "function") {
        heading.scrollIntoView({ block: "nearest" });
      }
    });
  };

  const openHistoryForBlock = (blockId: string) => {
    selectBlock(blockId);
    historyOpenerRef.current =
      document.activeElement instanceof HTMLButtonElement
        ? document.activeElement
        : null;
    overlayOpenerRef.current = null;
    setHistoryOpen(true);
    setMenuOpen(false);
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
    window.requestAnimationFrame(() => {
      const selected = document.querySelector<HTMLElement>(
        '.editorial-document__block[data-selected="true"]',
      );
      if (typeof selected?.scrollIntoView === "function") {
        selected.scrollIntoView({ block: "nearest" });
      }
      opener?.focus({ preventScroll: true });
      if (document.activeElement === document.body) {
        selected?.focus({ preventScroll: true });
      }
    });
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
      if (removeBlockId) return;
      if (moveState) {
        event.preventDefault();
        cancelMove();
        return;
      }
      if (reorderBlockId) {
        event.preventDefault();
        setReorderBlockId(null);
        setVisualError(null);
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
  }, [
    closeHistory,
    historyOpen,
    moveState,
    removeBlockId,
    reorderBlockId,
    cancelMove,
  ]);

  const selectFromOutline = (blockId: string) => {
    selectBlock(blockId);
    setOpenPanel(null);
    setMenuOpen(false);
    overlayOpenerRef.current = null;
    window.requestAnimationFrame(() => {
      const target = document.querySelector<HTMLElement>(
        `.editorial-document__block[data-block-id="${CSS.escape(blockId)}"]`,
      );
      if (typeof target?.scrollIntoView === "function") {
        target.scrollIntoView({ block: "start" });
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
    const drawerViewport =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(max-width: 1023px)").matches;
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
      // Below 1024px the outline is an off-canvas drawer, so focus returns to
      // the document block; above it the visible outline item is preferred.
      const focusTarget = drawerViewport
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
    <VisualWorkspaceProvider value={visualWorkspaceValue}>
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
          readOnlyReason={readOnlyReason}
          manualEditSaving={manualSaving}
          manualEditConflict={manualConflict}
          pendingProposal={pendingProposal}
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

        {visualAnnouncement ? (
          <p
            key={visualAnnouncement.key}
            className="visual-action-status"
            role="status"
            aria-live="polite"
          >
            {visualAnnouncement.text}
          </p>
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
            data-panel={openPanel}
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
              readOnly={readOnly}
              onStartEditing={startEditing}
              onDiscuss={discussBlock}
              onOpenSources={openSourcesForBlock}
              onOpenHistory={openHistoryForBlock}
              onAskBridge={setBridgeProposalPrompt}
              onRefresh={onRefresh}
              captionEditingBlockId={captionEditingBlockId}
              moveState={moveState}
              reorderBlockId={reorderBlockId}
              visualSaving={visualSaving}
              visualError={visualError}
              onStartCaption={startCaptionEditing}
              onSaveCaption={saveCaption}
              onCancelCaption={() =>
                closeCaptionEditing(
                  captionEditingBlockId ?? selectedBlockId ?? "",
                )
              }
              onStartMove={startMove}
              onChooseMoveAnchor={chooseMoveAnchor}
              onConfirmMove={() => void confirmMove()}
              onCancelMove={cancelMove}
              onStartReorder={startReorder}
              onSaveReorder={saveReorder}
              onCancelReorder={() => {
                setReorderBlockId(null);
                setVisualError(null);
              }}
              onRequestRemove={requestRemove}
              onRequestReplace={requestReplace}
              onAskBridgeVisual={discussBlock}
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
              activeTab={inspectorTab}
              focusComposerRequest={focusComposerRequest}
              proposalPrompt={bridgeProposalPrompt}
              onProposalPromptApplied={() => setBridgeProposalPrompt(null)}
              onLocalDraftChange={(text) => {
                if (selectedBlock)
                  onLocalDraftChange(selectedBlock.block_id, text);
              }}
              onStartEditing={startEditing}
              onRefresh={onRefresh}
              onOpenFigureGallery={() => setFigureGalleryOpen(true)}
              onBackToDocument={closeActiveOverlay}
              onChangeTarget={returnToTargetChoice}
              onTabChange={setInspectorTab}
              onOpenHistoryThread={openHistoryThread}
              onPendingProposalsChange={setPendingProposal}
            />
          </div>
        </div>
        {figureGalleryOpen ? (
          <SourceFigureGallery
            subjectId={subjectId}
            onClose={() => setFigureGalleryOpen(false)}
          />
        ) : null}
        {replaceFlow.unresolvedError ? (
          <p className="visual-action-status" role="alert">
            {replaceFlow.unresolvedError}
          </p>
        ) : null}
        {replaceFlow.dialog}
        {removeBlockId
          ? (() => {
              const removeBlock = ordered.find(
                (block) => block.block_id === removeBlockId,
              );
              return removeBlock ? (
                <RemoveElementDialog
                  block={removeBlock}
                  saving={visualSaving}
                  error={removeError}
                  onConfirm={() => void confirmRemove()}
                  onCancel={() => {
                    if (visualSaving) return;
                    setRemoveBlockId(null);
                    setRemoveError(null);
                  }}
                />
              ) : null;
            })()
          : null}
      </section>
    </VisualWorkspaceProvider>
  );
}
