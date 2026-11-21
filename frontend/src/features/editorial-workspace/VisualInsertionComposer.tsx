import { useEffect, useRef, useState } from "react";

import type {
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { BarChartInsertionPanel } from "./BarChartInsertionPanel";
import { TableInsertionPanel } from "./TableInsertionPanel";

interface VisualInsertionComposerProps {
  subjectId: string;
  snapshot: EditorialWorkspaceSnapshot;
  block: EditorialWorkspaceBlock;
  readOnly: boolean;
  onAskBridge?: (instruction: string) => void;
}

function isParagraphAnchor(block: EditorialWorkspaceBlock): boolean {
  return block.kind === "paragraph" || block.kind === "lead_paragraph";
}

export function VisualInsertionComposer({
  subjectId,
  snapshot,
  block,
  readOnly,
  onAskBridge,
}: VisualInsertionComposerProps) {
  const addButtonRef = useRef<HTMLButtonElement>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [editorMode, setEditorMode] = useState<"table" | "chart" | null>(null);
  const canInsertAfter =
    !readOnly &&
    snapshot.capabilities.can_add_enrichment &&
    block.allowed_operations.includes("insert_after") &&
    isParagraphAnchor(block);
  const canCreateTable =
    canInsertAfter && block.allowed_operations.includes("create_table");
  const canCreateBarChart =
    canInsertAfter && block.allowed_operations.includes("create_bar_chart");

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
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
        aria-expanded={menuOpen}
        onClick={() => setMenuOpen((open) => !open)}
      >
        Ajouter après
      </button>
      {menuOpen ? (
        <div
          className="editorial-visual-insertion__menu"
          role="menu"
          aria-label="Ajouter après"
        >
          <button
            type="button"
            role="menuitem"
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
            <p
              className="editorial-visual-insertion__bridge-reason"
              role="note"
            >
              Création de tableau indisponible pour ce passage : les artefacts
              éditoriaux ne sont pas prêts.
            </p>
          ) : null}
          <button
            type="button"
            role="menuitem"
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
            <p
              className="editorial-visual-insertion__bridge-reason"
              role="note"
            >
              Création de graphique à barres indisponible pour ce paragraphe :
              {block.allowed_operations.includes("create_bar_chart")
                ? " les artefacts éditoriaux ne sont pas prêts."
                : " l’opération n’est pas autorisée par l’instantané."}
            </p>
          ) : null}
        </div>
      ) : null}
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
    </div>
  );
}
