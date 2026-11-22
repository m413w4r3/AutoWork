import { useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";

import type {
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { BarChartInsertionPanel } from "./BarChartInsertionPanel";
import { FigureInsertionPanel } from "./FigureInsertionPanel";
import { supportsInsertionAfter } from "./insertionOperations";
import { TableInsertionPanel } from "./TableInsertionPanel";

interface VisualInsertionComposerProps {
  subjectId: string;
  snapshot: EditorialWorkspaceSnapshot;
  block: EditorialWorkspaceBlock;
  readOnly: boolean;
  onAskBridge?: (instruction: string) => void;
  onRefresh?: () => void;
}

export function VisualInsertionComposer({
  subjectId,
  snapshot,
  block,
  readOnly,
  onAskBridge,
  onRefresh,
}: VisualInsertionComposerProps) {
  const menuId = useId();
  const addButtonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [editorMode, setEditorMode] = useState<
    "table" | "chart" | "figure" | null
  >(null);
  const canInsertAfter =
    !readOnly &&
    snapshot.capabilities.can_add_enrichment &&
    supportsInsertionAfter(block);
  const canCreateTable =
    canInsertAfter && block.allowed_operations.includes("create_table");
  const canCreateBarChart =
    canInsertAfter && block.allowed_operations.includes("create_bar_chart");
  const canCreateFigure =
    canInsertAfter && block.allowed_operations.includes("create_source_figure");

  useEffect(() => {
    if (!menuOpen) return;
    menuRef.current
      ?.querySelector<HTMLButtonElement>(
        'button[role="menuitem"]:not(:disabled)',
      )
      ?.focus({ preventScroll: true });
  }, [menuOpen]);

  const handleMenuKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      setMenuOpen(false);
      window.requestAnimationFrame(() => addButtonRef.current?.focus());
      return;
    }
    if (
      event.key !== "ArrowDown" &&
      event.key !== "ArrowUp" &&
      event.key !== "Home" &&
      event.key !== "End"
    ) {
      return;
    }
    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLButtonElement>(
        'button[role="menuitem"]:not(:disabled)',
      ) ?? [],
    );
    if (items.length === 0 || !(event.target instanceof HTMLButtonElement)) {
      return;
    }
    const currentIndex = items.indexOf(event.target);
    if (currentIndex < 0) return;
    event.preventDefault();
    const nextIndex =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? items.length - 1
          : event.key === "ArrowDown"
            ? (currentIndex + 1) % items.length
            : (currentIndex - 1 + items.length) % items.length;
    items[nextIndex]?.focus({ preventScroll: true });
  };

  useEffect(() => {
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (editorMode === null && !menuOpen) return;
      event.preventDefault();
      event.stopPropagation();
      setMenuOpen(false);
      setEditorMode(null);
      window.requestAnimationFrame(() => addButtonRef.current?.focus());
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [editorMode, menuOpen]);

  if (!canInsertAfter) return null;

  const closePanel = () => {
    setEditorMode(null);
    window.requestAnimationFrame(() => addButtonRef.current?.focus());
  };

  return (
    <div className="editorial-visual-insertion">
      <button
        ref={addButtonRef}
        className="button button--secondary"
        type="button"
        aria-haspopup="menu"
        aria-controls={menuId}
        aria-expanded={menuOpen}
        onClick={() => setMenuOpen((open) => !open)}
      >
        Ajouter après
      </button>
      <div
        ref={menuRef}
        id={menuId}
        className="editorial-visual-insertion__menu"
        role="menu"
        aria-label="Ajouter après"
        hidden={!menuOpen}
        onKeyDown={handleMenuKeyDown}
      >
        <button
          type="button"
          role="menuitem"
          tabIndex={-1}
          disabled={!canCreateTable}
          onClick={() => {
            if (!canCreateTable) return;
            setMenuOpen(false);
            setEditorMode("table");
          }}
        >
          Tableau
        </button>
        {!canCreateTable ? (
          <p className="editorial-visual-insertion__bridge-reason" role="note">
            Création de tableau indisponible pour ce passage : les artefacts
            éditoriaux ne sont pas prêts.
          </p>
        ) : null}
        <button
          type="button"
          role="menuitem"
          tabIndex={-1}
          disabled={!canCreateBarChart}
          onClick={() => {
            if (!canCreateBarChart) return;
            setMenuOpen(false);
            setEditorMode("chart");
          }}
        >
          Graphique
        </button>
        {!canCreateBarChart ? (
          <p className="editorial-visual-insertion__bridge-reason" role="note">
            Création de graphique à barres indisponible pour ce paragraphe :
            {block.allowed_operations.includes("create_bar_chart")
              ? " les artefacts éditoriaux ne sont pas prêts."
              : " l’opération n’est pas autorisée par l’instantané."}
          </p>
        ) : null}
        <button
          type="button"
          role="menuitem"
          tabIndex={-1}
          disabled={!canCreateFigure}
          onClick={() => {
            if (!canCreateFigure) return;
            setMenuOpen(false);
            setEditorMode("figure");
          }}
        >
          Image issue des sources
        </button>
        {!canCreateFigure ? (
          <p className="editorial-visual-insertion__bridge-reason" role="note">
            Images sources indisponibles pour ce paragraphe :
            {block.allowed_operations.includes("create_source_figure")
              ? " la préparation de l’insertion n’est pas disponible."
              : " l’opération n’est pas autorisée par l’instantané."}
          </p>
        ) : null}
      </div>
      <TableInsertionPanel
        subjectId={subjectId}
        snapshot={snapshot}
        block={block}
        canCreateTable={canCreateTable}
        open={editorMode === "table"}
        onClose={closePanel}
        onAskBridge={onAskBridge}
      />
      <BarChartInsertionPanel
        subjectId={subjectId}
        snapshot={snapshot}
        block={block}
        canCreateBarChart={canCreateBarChart}
        open={editorMode === "chart"}
        onClose={closePanel}
        onSwitchToTable={() => setEditorMode("table")}
      />
      {editorMode === "figure" ? (
        <FigureInsertionPanel
          subjectId={subjectId}
          snapshot={snapshot}
          block={block}
          canCreateFigure={canCreateFigure}
          onClose={closePanel}
          onRefresh={onRefresh ?? (() => undefined)}
        />
      ) : null}
    </div>
  );
}
