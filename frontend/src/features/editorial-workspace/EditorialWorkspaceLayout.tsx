import { useEffect, useRef, useState } from "react";

import type {
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { BLOCK_KIND_LABELS } from "./blockKindLabels";
import { EditorialInspector } from "./EditorialInspector";
import { EditorialOutline } from "./EditorialOutline";
import { EditorialServerDraftStatus } from "./EditorialServerDraftStatus";
import { EditorialTopBar } from "./EditorialTopBar";

interface EditorialWorkspaceLayoutProps {
  subjectId: string;
  snapshot: EditorialWorkspaceSnapshot;
  selectedBlockId: string | null;
  localDraftText: string;
  onSelectBlock: (blockId: string) => void;
  onLocalDraftChange: (blockId: string, text: string) => void;
  onRefresh: () => void;
}

type OpenPanel = "outline" | "inspector" | null;

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

function kindElement(block: EditorialWorkspaceBlock) {
  const className = "editorial-document__text";
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

export function EditorialWorkspaceLayout({
  subjectId,
  snapshot,
  selectedBlockId,
  localDraftText,
  onSelectBlock,
  onLocalDraftChange,
  onRefresh,
}: EditorialWorkspaceLayoutProps) {
  const [openPanel, setOpenPanel] = useState<OpenPanel>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [technicalOpen, setTechnicalOpen] = useState(false);
  const overlayOpenerRef = useRef<HTMLElement | null>(null);
  // The snapshot's `blocks` array is already the server-resolved document
  // order. `enrichments` is a companion projection of those same block IDs.
  const ordered = snapshot.blocks;
  const selectedBlock =
    ordered.find((block) => block.block_id === selectedBlockId) ?? null;
  const readOnly = snapshot.publication_kind === null;
  const stale =
    snapshot.preview_status === "STALE" ||
    snapshot.blocks.some((block) => block.revision_status === "stale");
  const activeSection = selectedSection(selectedBlock, ordered);
  const sectionTitle = activeSection?.text ?? null;
  const activeSectionId = activeSection?.block_id ?? null;

  useEffect(() => {
    const closeTransientUi = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
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
  }, []);

  const selectFromOutline = (blockId: string) => {
    onSelectBlock(blockId);
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

  return (
    <section
      className="editorial-workspace"
      aria-labelledby="editorial-workspace-heading"
    >
      <EditorialTopBar
        subjectId={subjectId}
        snapshot={snapshot}
        stale={stale}
        menuOpen={menuOpen}
        technicalOpen={technicalOpen}
        onToggleMenu={toggleMenu}
        onOpenTechnicalDetails={openTechnicalDetails}
        onTechnicalOpenChange={setTechnicalOpen}
        onRefresh={onRefresh}
      />

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
          Type de publication indéterminé : le document est consultable en
          lecture seule.
        </div>
      ) : null}
      <div className="editorial-workspace__controls">
        <button
          className="button button--secondary editorial-workspace__outline-toggle"
          type="button"
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
          className="editorial-workspace__outline-column"
          data-panel-open={openPanel === "outline"}
        >
          <EditorialOutline
            blocks={snapshot.blocks}
            outline={snapshot.document_outline}
            selectedBlockId={selectedBlockId}
            activeSectionId={activeSectionId}
            onSelectBlock={selectFromOutline}
          />
        </div>
        <div className="editorial-document-column">
          <article
            className="editorial-document"
            aria-label="Document éditorial"
          >
            {ordered.map((block) => {
              const selected = block.block_id === selectedBlockId;
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
                      event.target.closest("a, button")
                    ) {
                      return;
                    }
                    onSelectBlock(block.block_id);
                  }}
                >
                  {kindElement(block)}
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
        </div>
        <div
          className="editorial-workspace__inspector-column"
          data-panel-open={openPanel === "inspector"}
        >
          <EditorialInspector
            subjectId={snapshot.subject_id}
            snapshot={snapshot}
            block={selectedBlock}
            sectionTitle={sectionTitle}
            readOnly={readOnly}
            localDraftText={localDraftText}
            onLocalDraftChange={(text) => {
              if (selectedBlock)
                onLocalDraftChange(selectedBlock.block_id, text);
            }}
            onOpenSources={showSources}
            onBackToDocument={closeActiveOverlay}
            onChangeTarget={returnToTargetChoice}
          />
        </div>
      </div>
    </section>
  );
}
