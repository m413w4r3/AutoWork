import { useMutation } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { ApiError } from "../../api/editions";
import {
  createManualEditorialBarChartProposal,
  type EditorialManualBarChartProposalInput,
  type EditorialWorkspaceBlock,
  type EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { EditorialProposalCard } from "./EditorialProposalCard";
import { admittedEvidence } from "./insertionEvidence";
import {
  ManualBarChartEditor,
  type BarChartPointDraft,
} from "./ManualBarChartEditor";

const DEFAULT_INSTRUCTION =
  "Créer un graphique à barres à partir des valeurs chiffrées de ce passage et de ses preuves associées.";
const INSUFFICIENT_CHART_DATA_CODES = new Set([
  "bar_chart_insufficient_data",
  "bar_chart_value_not_evidenced",
]);

interface BarChartInsertionPanelProps {
  subjectId: string;
  snapshot: EditorialWorkspaceSnapshot;
  block: EditorialWorkspaceBlock;
  canCreateBarChart: boolean;
  open: boolean;
  onClose: () => void;
  onSwitchToTable: () => void;
}

function newChartPoint(): BarChartPointDraft {
  return { label: "", value: "", evidenceIdentities: [] };
}

export function BarChartInsertionPanel({
  subjectId,
  snapshot,
  block,
  canCreateBarChart,
  open,
  onClose,
  onSwitchToTable,
}: BarChartInsertionPanelProps) {
  const instructionRef = useRef<HTMLTextAreaElement>(null);
  const [instruction, setInstruction] = useState(DEFAULT_INSTRUCTION);
  const [title, setTitle] = useState("");
  const [legend, setLegend] = useState("");
  const [categoryAxisLabel, setCategoryAxisLabel] = useState("");
  const [valueAxisLabel, setValueAxisLabel] = useState("");
  const [unit, setUnit] = useState("");
  const [points, setPoints] = useState<BarChartPointDraft[]>([
    newChartPoint(),
    newChartPoint(),
  ]);
  const [proposalId, setProposalId] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [dataInsufficient, setDataInsufficient] = useState(false);
  const proposalRequestRef = useRef<{
    idempotencyKey: string;
    chartKey: string;
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

  const invalidateDraft = () => {
    proposalRequestRef.current = null;
    setProposalId(null);
    setRequestError(null);
    setDataInsufficient(false);
  };

  const proposalMutation = useMutation({
    mutationFn: () => {
      const proposalRequest = proposalRequestRef.current ?? {
        idempotencyKey: crypto.randomUUID(),
        chartKey: crypto.randomUUID(),
      };
      proposalRequestRef.current = proposalRequest;
      const evidenceForPoints = points.map((point) =>
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
          title: title.trim(),
          caption: null,
          category_axis_label: categoryAxisLabel.trim(),
          value_axis_label: valueAxisLabel.trim(),
          unit: unit.trim(),
          points: points.map((point, index) => ({
            label: point.label.trim(),
            series: legend.trim(),
            value: point.value.trim(),
            evidence_refs: evidenceForPoints[index] ?? [],
          })),
          purpose: {
            question: instruction.trim(),
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
      setProposalId(proposal.id);
      setRequestError(null);
      setDataInsufficient(false);
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
      setDataInsufficient(
        error instanceof ApiError &&
          INSUFFICIENT_CHART_DATA_CODES.has(error.code),
      );
    },
  });

  const validChart =
    title.trim().length > 0 &&
    legend.trim().length > 0 &&
    categoryAxisLabel.trim().length > 0 &&
    valueAxisLabel.trim().length > 0 &&
    unit.trim().length > 0 &&
    instruction.trim().length > 0 &&
    points.length >= 2 &&
    points.length <= 24 &&
    new Set(points.map((point) => point.label.trim().toLocaleLowerCase()))
      .size === points.length &&
    points.every((point) => {
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

  const updatePoint = (
    pointIndex: number,
    update: (point: BarChartPointDraft) => BarChartPointDraft,
  ) => {
    invalidateDraft();
    setPoints((current) =>
      current.map((point, index) =>
        index === pointIndex ? update(point) : point,
      ),
    );
  };

  const addPoint = () => {
    if (points.length >= 24) return;
    invalidateDraft();
    setPoints((current) => [...current, newChartPoint()]);
  };

  const removePoint = (pointIndex: number) => {
    if (points.length <= 2) return;
    invalidateDraft();
    setPoints((current) => current.filter((_, index) => index !== pointIndex));
  };

  if (!open) return null;

  return (
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
          onClick={onClose}
        >
          Fermer
        </button>
      </header>
      <p className="editorial-visual-insertion__manual-note">
        Cette proposition est créée manuellement, sans appel de modèle.
      </p>
      <ManualBarChartEditor
        instruction={instruction}
        instructionRef={instructionRef}
        title={title}
        legend={legend}
        categoryAxisLabel={categoryAxisLabel}
        valueAxisLabel={valueAxisLabel}
        unit={unit}
        points={points}
        admitted={admitted}
        onInstructionChange={(value) => {
          invalidateDraft();
          setInstruction(value);
        }}
        onTitleChange={(value) => {
          invalidateDraft();
          setTitle(value);
        }}
        onLegendChange={(value) => {
          invalidateDraft();
          setLegend(value);
        }}
        onCategoryAxisLabelChange={(value) => {
          invalidateDraft();
          setCategoryAxisLabel(value);
        }}
        onValueAxisLabelChange={(value) => {
          invalidateDraft();
          setValueAxisLabel(value);
        }}
        onUnitChange={(value) => {
          invalidateDraft();
          setUnit(value);
        }}
        onPointLabelChange={(pointIndex, value) =>
          updatePoint(pointIndex, (point) => ({ ...point, label: value }))
        }
        onPointValueChange={(pointIndex, value) =>
          updatePoint(pointIndex, (point) => ({ ...point, value }))
        }
        onTogglePointEvidence={(pointIndex, identity, checked) =>
          updatePoint(pointIndex, (point) => ({
            ...point,
            evidenceIdentities: checked
              ? [...new Set([...point.evidenceIdentities, identity])]
              : point.evidenceIdentities.filter((item) => item !== identity),
          }))
        }
        onAddPoint={addPoint}
        onRemovePoint={removePoint}
      />
      {requestError ? <p role="alert">{requestError}</p> : null}
      {dataInsufficient ? (
        <button
          className="button button--secondary"
          type="button"
          onClick={onSwitchToTable}
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
          proposalMutation.isPending ||
          proposalId !== null
        }
        onClick={() => {
          if (!canCreateBarChart) return;
          setProposalId(null);
          setRequestError(null);
          setDataInsufficient(false);
          proposalMutation.mutate();
        }}
      >
        {proposalMutation.isPending
          ? "Création de la proposition…"
          : proposalId
            ? "Proposition créée"
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
