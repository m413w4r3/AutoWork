import type { Ref } from "react";

import type { EditorialEvidenceReference } from "../../api/editorialWorkspace";
import {
  ManualTablePreview,
  type TableColumnDraft,
  type TableRowDraft,
} from "./ManualTablePreview";

export interface AdmittedEvidence {
  reference: EditorialEvidenceReference;
  identity: string;
  label: string;
  passage: string | null;
}

interface ManualTableEditorProps {
  instruction: string;
  instructionRef: Ref<HTMLTextAreaElement>;
  title: string;
  caption: string;
  columns: TableColumnDraft[];
  rows: TableRowDraft[];
  admitted: AdmittedEvidence[];
  onInstructionChange: (value: string) => void;
  onTitleChange: (value: string) => void;
  onCaptionChange: (value: string) => void;
  onColumnLabelChange: (index: number, value: string) => void;
  onAddColumn: () => void;
  onRemoveColumn: (index: number) => void;
  onCellChange: (rowIndex: number, columnIndex: number, value: string) => void;
  onToggleRowEvidence: (
    rowIndex: number,
    identity: string,
    checked: boolean,
  ) => void;
  onAddRow: () => void;
  onRemoveRow: (index: number) => void;
}

export function ManualTableEditor({
  instruction,
  instructionRef,
  title,
  caption,
  columns,
  rows,
  admitted,
  onInstructionChange,
  onTitleChange,
  onCaptionChange,
  onColumnLabelChange,
  onAddColumn,
  onRemoveColumn,
  onCellChange,
  onToggleRowEvidence,
  onAddRow,
  onRemoveRow,
}: ManualTableEditorProps) {
  return (
    <>
      <label className="editorial-visual-insertion__field">
        <span>Consigne</span>
        <textarea
          ref={instructionRef}
          value={instruction}
          onChange={(event) => onInstructionChange(event.currentTarget.value)}
          rows={2}
        />
      </label>
      <label className="editorial-visual-insertion__field">
        <span>Titre du tableau</span>
        <input
          value={title}
          onChange={(event) => onTitleChange(event.currentTarget.value)}
          maxLength={1000}
        />
      </label>
      <label className="editorial-visual-insertion__field">
        <span>Légende</span>
        <input
          value={caption}
          onChange={(event) => onCaptionChange(event.currentTarget.value)}
          maxLength={2000}
        />
      </label>
      <fieldset className="editorial-visual-insertion__columns">
        <legend>Intitulés de colonnes</legend>
        {columns.map((column, columnIndex) => (
          <div className="editorial-visual-insertion__column" key={columnIndex}>
            <label>
              <span>Colonne {columnIndex + 1}</span>
              <input
                aria-label={`Intitulé de colonne ${columnIndex + 1}`}
                value={column.label}
                maxLength={500}
                onChange={(event) =>
                  onColumnLabelChange(columnIndex, event.currentTarget.value)
                }
              />
            </label>
            <button
              className="button button--secondary"
              type="button"
              disabled={columns.length <= 2}
              aria-label={`Supprimer la colonne ${columnIndex + 1}`}
              onClick={() => onRemoveColumn(columnIndex)}
            >
              Supprimer
            </button>
          </div>
        ))}
        <button
          className="button button--secondary"
          type="button"
          disabled={columns.length >= 64}
          onClick={onAddColumn}
        >
          Ajouter une colonne
        </button>
      </fieldset>
      <fieldset className="editorial-visual-insertion__rows">
        <legend>Lignes et preuves associées</legend>
        {rows.map((row, rowIndex) => (
          <section
            className="editorial-visual-insertion__row"
            key={rowIndex}
            aria-labelledby={`editorial-table-row-${rowIndex + 1}`}
          >
            <header>
              <h4 id={`editorial-table-row-${rowIndex + 1}`}>
                Ligne {rowIndex + 1}
              </h4>
              <button
                className="button button--secondary"
                type="button"
                disabled={rows.length <= 1}
                aria-label={`Supprimer la ligne ${rowIndex + 1}`}
                onClick={() => onRemoveRow(rowIndex)}
              >
                Supprimer la ligne
              </button>
            </header>
            <div className="editorial-visual-insertion__cells">
              {columns.map((_, columnIndex) => (
                <label key={columnIndex}>
                  <span>Colonne {columnIndex + 1}</span>
                  <input
                    aria-label={`Cellule ligne ${rowIndex + 1}, colonne ${columnIndex + 1}`}
                    value={row.cells[columnIndex] ?? ""}
                    maxLength={2000}
                    onChange={(event) =>
                      onCellChange(
                        rowIndex,
                        columnIndex,
                        event.currentTarget.value,
                      )
                    }
                  />
                </label>
              ))}
            </div>
            <fieldset
              className="editorial-visual-insertion__evidence"
              aria-label={`Preuves ligne ${rowIndex + 1}`}
            >
              <legend>Preuves admises pour cette ligne</legend>
              {admitted.length > 0 ? (
                admitted.map((item) => (
                  <label key={item.identity}>
                    <input
                      type="checkbox"
                      checked={row.evidenceIdentities.includes(item.identity)}
                      onChange={(event) =>
                        onToggleRowEvidence(
                          rowIndex,
                          item.identity,
                          event.currentTarget.checked,
                        )
                      }
                    />
                    <span>
                      <strong>{item.label}</strong>
                      {item.passage ? <small>{item.passage}</small> : null}
                    </span>
                  </label>
                ))
              ) : (
                <p>Aucune preuve admise n’est disponible pour ce paragraphe.</p>
              )}
            </fieldset>
          </section>
        ))}
        <button
          className="button button--secondary"
          type="button"
          disabled={rows.length >= 1000}
          onClick={onAddRow}
        >
          Ajouter une ligne
        </button>
      </fieldset>
      <ManualTablePreview
        title={title}
        caption={caption}
        columns={columns}
        rows={rows}
      />
    </>
  );
}
