import { useMutation } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { ApiError } from "../../api/editions";
import {
  createManualEditorialTableProposal,
  type EditorialWorkspaceBlock,
  type EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { EditorialProposalCard } from "./EditorialProposalCard";
import { admittedEvidence } from "./insertionEvidence";
import { ManualTableEditor } from "./ManualTableEditor";
import type { TableColumnDraft, TableRowDraft } from "./ManualTablePreview";

const DEFAULT_INSTRUCTION =
  "Créer un tableau à partir des informations de ce passage et de ses preuves associées.";
const BRIDGE_TABLE_INSTRUCTION =
  "Propose un tableau typé à partir de ce paragraphe et de ses preuves admises, sans ajouter de fait.";

interface TableInsertionPanelProps {
  subjectId: string;
  snapshot: EditorialWorkspaceSnapshot;
  block: EditorialWorkspaceBlock;
  canCreateTable: boolean;
  open: boolean;
  onClose: () => void;
  onAskBridge?: (instruction: string) => void;
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

export function TableInsertionPanel({
  subjectId,
  snapshot,
  block,
  canCreateTable,
  open,
  onClose,
  onAskBridge,
}: TableInsertionPanelProps) {
  const instructionRef = useRef<HTMLTextAreaElement>(null);
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
  const proposalRequestRef = useRef<{
    idempotencyKey: string;
    tableKey: string;
  } | null>(null);
  const admitted = admittedEvidence(block);
  const admittedByIdentity = new Map(
    admitted.map((item) => [item.identity, item.reference]),
  );

  useEffect(() => {
    if (open) instructionRef.current?.focus();
  }, [open]);

  useEffect(() => {
    proposalRequestRef.current = null;
  }, [block.block_id, snapshot.snapshot_id, subjectId]);

  const invalidateProposalRequest = () => {
    proposalRequestRef.current = null;
  };

  const proposalMutation = useMutation({
    mutationFn: () => {
      const proposalRequest = proposalRequestRef.current ?? {
        idempotencyKey: crypto.randomUUID(),
        tableKey: crypto.randomUUID(),
      };
      proposalRequestRef.current = proposalRequest;
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
        proposalRequest.idempotencyKey,
        {
          snapshot_id: snapshot.snapshot_id,
          anchor_block_id: block.block_id,
          table: {
            key: `table-${proposalRequest.tableKey}`,
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
      proposalRequestRef.current = null;
      setProposalId(proposal.id);
      setRequestError(null);
    },
    onError: (error: Error) => {
      if (
        error instanceof ApiError &&
        error.status >= 400 &&
        error.status < 500
      ) {
        proposalRequestRef.current = null;
      }
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

  const addColumn = () => {
    if (columns.length >= 64) return;
    invalidateProposalRequest();
    setColumns((current) => [...current, newColumn()]);
    setRows((current) =>
      current.map((row) => ({ ...row, cells: [...row.cells, ""] })),
    );
  };

  const removeColumn = (columnIndex: number) => {
    if (columns.length <= 2) return;
    invalidateProposalRequest();
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

  const updateColumnLabel = (columnIndex: number, label: string) => {
    invalidateProposalRequest();
    setColumns((current) =>
      current.map((column, index) =>
        index === columnIndex ? { ...column, label } : column,
      ),
    );
  };

  const updateCell = (rowIndex: number, columnIndex: number, value: string) => {
    invalidateProposalRequest();
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
    invalidateProposalRequest();
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

  const addRow = () => {
    if (rows.length >= 1000) return;
    invalidateProposalRequest();
    setRows((current) => [...current, newRow(columns.length)]);
  };

  const removeRow = (rowIndex: number) => {
    if (rows.length <= 1) return;
    invalidateProposalRequest();
    setRows((current) => current.filter((_, index) => index !== rowIndex));
  };

  if (!open) return null;

  return (
    <section
      className="editorial-visual-insertion__panel"
      aria-labelledby="editorial-table-composer-heading"
    >
      <header>
        <h3 id="editorial-table-composer-heading">Créer un tableau</h3>
        <button
          className="editorial-visual-insertion__close"
          type="button"
          onClick={onClose}
        >
          Fermer
        </button>
      </header>
      <p className="editorial-visual-insertion__manual-note">
        Cette proposition est créée manuellement, sans appel de modèle.
      </p>
      <ManualTableEditor
        instruction={instruction}
        instructionRef={instructionRef}
        title={title}
        caption={caption}
        columns={columns}
        rows={rows}
        admitted={admitted}
        onInstructionChange={(value) => {
          invalidateProposalRequest();
          setInstruction(value);
        }}
        onTitleChange={(value) => {
          invalidateProposalRequest();
          setTitle(value);
        }}
        onCaptionChange={(value) => {
          invalidateProposalRequest();
          setCaption(value);
        }}
        onColumnLabelChange={updateColumnLabel}
        onAddColumn={addColumn}
        onRemoveColumn={removeColumn}
        onCellChange={updateCell}
        onToggleRowEvidence={toggleRowEvidence}
        onAddRow={addRow}
        onRemoveRow={removeRow}
      />
      {canCreateTable ? (
        <button
          className="button button--secondary editorial-visual-insertion__bridge"
          type="button"
          onClick={() => onAskBridge?.(BRIDGE_TABLE_INSTRUCTION)}
        >
          Demander au Bridge
        </button>
      ) : (
        <p className="editorial-visual-insertion__bridge-reason" role="note">
          Création de tableau indisponible pour ce passage : les artefacts
          éditoriaux ne sont pas prêts.
        </p>
      )}
      {requestError ? <p role="alert">{requestError}</p> : null}
      <button
        className="button editorial-visual-insertion__propose"
        type="button"
        disabled={!canCreateTable || !validTable || proposalMutation.isPending}
        onClick={() => {
          if (!canCreateTable) return;
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
  );
}
