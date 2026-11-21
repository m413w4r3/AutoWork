import { useMutation } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { ApiError } from "../../api/editions";
import {
  createManualEditorialBarChartProposal,
  createManualEditorialTableProposal,
  type EditorialEvidenceReference,
  type EditorialManualBarChartProposalInput,
  type EditorialWorkspaceBlock,
  type EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { EditorialProposalCard } from "./EditorialProposalCard";
import {
  ManualBarChartEditor,
  type BarChartPointDraft,
} from "./ManualBarChartEditor";
import { ManualTableEditor, type AdmittedEvidence } from "./ManualTableEditor";
import type { TableColumnDraft, TableRowDraft } from "./ManualTablePreview";

const DEFAULT_INSTRUCTION =
  "Créer un tableau à partir des informations de ce passage et de ses preuves associées.";
const BRIDGE_TABLE_INSTRUCTION =
  "Propose un tableau typé à partir de ce paragraphe et de ses preuves admises, sans ajouter de fait.";
const DEFAULT_BAR_CHART_INSTRUCTION =
  "Créer un graphique à barres à partir des valeurs chiffrées de ce passage et de ses preuves associées.";
const INSUFFICIENT_CHART_DATA_CODES = new Set([
  "bar_chart_insufficient_data",
  "bar_chart_value_not_evidenced",
]);

interface VisualInsertionComposerProps {
  subjectId: string;
  snapshot: EditorialWorkspaceSnapshot;
  block: EditorialWorkspaceBlock;
  readOnly: boolean;
  onAskBridge?: (instruction: string) => void;
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

function newChartPoint(): BarChartPointDraft {
  return { label: "", value: "", evidenceIdentities: [] };
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
  const instructionRef = useRef<HTMLTextAreaElement>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [editorMode, setEditorMode] = useState<"table" | "chart" | null>(null);
  const [instruction, setInstruction] = useState(DEFAULT_INSTRUCTION);
  const [title, setTitle] = useState("");
  const [caption, setCaption] = useState("");
  const [columns, setColumns] = useState<TableColumnDraft[]>([
    newColumn(),
    newColumn(),
  ]);
  const [rows, setRows] = useState<TableRowDraft[]>([newRow(2)]);
  const [chartInstruction, setChartInstruction] = useState(
    DEFAULT_BAR_CHART_INSTRUCTION,
  );
  const [chartTitle, setChartTitle] = useState("");
  const [chartLegend, setChartLegend] = useState("");
  const [chartCategoryAxisLabel, setChartCategoryAxisLabel] = useState("");
  const [chartValueAxisLabel, setChartValueAxisLabel] = useState("");
  const [chartUnit, setChartUnit] = useState("");
  const [chartPoints, setChartPoints] = useState<BarChartPointDraft[]>([
    newChartPoint(),
    newChartPoint(),
  ]);
  const [proposalId, setProposalId] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [chartProposalId, setChartProposalId] = useState<string | null>(null);
  const [chartRequestError, setChartRequestError] = useState<string | null>(
    null,
  );
  const [chartDataInsufficient, setChartDataInsufficient] = useState(false);
  const proposalRequestRef = useRef<{
    idempotencyKey: string;
    tableKey: string;
  } | null>(null);
  const chartProposalRequestRef = useRef<{
    idempotencyKey: string;
    chartKey: string;
  } | null>(null);
  const admitted = admittedEvidence(block);
  const admittedByIdentity = new Map(
    admitted.map((item) => [item.identity, item.reference]),
  );
  const canInsertAfter =
    !readOnly &&
    snapshot.capabilities.can_add_enrichment &&
    block.allowed_operations.includes("insert_after") &&
    isParagraphAnchor(block);
  const canCreateTable =
    canInsertAfter && block.allowed_operations.includes("create_table");
  const canCreateBarChart =
    canInsertAfter && block.allowed_operations.includes("create_bar_chart");
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
  const chartProposalMutation = useMutation({
    mutationFn: () => {
      const proposalRequest = chartProposalRequestRef.current ?? {
        idempotencyKey: crypto.randomUUID(),
        chartKey: crypto.randomUUID(),
      };
      chartProposalRequestRef.current = proposalRequest;
      const evidenceForPoints = chartPoints.map((point) =>
        point.evidenceIdentities.flatMap((identity) => {
          const reference = admittedByIdentity.get(identity);
          return reference ? [reference] : [];
        }),
      );
      const purposeEvidence = Array.from(
        new Map(
          evidenceForPoints.flatMap((references) =>
            references.map((reference) => [
              `${reference.source_document_id}:${reference.kind}:${reference.evidence_key}`,
              reference,
            ]),
          ),
        ).values(),
      );
      const input: EditorialManualBarChartProposalInput = {
        snapshot_id: snapshot.snapshot_id,
        anchor_block_id: block.block_id,
        chart: {
          key: `chart-${proposalRequest.chartKey}`,
          title: chartTitle.trim(),
          caption: null,
          category_axis_label: chartCategoryAxisLabel.trim(),
          value_axis_label: chartValueAxisLabel.trim(),
          unit: chartUnit.trim(),
          points: chartPoints.map((point, index) => ({
            label: point.label.trim(),
            series: chartLegend.trim(),
            value: point.value.trim(),
            evidence_refs: evidenceForPoints[index] ?? [],
          })),
          purpose: {
            question: chartInstruction.trim(),
            available_data:
              "Valeurs chiffrées du paragraphe et preuves admises sélectionnées.",
            comprehension_gain:
              "Comparer les valeurs quantitatives documentées.",
            scope: "Le paragraphe d’ancrage et les preuves sélectionnées.",
            evidence_refs: purposeEvidence,
            knowledge_limits:
              "Ne pas ajouter, extrapoler ou calculer de valeur au-delà des preuves sélectionnées.",
            placement_reason: "Après le paragraphe sélectionné.",
          },
        },
      };
      return createManualEditorialBarChartProposal(
        subjectId,
        block.block_id,
        proposalRequest.idempotencyKey,
        input,
      );
    },
    onSuccess: (proposal) => {
      setChartProposalId(proposal.id);
      setChartRequestError(null);
      setChartDataInsufficient(false);
    },
    onError: (error: Error) => {
      if (
        error instanceof ApiError &&
        error.status >= 400 &&
        error.status < 500
      ) {
        chartProposalRequestRef.current = null;
      }
      setChartRequestError(error.message);
      setChartDataInsufficient(
        error instanceof ApiError &&
          INSUFFICIENT_CHART_DATA_CODES.has(error.code),
      );
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
  const validChart =
    chartTitle.trim().length > 0 &&
    chartLegend.trim().length > 0 &&
    chartCategoryAxisLabel.trim().length > 0 &&
    chartValueAxisLabel.trim().length > 0 &&
    chartUnit.trim().length > 0 &&
    chartInstruction.trim().length > 0 &&
    chartPoints.length >= 2 &&
    chartPoints.length <= 24 &&
    new Set(chartPoints.map((point) => point.label.trim().toLocaleLowerCase()))
      .size === chartPoints.length &&
    chartPoints.every((point) => {
      const value = point.value.trim();
      return (
        point.label.trim().length > 0 &&
        value.length > 0 &&
        Number.isFinite(Number(value)) &&
        point.evidenceIdentities.some((identity) =>
          admittedByIdentity.has(identity),
        )
      );
    });

  useEffect(() => {
    if (editorMode !== null) instructionRef.current?.focus();
  }, [editorMode]);

  useEffect(() => {
    proposalRequestRef.current = null;
    chartProposalRequestRef.current = null;
  }, [block.block_id, snapshot.snapshot_id, subjectId]);

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

  const invalidateProposalRequest = () => {
    proposalRequestRef.current = null;
  };

  const invalidateChartDraft = () => {
    chartProposalRequestRef.current = null;
    setChartProposalId(null);
    setChartRequestError(null);
    setChartDataInsufficient(false);
  };

  const updateInstruction = (value: string) => {
    invalidateProposalRequest();
    setInstruction(value);
  };

  const updateTitle = (value: string) => {
    invalidateProposalRequest();
    setTitle(value);
  };

  const updateCaption = (value: string) => {
    invalidateProposalRequest();
    setCaption(value);
  };

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

  const updateColumnLabel = (columnIndex: number, label: string) => {
    invalidateProposalRequest();
    setColumns((current) =>
      current.map((item, index) =>
        index === columnIndex ? { ...item, label } : item,
      ),
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

  const updateChartInstruction = (value: string) => {
    invalidateChartDraft();
    setChartInstruction(value);
  };
  const updateChartTitle = (value: string) => {
    invalidateChartDraft();
    setChartTitle(value);
  };
  const updateChartLegend = (value: string) => {
    invalidateChartDraft();
    setChartLegend(value);
  };
  const updateChartCategoryAxisLabel = (value: string) => {
    invalidateChartDraft();
    setChartCategoryAxisLabel(value);
  };
  const updateChartValueAxisLabel = (value: string) => {
    invalidateChartDraft();
    setChartValueAxisLabel(value);
  };
  const updateChartUnit = (value: string) => {
    invalidateChartDraft();
    setChartUnit(value);
  };
  const updateChartPoint = (
    pointIndex: number,
    update: (point: BarChartPointDraft) => BarChartPointDraft,
  ) => {
    invalidateChartDraft();
    setChartPoints((current) =>
      current.map((point, index) =>
        index === pointIndex ? update(point) : point,
      ),
    );
  };
  const updateChartPointLabel = (pointIndex: number, value: string) => {
    updateChartPoint(pointIndex, (point) => ({ ...point, label: value }));
  };
  const updateChartPointValue = (pointIndex: number, value: string) => {
    updateChartPoint(pointIndex, (point) => ({ ...point, value }));
  };
  const toggleChartPointEvidence = (
    pointIndex: number,
    identity: string,
    checked: boolean,
  ) => {
    updateChartPoint(pointIndex, (point) => ({
      ...point,
      evidenceIdentities: checked
        ? [...new Set([...point.evidenceIdentities, identity])]
        : point.evidenceIdentities.filter((item) => item !== identity),
    }));
  };
  const addChartPoint = () => {
    if (chartPoints.length >= 24) return;
    invalidateChartDraft();
    setChartPoints((current) => [...current, newChartPoint()]);
  };
  const removeChartPoint = (pointIndex: number) => {
    if (chartPoints.length <= 2) return;
    invalidateChartDraft();
    setChartPoints((current) =>
      current.filter((_, index) => index !== pointIndex),
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
      {editorMode === "table" ? (
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
                setEditorMode(null);
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
          <ManualTableEditor
            instruction={instruction}
            instructionRef={instructionRef}
            title={title}
            caption={caption}
            columns={columns}
            rows={rows}
            admitted={admitted}
            onInstructionChange={updateInstruction}
            onTitleChange={updateTitle}
            onCaptionChange={updateCaption}
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
          ) : null}
          {!canCreateTable ? (
            <p
              className="editorial-visual-insertion__bridge-reason"
              role="note"
            >
              Création de tableau indisponible pour ce passage : les artefacts
              éditoriaux ne sont pas prêts.
            </p>
          ) : null}
          {requestError ? <p role="alert">{requestError}</p> : null}
          <button
            className="button editorial-visual-insertion__propose"
            type="button"
            disabled={
              !canCreateTable || !validTable || proposalMutation.isPending
            }
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
      ) : null}
      {editorMode === "chart" ? (
        <section
          className="editorial-visual-insertion__panel"
          aria-labelledby="editorial-bar-chart-composer-heading"
        >
          <header>
            <h3 id="editorial-bar-chart-composer-heading">
              Créer un graphique à barres
            </h3>
            <button
              className="editorial-visual-insertion__close"
              type="button"
              onClick={() => {
                setEditorMode(null);
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
          <ManualBarChartEditor
            instruction={chartInstruction}
            instructionRef={instructionRef}
            title={chartTitle}
            legend={chartLegend}
            categoryAxisLabel={chartCategoryAxisLabel}
            valueAxisLabel={chartValueAxisLabel}
            unit={chartUnit}
            points={chartPoints}
            admitted={admitted}
            onInstructionChange={updateChartInstruction}
            onTitleChange={updateChartTitle}
            onLegendChange={updateChartLegend}
            onCategoryAxisLabelChange={updateChartCategoryAxisLabel}
            onValueAxisLabelChange={updateChartValueAxisLabel}
            onUnitChange={updateChartUnit}
            onPointLabelChange={updateChartPointLabel}
            onPointValueChange={updateChartPointValue}
            onTogglePointEvidence={toggleChartPointEvidence}
            onAddPoint={addChartPoint}
            onRemovePoint={removeChartPoint}
          />
          {chartRequestError ? <p role="alert">{chartRequestError}</p> : null}
          {chartDataInsufficient ? (
            <button
              className="button button--secondary"
              type="button"
              onClick={() => setEditorMode("table")}
            >
              Proposer un tableau à la place
            </button>
          ) : null}
          <button
            className="button editorial-visual-insertion__propose"
            type="button"
            disabled={
              !canCreateBarChart ||
              !validChart ||
              chartProposalMutation.isPending ||
              chartProposalId !== null
            }
            onClick={() => {
              if (!canCreateBarChart) return;
              setChartProposalId(null);
              setChartRequestError(null);
              setChartDataInsufficient(false);
              chartProposalMutation.mutate();
            }}
          >
            {chartProposalMutation.isPending
              ? "Création de la proposition…"
              : chartProposalId
                ? "Proposition créée"
                : "Proposer"}
          </button>
          {chartProposalId ? (
            <div className="editorial-visual-insertion__proposal">
              <EditorialProposalCard
                subjectId={subjectId}
                occurrenceId={block.block_id}
                proposalId={chartProposalId}
                anchorText={block.text ?? undefined}
              />
            </div>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}
