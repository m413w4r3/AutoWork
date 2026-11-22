import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "../../api/editions";
import {
  acceptEditorialProposal,
  getEditorialProposal,
  isVisualRevisionProposalPatch,
  rejectEditorialProposal,
  editorialFigureThumbnailUrl,
  type EditorialManualSourceFigurePatchReference,
  type EditorialProposalPatch,
  type EditorialProposalRead,
  type EditorialProposalRefusalCode,
  type EditorialProposalState,
  type EditorialTableProposal,
} from "../../api/editorialWorkspace";
import { editorialWorkspaceQueryKeys } from "./editorialWorkspaceQueries";
import { EditorialDiffView } from "./EditorialDiffView";
import { ManualBarChartPreview } from "./ManualBarChartPreview";
import { VisualProposalDiff } from "./VisualProposalDiff";
import {
  visualElementsSharingAnchor,
  visualPatchTarget,
} from "./visualElementOperations";
import { useVisualWorkspaceContext } from "./visualWorkspaceContext";

interface EditorialProposalCardProps {
  subjectId: string;
  occurrenceId: string;
  proposalId: string;
  referenceOrdinal?: number;
  anchorText?: string;
  onRequestVariant?: () => void;
  onStateChange?: (proposalId: string, state: EditorialProposalState) => void;
}

const stateLabels: Record<EditorialProposalState, string> = {
  preparing: "Préparation en cours",
  proposed: "Proposition à consulter",
  refused: "Refus Bridge",
  needs_review: "Revue nécessaire",
  accepted: "Acceptée",
  rejected: "Rejetée",
  stale: "Périmée",
};

const refusalLabels: Record<EditorialProposalRefusalCode, string> = {
  revision_not_applicable:
    "Les données disponibles ne permettent pas de proposer cette révision.",
};

function refusalLabel(code: unknown): string {
  if (code === "revision_not_applicable") {
    return refusalLabels[code];
  }
  return "Le Bridge n’a pas produit de proposition exploitable.";
}

function focusDocumentBlock(occurrenceId: string) {
  const block = Array.from(
    document.querySelectorAll<HTMLElement>(
      ".editorial-document__block[data-block-id]",
    ),
  ).find((candidate) => candidate.dataset.blockId === occurrenceId);
  block?.focus({ preventScroll: true });
}

function proposalWarnings(diagnostics: Record<string, unknown>): string[] {
  const warnings = diagnostics.warnings;
  return Array.isArray(warnings)
    ? warnings.filter(
        (warning): warning is string => typeof warning === "string",
      )
    : [];
}

function tableFromPatch(
  patch: EditorialProposalPatch | null,
): EditorialTableProposal | null {
  if (patch?.operation !== "create_table") return null;
  const tables = patch.enrichment_patch?.tables;
  return tables?.length === 1 ? (tables[0] ?? null) : null;
}

function sourceFiguresFromPatch(
  patch: EditorialProposalPatch | null,
): EditorialManualSourceFigurePatchReference[] | null {
  if (patch?.operation !== "create_source_figure") return null;
  const figures = patch.enrichment_patch?.manual_source_figures;
  if (!figures || figures.length === 0) return null;
  return [...figures].sort((left, right) => left.order - right.order);
}

interface BarChartProposalPoint {
  label: string;
  series: string;
  value: string;
  evidence_handles: string[];
}

interface BarChartProposal {
  title: string;
  caption: string | null;
  category_axis_label: string;
  value_axis_label: string;
  unit: string;
  points: BarChartProposalPoint[];
  anchor_block_id: string | null;
  purpose_question: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringArray(value: unknown): string[] | null {
  return Array.isArray(value) && value.every((item) => typeof item === "string")
    ? value
    : null;
}

function barChartFromPatch(
  patch: EditorialProposalPatch | null,
): BarChartProposal | null {
  if (patch?.operation !== "create_bar_chart") return null;
  const rawChart = patch.enrichment_patch?.charts?.[0];
  if (!isRecord(rawChart) || rawChart.kind !== "bar") return null;
  const rawPoints = rawChart.points;
  const placement = rawChart.placement;
  const purpose = rawChart.purpose;
  if (
    typeof rawChart.title !== "string" ||
    (rawChart.caption !== null && typeof rawChart.caption !== "string") ||
    typeof rawChart.category_axis_label !== "string" ||
    typeof rawChart.value_axis_label !== "string" ||
    typeof rawChart.unit !== "string" ||
    !Array.isArray(rawPoints) ||
    !isRecord(placement) ||
    !isRecord(purpose)
  ) {
    return null;
  }
  const points: BarChartProposalPoint[] = [];
  for (const rawPoint of rawPoints) {
    if (
      !isRecord(rawPoint) ||
      typeof rawPoint.label !== "string" ||
      typeof rawPoint.series !== "string" ||
      typeof rawPoint.value !== "string"
    ) {
      return null;
    }
    const evidenceHandles = stringArray(rawPoint.evidence_handles);
    if (!evidenceHandles) return null;
    points.push({
      label: rawPoint.label,
      series: rawPoint.series,
      value: rawPoint.value,
      evidence_handles: evidenceHandles,
    });
  }
  return {
    title: rawChart.title,
    caption: typeof rawChart.caption === "string" ? rawChart.caption : null,
    category_axis_label: rawChart.category_axis_label,
    value_axis_label: rawChart.value_axis_label,
    unit: rawChart.unit,
    points,
    anchor_block_id:
      typeof placement.anchor_block_id === "string"
        ? placement.anchor_block_id
        : null,
    purpose_question:
      typeof purpose.question === "string" ? purpose.question : "",
  };
}

function BarChartProposalComparison({
  chart,
  anchorText,
  evidenceRefs,
}: {
  chart: BarChartProposal;
  anchorText: string | undefined;
  evidenceRefs: EditorialProposalPatch["evidence_refs"];
}) {
  const legend = Array.from(new Set(chart.points.map((point) => point.series)));
  return (
    <section
      className="editorial-proposal-card__chart-comparison"
      aria-label="Comparaison de l’insertion de graphique"
    >
      <div className="editorial-proposal-card__table-anchor">
        <h5>Paragraphe d’ancrage</h5>
        <p>{anchorText || "Paragraphe sélectionné"}</p>
      </div>
      <p className="editorial-proposal-card__table-placement">
        Après ce paragraphe
      </p>
      <div className="editorial-proposal-card__chart-proposed">
        <h5>Insertion de graphique</h5>
        <p className="editorial-proposal-card__table-purpose">
          <strong>Consigne · </strong>
          {chart.purpose_question}
        </p>
        <ManualBarChartPreview
          title={chart.title}
          caption={chart.caption}
          legend={legend}
          categoryAxisLabel={chart.category_axis_label}
          valueAxisLabel={chart.value_axis_label}
          unit={chart.unit}
          points={chart.points.map(({ label, value }) => ({ label, value }))}
        />
        <details className="editorial-proposal-card__table-evidence">
          <summary>Preuves associées</summary>
          {evidenceRefs && evidenceRefs.length > 0 ? (
            <ul>
              {evidenceRefs.map((reference, index) => (
                <li
                  key={`${reference.source_document_id}-${reference.evidence_key}-${index}`}
                >
                  <span>
                    Source {reference.source_document_id} · {reference.kind}
                  </span>
                  <code>{reference.evidence_key}</code>
                </li>
              ))}
            </ul>
          ) : (
            <p>
              Aucune référence de preuve n’est disponible pour cette
              proposition.
            </p>
          )}
        </details>
      </div>
    </section>
  );
}

function TableProposalComparison({
  table,
  anchorText,
  evidenceRefs,
}: {
  table: EditorialTableProposal;
  anchorText: string | undefined;
  evidenceRefs: EditorialProposalPatch["evidence_refs"];
}) {
  return (
    <section
      className="editorial-proposal-card__table-comparison"
      aria-label="Comparaison de l’insertion de tableau"
    >
      <div className="editorial-proposal-card__table-anchor">
        <h5>Paragraphe d’ancrage</h5>
        <p>{anchorText || "Paragraphe sélectionné"}</p>
      </div>
      <p className="editorial-proposal-card__table-placement">
        Après ce paragraphe
      </p>
      <div className="editorial-proposal-card__table-proposed">
        <h5>Insertion de tableau</h5>
        <p className="editorial-proposal-card__table-title">{table.title}</p>
        <p className="editorial-proposal-card__table-purpose">
          <strong>Consigne · </strong>
          {table.purpose.question}
        </p>
        <div className="editorial-visual-insertion__table-scroll">
          <table aria-label={table.title}>
            <caption>{table.caption || table.title}</caption>
            <thead>
              <tr>
                {table.columns.map((column) => (
                  <th key={column.key} scope="col">
                    {column.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {table.rows.map((row, index) => (
                <tr key={`${table.key}-row-${index}`}>
                  {row.cells.map((cell, cellIndex) => (
                    <td key={`${table.key}-row-${index}-cell-${cellIndex}`}>
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {table.rows.some((row) => row.evidence_handles.length > 0) ? (
          <details className="editorial-proposal-card__table-handles">
            <summary>Références associées aux lignes</summary>
            <ul>
              {table.rows.flatMap((row, index) =>
                row.evidence_handles.map((handle) => (
                  <li key={`${index}-${handle}`}>
                    Ligne {index + 1} · {handle}
                  </li>
                )),
              )}
            </ul>
          </details>
        ) : null}
      </div>
      <details className="editorial-proposal-card__table-evidence">
        <summary>Preuves associées</summary>
        {evidenceRefs && evidenceRefs.length > 0 ? (
          <ul>
            {evidenceRefs.map((reference, index) => (
              <li
                key={`${reference.source_document_id}-${reference.evidence_key}-${index}`}
              >
                <span>
                  Source {reference.source_document_id} · {reference.kind}
                </span>
                <code>{reference.evidence_key}</code>
              </li>
            ))}
          </ul>
        ) : (
          <p>
            Aucune référence de preuve n’est disponible pour cette proposition.
          </p>
        )}
      </details>
    </section>
  );
}

function SourceFigureProposalComparison({
  subjectId,
  figures,
  anchorText,
}: {
  subjectId: string;
  figures: EditorialManualSourceFigurePatchReference[];
  anchorText: string | undefined;
}) {
  return (
    <section
      className="editorial-proposal-card__figure-comparison"
      aria-labelledby="editorial-proposal-figure-heading"
    >
      <div className="editorial-proposal-card__table-anchor">
        <h5>Paragraphe d’ancrage</h5>
        <p>{anchorText || "Paragraphe sélectionné"}</p>
      </div>
      <p className="editorial-proposal-card__table-placement">
        Après ce paragraphe
      </p>
      <div className="editorial-proposal-card__figure-proposed">
        <h5 id="editorial-proposal-figure-heading">
          Comparaison de l’insertion de figures
        </h5>
        <ol>
          {figures.map((figure) => (
            <li key={`${figure.figure_id}-${figure.order}`}>
              <figure className="editorial-proposal-card__source-figure">
                <img
                  src={editorialFigureThumbnailUrl(subjectId, figure.figure_id)}
                  alt={figure.caption}
                  loading="lazy"
                />
                <figcaption>{figure.caption}</figcaption>
                <p>
                  <strong>Source · </strong>
                  {figure.source_url}
                </p>
                <p>
                  <strong>Provenance · </strong>
                  {figure.provenance}
                </p>
                <p>
                  {figure.width} × {figure.height} px · {figure.mime_type} ·{" "}
                  {figure.byte_size} octets
                </p>
                <details>
                  <summary>Preuves et détails de l’archive</summary>
                  <p>Source document · {figure.source_document_id}</p>
                  <p>Figure · {figure.figure_id}</p>
                  <p>SHA-256 · {figure.sha256}</p>
                </details>
              </figure>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}

export function EditorialProposalCard({
  subjectId,
  occurrenceId,
  proposalId,
  referenceOrdinal,
  anchorText,
  onRequestVariant,
  onStateChange,
}: EditorialProposalCardProps) {
  const queryClient = useQueryClient();
  const visualContext = useVisualWorkspaceContext();
  const proposalQueryKey = editorialWorkspaceQueryKeys.proposal(
    subjectId,
    occurrenceId,
    proposalId,
  );
  const proposalQuery = useQuery({
    queryKey: proposalQueryKey,
    queryFn: () => getEditorialProposal(subjectId, occurrenceId, proposalId),
  });
  const proposalState = proposalQuery.data?.state;
  useEffect(() => {
    if (!proposalState) return;
    onStateChange?.(proposalId, proposalState);
  }, [onStateChange, proposalId, proposalState]);
  const invalidateDecisionContext = async () => {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: editorialWorkspaceQueryKeys.snapshot(subjectId),
      }),
      queryClient.invalidateQueries({
        queryKey: ["editorial-pdf-status", subjectId],
      }),
      queryClient.invalidateQueries({
        queryKey: ["editorial-composition-status", subjectId],
      }),
      queryClient.invalidateQueries({
        queryKey: editorialWorkspaceQueryKeys.threadsForOccurrence(
          subjectId,
          occurrenceId,
        ),
      }),
      queryClient.invalidateQueries({
        queryKey: editorialWorkspaceQueryKeys.proposalsForOccurrence(
          subjectId,
          occurrenceId,
        ),
      }),
    ]);
  };
  const acceptMutation = useMutation({
    mutationFn: () => {
      const proposal = proposalQuery.data;
      if (!proposal?.patch_sha256) {
        throw new Error("L’empreinte du patch est indisponible.");
      }
      return acceptEditorialProposal(subjectId, occurrenceId, proposalId, {
        expected_state_version: proposal.state_version,
        patch_sha256: proposal.patch_sha256,
      });
    },
    onSuccess: async (accepted) => {
      queryClient.setQueryData<EditorialProposalRead>(
        proposalQueryKey,
        (current) =>
          current
            ? {
                ...current,
                state: "accepted",
                state_version: accepted.state_version,
              }
            : current,
      );
      await invalidateDecisionContext();
      const patch = proposalQuery.data?.patch;
      const visualTarget =
        patch && isVisualRevisionProposalPatch(patch)
          ? visualPatchTarget(patch.visual_patch)
          : null;
      if (visualTarget && visualContext) {
        visualContext.restoreElementSelection(
          visualTarget.kind,
          visualTarget.key,
          occurrenceId,
        );
      } else {
        focusDocumentBlock(occurrenceId);
      }
    },
  });
  const rejectMutation = useMutation({
    mutationFn: () => {
      const proposal = proposalQuery.data;
      if (!proposal?.patch_sha256) {
        throw new Error("L’empreinte du patch est indisponible.");
      }
      return rejectEditorialProposal(subjectId, occurrenceId, proposalId, {
        expected_state_version: proposal.state_version,
        patch_sha256: proposal.patch_sha256,
      });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: proposalQueryKey,
      });
      focusDocumentBlock(occurrenceId);
    },
  });

  if (proposalQuery.isPending) {
    return (
      <p className="editorial-proposal-card__status" role="status">
        Chargement de la proposition…
      </p>
    );
  }

  if (proposalQuery.isError) {
    return (
      <p className="editorial-proposal-card__status" role="alert">
        {proposalQuery.error.message}
      </p>
    );
  }

  const proposal = proposalQuery.data;
  const patchDocument = proposal.patch;
  const visualProposal = isVisualRevisionProposalPatch(patchDocument)
    ? patchDocument
    : null;
  const visualPatch = visualProposal?.visual_patch ?? null;
  const textPatch = isVisualRevisionProposalPatch(patchDocument)
    ? null
    : patchDocument;
  const proposedText = textPatch?.after;
  const tableProposal = tableFromPatch(textPatch);
  const barChartProposal = barChartFromPatch(textPatch);
  const sourceFigureProposal = sourceFiguresFromPatch(textPatch);
  const visualTarget = visualPatch ? visualPatchTarget(visualPatch) : null;
  const visualSnapshot = visualContext?.snapshot ?? null;
  const currentElement =
    visualTarget && visualSnapshot
      ? (visualSnapshot.blocks.find(
          (block) =>
            block.element_kind === visualTarget.kind &&
            block.element_key === visualTarget.key,
        ) ?? null)
      : null;
  const currentGroup =
    visualPatch?.kind === "reorder_enrichment_elements" && visualSnapshot
      ? visualElementsSharingAnchor(visualSnapshot, visualPatch.anchor_block_id)
      : [];
  const anchorLabel = (blockId: string) =>
    visualContext?.anchorLabel(blockId) ?? null;
  const diagnosticsWarnings = proposalWarnings(proposal.diagnostics);
  const visualWarnings =
    visualPatch?.kind === "move_enrichment_element"
      ? [
          ...diagnosticsWarnings,
          ...(anchorLabel(visualPatch.anchor_block_id) === null
            ? [
                "Le paragraphe d’ancrage proposé n’est plus présent dans cette version. La proposition ne peut pas être acceptée en l’état.",
              ]
            : []),
        ]
      : diagnosticsWarnings;
  const acceptanceConflict =
    acceptMutation.isError &&
    acceptMutation.error instanceof ApiError &&
    acceptMutation.error.status === 409;
  const recoverableProposal = proposal.state === "stale" || acceptanceConflict;
  const acceptanceComplete =
    proposal.state === "accepted" || acceptMutation.isSuccess;
  const decisionPending = acceptMutation.isPending || rejectMutation.isPending;

  return (
    <article
      className="editorial-proposal-card"
      aria-label="Proposition éditoriale"
      aria-busy={proposalQuery.isFetching}
    >
      <header className="editorial-proposal-card__header">
        <h4>
          {referenceOrdinal === undefined
            ? "Proposition"
            : `Proposition ${referenceOrdinal}`}
        </h4>
        <span>{stateLabels[proposal.state]}</span>
      </header>
      <p className="editorial-proposal-card__base">
        Base canonique · version {proposal.base_artifact_version}
      </p>
      {anchorText ? (
        <p className="editorial-proposal-card__target">
          <strong>Bloc ciblé · </strong>
          {anchorText}
        </p>
      ) : null}
      {proposal.origin === "manual_patch" ? (
        <p className="editorial-proposal-card__origin">Modification manuelle</p>
      ) : null}
      {visualProposal ? (
        <VisualProposalDiff
          patch={visualProposal}
          currentElement={currentElement}
          currentGroup={currentGroup}
          anchorLabel={anchorLabel}
          warnings={visualWarnings}
        />
      ) : tableProposal ? (
        <TableProposalComparison
          table={tableProposal}
          anchorText={anchorText}
          evidenceRefs={textPatch?.evidence_refs}
        />
      ) : barChartProposal ? (
        <BarChartProposalComparison
          chart={barChartProposal}
          anchorText={anchorText}
          evidenceRefs={textPatch?.evidence_refs}
        />
      ) : sourceFigureProposal ? (
        <SourceFigureProposalComparison
          subjectId={subjectId}
          figures={sourceFigureProposal}
          anchorText={anchorText}
        />
      ) : typeof proposedText === "string" ? (
        typeof proposal.base_text === "string" ? (
          <EditorialDiffView
            baseText={proposal.base_text}
            proposedText={proposedText}
            baseLabel={
              proposal.state === "stale"
                ? "Version de base"
                : "Version actuelle"
            }
          />
        ) : (
          <div className="editorial-proposal-card__content">
            <h5>Texte proposé</h5>
            <p>{proposedText}</p>
          </div>
        )
      ) : proposal.state === "refused" ? (
        <p className="editorial-proposal-card__status" role="status">
          {refusalLabel(proposal.diagnostics.code)}
        </p>
      ) : proposal.patch === null ? (
        <p className="editorial-proposal-card__status">
          Aucun patch éditorial n’est disponible pour cette proposition.
        </p>
      ) : (
        <p className="editorial-proposal-card__status">
          Patch éditorial typé disponible.
        </p>
      )}
      {typeof proposal.patch?.rationale === "string" ? (
        <p className="editorial-proposal-card__rationale">
          <strong>Justification · </strong>
          {proposal.patch.rationale}
        </p>
      ) : null}
      {!tableProposal &&
      !barChartProposal &&
      !sourceFigureProposal &&
      !visualProposal &&
      (textPatch?.evidence_refs?.length ?? 0) > 0 ? (
        <details className="editorial-proposal-card__text-evidence">
          <summary>Preuves concernées</summary>
          <ul>
            {textPatch?.evidence_refs?.map((reference, index) => (
              <li
                key={`${reference.source_document_id}-${reference.evidence_key}-${index}`}
              >
                <span>
                  Source {reference.source_document_id} · {reference.kind}
                </span>
                <code>{reference.evidence_key}</code>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      {proposal.state === "proposed" || proposal.state === "accepted" ? (
        <div className="editorial-proposal-card__actions">
          <button
            className="editorial-proposal-card__accept"
            type="button"
            aria-disabled={
              acceptanceComplete ||
              acceptanceConflict ||
              decisionPending ||
              !proposal.patch_sha256
            }
            disabled={decisionPending}
            onClick={() => {
              if (
                acceptanceComplete ||
                acceptanceConflict ||
                decisionPending ||
                !proposal.patch_sha256
              ) {
                return;
              }
              acceptMutation.mutate();
            }}
          >
            {acceptanceComplete
              ? "Acceptée"
              : acceptMutation.isPending
                ? "Acceptation en cours…"
                : "Accepter"}
          </button>
          {proposal.state === "proposed" ? (
            <button
              className="editorial-proposal-card__reject"
              type="button"
              onClick={() => rejectMutation.mutate()}
              disabled={
                decisionPending || acceptanceConflict || !proposal.patch_sha256
              }
            >
              Rejeter
            </button>
          ) : null}
          {proposal.state === "proposed" &&
          onRequestVariant &&
          !recoverableProposal ? (
            <button
              className="editorial-proposal-card__variant"
              type="button"
              disabled={decisionPending || acceptanceConflict}
              onClick={() => {
                acceptMutation.reset();
                onRequestVariant();
              }}
            >
              Demander une variante
            </button>
          ) : null}
          {rejectMutation.isError ? (
            <p role="alert">{rejectMutation.error.message}</p>
          ) : null}
        </div>
      ) : null}
      {acceptMutation.isPending ? (
        <p
          className="editorial-proposal-card__status"
          role="status"
          aria-live="polite"
        >
          Acceptation en cours…
        </p>
      ) : null}
      {rejectMutation.isPending ? (
        <p
          className="editorial-proposal-card__status"
          role="status"
          aria-live="polite"
        >
          Rejet en cours…
        </p>
      ) : null}
      {acceptMutation.isSuccess ? (
        <p
          className="editorial-proposal-card__status"
          role="status"
          aria-live="polite"
        >
          Proposition acceptée. Le document et les éléments associés sont
          actualisés.
        </p>
      ) : null}
      {acceptMutation.isError && !acceptanceConflict ? (
        <p className="editorial-proposal-card__error" role="alert">
          {acceptMutation.error.message}
        </p>
      ) : null}
      {recoverableProposal ? (
        <div className="editorial-proposal-card__recovery">
          {acceptanceConflict || proposal.state === "stale" ? (
            <p role="alert" aria-live="polite">
              {acceptanceConflict
                ? acceptMutation.error.message
                : "Cette proposition repose sur une version qui a changé. Elle n’a pas été appliquée. Actualisez le document, puis comparez et formulez une nouvelle proposition."}
            </p>
          ) : null}
          <button
            className="button button--secondary"
            type="button"
            onClick={() => {
              acceptMutation.reset();
              void invalidateDecisionContext();
            }}
          >
            Comparer à nouveau
          </button>
          {onRequestVariant ? (
            <button
              className="button button--secondary"
              type="button"
              onClick={() => {
                acceptMutation.reset();
                onRequestVariant();
              }}
            >
              Demander une variante
            </button>
          ) : null}
        </div>
      ) : null}
      <details className="editorial-proposal-card__details">
        <summary>Détails de la proposition</summary>
        {tableProposal ? (
          <p className="editorial-proposal-card__table-anchor-id">
            Ancre ·{" "}
            {tableProposal.placement.anchor_block_id ?? "non renseignée"}
          </p>
        ) : null}
        {barChartProposal ? (
          <p className="editorial-proposal-card__table-anchor-id">
            Ancre · {barChartProposal.anchor_block_id ?? "non renseignée"}
          </p>
        ) : null}
        {textPatch?.enrichment_patch ? (
          <pre aria-label="Patch éditorial typé">
            {JSON.stringify(textPatch.enrichment_patch, null, 2)}
          </pre>
        ) : null}
        {visualProposal ? (
          <pre aria-label="Révision visuelle typée">
            {JSON.stringify(visualProposal.visual_patch, null, 2)}
          </pre>
        ) : null}
        <dl>
          <div>
            <dt>Opération</dt>
            <dd>{proposal.operation}</dd>
          </div>
          <div>
            <dt>SHA-256 de la base</dt>
            <dd>{proposal.base_canonical_sha256}</dd>
          </div>
          {proposal.patch_sha256 ? (
            <div>
              <dt>SHA-256 du patch</dt>
              <dd>{proposal.patch_sha256}</dd>
            </div>
          ) : null}
        </dl>
      </details>
    </article>
  );
}
