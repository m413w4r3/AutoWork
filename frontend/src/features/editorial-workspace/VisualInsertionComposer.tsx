import { useMutation } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import {
  createManualEditorialTableProposal,
  type EditorialEvidenceReference,
  type EditorialWorkspaceBlock,
  type EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { EditorialProposalCard } from "./EditorialProposalCard";

const DEFAULT_INSTRUCTION =
  "Créer un tableau à partir des informations de ce passage et de ses preuves associées.";
const BRIDGE_TABLE_INSTRUCTION =
  "Propose un tableau typé à partir de ce paragraphe et de ses preuves admises, sans ajouter de fait.";

interface VisualInsertionComposerProps {
  subjectId: string;
  snapshot: EditorialWorkspaceSnapshot;
  block: EditorialWorkspaceBlock;
  readOnly: boolean;
  onAskBridge?: (instruction: string) => void;
}

interface AdmittedEvidence {
  reference: EditorialEvidenceReference;
  identity: string;
  label: string;
  passage: string | null;
}

interface TableColumnDraft {
  label: string;
}

interface TableRowDraft {
  cells: string[];
  evidenceIdentities: string[];
}

const evidenceKinds = new Set<EditorialEvidenceReference["kind"]>([
  "fact",
  "event",
  "source_metadata",
  "indicator",
  "rule",
  "uncertainty",
]);

function admittedEvidence(block: EditorialWorkspaceBlock): AdmittedEvidence[] {
  const references: AdmittedEvidence[] = [];
  const seen = new Set<string>();
  for (const value of block.evidence_refs) {
    if (typeof value !== "object" || value === null) continue;
    const sourceDocumentId = value.source_document_id;
    const kind = value.kind;
    const evidenceKey = value.evidence_key;
    if (
      typeof sourceDocumentId !== "string" ||
      typeof kind !== "string" ||
      !evidenceKinds.has(kind as EditorialEvidenceReference["kind"]) ||
      typeof evidenceKey !== "string" ||
      !/^[0-9a-f]{64}$/.test(evidenceKey)
    ) {
      continue;
    }
    const reference: EditorialEvidenceReference = {
      source_document_id: sourceDocumentId,
      kind: kind as EditorialEvidenceReference["kind"],
      evidence_key: evidenceKey,
    };
    const identity = `${sourceDocumentId}:${kind}:${evidenceKey}`;
    if (seen.has(identity)) continue;
    seen.add(identity);
    const title =
      textValue(value.source_title) ??
      textValue(value.source_name) ??
      textValue(value.title) ??
      textValue(value.label) ??
      `Source ${sourceDocumentId}`;
    references.push({
      reference,
      identity,
      label: `${title} · ${kind}`,
      passage:
        textValue(value.passage) ??
        textValue(value.excerpt) ??
        textValue(value.quote),
    });
  }
  return references;
}

function textValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function newColumn(): TableColumnDraft {
  return { label: "" };
}

function newRow(columnCount: number): TableRowDraft {
  return {
    cells: Array.from({ length: columnCount }, () => ""),
    evidenceIdentities: [],
  };
}

function isTableBlock(block: EditorialWorkspaceBlock): boolean {
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
  const instructionRef = useRef<HTMLTextAreaElement>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);
  const [instruction, setInstruction] = useState(DEFAULT_INSTRUCTION);
  const [title, setTitle] = useState("");
  const [caption, setCaption] = useState("");
  const [columns, setColumns] = useState<TableColumnDraft[]>([
    newColumn(),
    newColumn(),
  ]);
  const [rows, setRows] = useState<TableRowDraft[]>([newRow(2)]);
  const [proposalId, setProposalId] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const admitted = admittedEvidence(block);
  const admittedByIdentity = new Map(
    admitted.map((item) => [item.identity, item.reference]),
  );
  const canInsertAfter =
    !readOnly &&
    snapshot.capabilities.can_add_enrichment &&
    block.allowed_operations.includes("insert_after") &&
    isTableBlock(block);
  const canCreateTable =
    canInsertAfter && block.allowed_operations.includes("create_table");
  const proposalMutation = useMutation({
    mutationFn: () => {
      const evidenceForRows = rows.map((row) =>
        row.evidenceIdentities.flatMap((identity) => {
          const reference = admittedByIdentity.get(identity);
          return reference ? [reference] : [];
        }),
      );
      const purposeEvidence = Array.from(
        new Map(
          evidenceForRows.flatMap((references) =>
            references.map((reference) => [
              `${reference.source_document_id}:${reference.kind}:${reference.evidence_key}`,
              reference,
            ]),
          ),
        ).values(),
      );
      return createManualEditorialTableProposal(
        subjectId,
        block.block_id,
        crypto.randomUUID(),
        {
          snapshot_id: snapshot.snapshot_id,
          anchor_block_id: block.block_id,
          table: {
            key: `table-${crypto.randomUUID()}`,
            kind: "custom",
            title: title.trim(),
            caption: caption.trim() || null,
            columns: columns.map((column, index) => ({
              key: `column-${index + 1}`,
              label: column.label.trim(),
            })),
            rows: rows.map((row, index) => ({
              cells: row.cells.map((cell) => cell.trim()),
              evidence_refs: evidenceForRows[index] ?? [],
            })),
            purpose: {
              question: instruction.trim(),
              available_data:
                "Informations du paragraphe et preuves admises sélectionnées.",
              comprehension_gain:
                "Présenter ces informations sous forme structurée.",
              scope: "Le paragraphe d’ancrage et les preuves sélectionnées.",
              evidence_refs: purposeEvidence,
              knowledge_limits:
                "Ne pas ajouter de fait au-delà des cellules et preuves sélectionnées.",
              placement_reason: "Après le paragraphe sélectionné.",
            },
          },
        },
      );
    },
    onSuccess: (proposal) => {
      setProposalId(proposal.id);
      setRequestError(null);
    },
    onError: (error: Error) => {
      setRequestError(error.message);
    },
  });
  const validTable =
    title.trim().length > 0 &&
    instruction.trim().length > 0 &&
    columns.length >= 2 &&
    columns.every((column) => column.label.trim().length > 0) &&
    rows.length > 0 &&
    rows.every(
      (row) =>
        row.cells.length === columns.length &&
        row.cells.every((cell) => cell.trim().length > 0) &&
        row.evidenceIdentities.some((identity) =>
          admittedByIdentity.has(identity),
        ),
    );

  useEffect(() => {
    if (editorOpen) instructionRef.current?.focus();
  }, [editorOpen]);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (!editorOpen && !menuOpen) return;
      event.preventDefault();
      event.stopPropagation();
      setMenuOpen(false);
      setEditorOpen(false);
      window.requestAnimationFrame(() => addButtonRef.current?.focus());
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [editorOpen, menuOpen]);

  if (!canInsertAfter) return null;

  const addColumn = () => {
    if (columns.length >= 64) return;
    setColumns((current) => [...current, newColumn()]);
    setRows((current) =>
      current.map((row) => ({ ...row, cells: [...row.cells, ""] })),
    );
  };

  const removeColumn = (columnIndex: number) => {
    if (columns.length <= 2) return;
    setColumns((current) =>
      current.filter((_, index) => index !== columnIndex),
    );
    setRows((current) =>
      current.map((row) => ({
        ...row,
        cells: row.cells.filter((_, index) => index !== columnIndex),
      })),
    );
  };

  const updateCell = (rowIndex: number, columnIndex: number, value: string) => {
    setRows((current) =>
      current.map((row, index) =>
        index === rowIndex
          ? {
              ...row,
              cells: row.cells.map((cell, cellIndex) =>
                cellIndex === columnIndex ? value : cell,
              ),
            }
          : row,
      ),
    );
  };

  const toggleRowEvidence = (
    rowIndex: number,
    identity: string,
    checked: boolean,
  ) => {
    setRows((current) =>
      current.map((row, index) => {
        if (index !== rowIndex) return row;
        const evidenceIdentities = checked
          ? [...new Set([...row.evidenceIdentities, identity])]
          : row.evidenceIdentities.filter((value) => value !== identity);
        return { ...row, evidenceIdentities };
      }),
    );
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
            onClick={() => {
              setMenuOpen(false);
              setEditorOpen(true);
            }}
          >
            Tableau
          </button>
        </div>
      ) : null}
      {editorOpen ? (
        <section
          className="editorial-visual-insertion__panel"
          aria-labelledby="editorial-table-composer-heading"
        >
          <header>
            <h3 id="editorial-table-composer-heading">Créer un tableau</h3>
            <button
              className="editorial-visual-insertion__close"
              type="button"
              onClick={() => {
                setEditorOpen(false);
                window.requestAnimationFrame(() =>
                  addButtonRef.current?.focus(),
                );
              }}
            >
              Fermer
            </button>
          </header>
          <p className="editorial-visual-insertion__manual-note">
            Cette proposition est créée manuellement, sans appel de modèle.
          </p>
          <label className="editorial-visual-insertion__field">
            <span>Consigne</span>
            <textarea
              ref={instructionRef}
              value={instruction}
              onChange={(event) => setInstruction(event.currentTarget.value)}
              rows={2}
            />
          </label>
          <label className="editorial-visual-insertion__field">
            <span>Titre du tableau</span>
            <input
              value={title}
              onChange={(event) => setTitle(event.currentTarget.value)}
              maxLength={1000}
            />
          </label>
          <label className="editorial-visual-insertion__field">
            <span>Légende</span>
            <input
              value={caption}
              onChange={(event) => setCaption(event.currentTarget.value)}
              maxLength={2000}
            />
          </label>
          <fieldset className="editorial-visual-insertion__columns">
            <legend>Intitulés de colonnes</legend>
            {columns.map((column, columnIndex) => (
              <div
                className="editorial-visual-insertion__column"
                key={columnIndex}
              >
                <label>
                  <span>Colonne {columnIndex + 1}</span>
                  <input
                    aria-label={`Intitulé de colonne ${columnIndex + 1}`}
                    value={column.label}
                    maxLength={500}
                    onChange={(event) => {
                      const label = event.currentTarget.value;
                      setColumns((current) =>
                        current.map((item, index) =>
                          index === columnIndex ? { ...item, label } : item,
                        ),
                      );
                    }}
                  />
                </label>
                <button
                  className="button button--secondary"
                  type="button"
                  disabled={columns.length <= 2}
                  aria-label={`Supprimer la colonne ${columnIndex + 1}`}
                  onClick={() => removeColumn(columnIndex)}
                >
                  Supprimer
                </button>
              </div>
            ))}
            <button
              className="button button--secondary"
              type="button"
              disabled={columns.length >= 64}
              onClick={addColumn}
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
                    onClick={() =>
                      setRows((current) =>
                        current.filter((_, index) => index !== rowIndex),
                      )
                    }
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
                          updateCell(
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
                          checked={row.evidenceIdentities.includes(
                            item.identity,
                          )}
                          onChange={(event) =>
                            toggleRowEvidence(
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
                    <p>
                      Aucune preuve admise n’est disponible pour ce paragraphe.
                    </p>
                  )}
                </fieldset>
              </section>
            ))}
            <button
              className="button button--secondary"
              type="button"
              disabled={rows.length >= 1000}
              onClick={() =>
                setRows((current) => [...current, newRow(columns.length)])
              }
            >
              Ajouter une ligne
            </button>
          </fieldset>
          <section
            className="editorial-visual-insertion__preview"
            aria-labelledby="editorial-table-preview-heading"
          >
            <h4 id="editorial-table-preview-heading">Aperçu du tableau</h4>
            <p className="editorial-visual-insertion__table-title">
              {title || "Titre du tableau"}
            </p>
            <div className="editorial-visual-insertion__table-scroll">
              <table aria-label={title || "Aperçu du tableau"}>
                <caption>{caption || title || "Tableau proposé"}</caption>
                <thead>
                  <tr>
                    {columns.map((column, index) => (
                      <th key={index} scope="col">
                        {column.label || `Colonne ${index + 1}`}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, rowIndex) => (
                    <tr key={rowIndex}>
                      {columns.map((_, columnIndex) => (
                        <td key={columnIndex}>
                          {row.cells[columnIndex] || "—"}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
          {canCreateTable ? (
            <button
              className="button button--secondary editorial-visual-insertion__bridge"
              type="button"
              onClick={() => onAskBridge?.(BRIDGE_TABLE_INSTRUCTION)}
            >
              Demander au Bridge
            </button>
          ) : (
            <p
              className="editorial-visual-insertion__bridge-reason"
              role="note"
            >
              Le Bridge ne peut pas proposer un tableau pour ce paragraphe :
              l’opération « create_table » n’est pas autorisée dans
              l’instantané.
            </p>
          )}
          {requestError ? <p role="alert">{requestError}</p> : null}
          <button
            className="button editorial-visual-insertion__propose"
            type="button"
            disabled={!validTable || proposalMutation.isPending}
            onClick={() => {
              setProposalId(null);
              setRequestError(null);
              proposalMutation.mutate();
            }}
          >
            {proposalMutation.isPending
              ? "Création de la proposition…"
              : "Proposer"}
          </button>
          {proposalId ? (
            <div className="editorial-visual-insertion__proposal">
              <EditorialProposalCard
                subjectId={subjectId}
                occurrenceId={block.block_id}
                proposalId={proposalId}
                anchorText={block.text ?? undefined}
              />
            </div>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}
