import type {
  EditorialEvidenceReference,
  EditorialVisualRevisionProposalPatch,
  EditorialWorkspaceBlock,
} from "../../api/editorialWorkspace";
import { EditorialDiffView } from "./EditorialDiffView";
import {
  visualElementAnchorId,
  visualElementKindLabel,
  visualElementTitle,
} from "./visualElementOperations";

interface VisualProposalDiffProps {
  patch: EditorialVisualRevisionProposalPatch;
  currentElement: EditorialWorkspaceBlock | null;
  currentGroup: readonly EditorialWorkspaceBlock[];
  anchorLabel: (blockId: string) => string | null;
  warnings: readonly string[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0
    ? value.trim()
    : null;
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

interface PointSummary {
  label: string;
  series: string;
  value: string;
}

interface ChartSummary {
  title: string;
  caption: string | null;
  categoryAxisLabel: string;
  valueAxisLabel: string;
  unit: string;
  points: PointSummary[];
  evidenceRefs: EditorialEvidenceReference[];
}

function evidenceReference(value: unknown): EditorialEvidenceReference | null {
  if (
    !isRecord(value) ||
    typeof value.source_document_id !== "string" ||
    typeof value.kind !== "string" ||
    typeof value.evidence_key !== "string"
  ) {
    return null;
  }
  const kind = value.kind;
  if (
    kind !== "fact" &&
    kind !== "event" &&
    kind !== "source_metadata" &&
    kind !== "indicator" &&
    kind !== "rule" &&
    kind !== "uncertainty"
  ) {
    return null;
  }
  return {
    source_document_id: value.source_document_id,
    kind,
    evidence_key: value.evidence_key,
  };
}

function chartSummary(raw: Record<string, unknown>): ChartSummary {
  const evidenceRefs = new Map<string, EditorialEvidenceReference>();
  const points = records(raw.points).flatMap((point) => {
    const label = text(point.label) ?? "";
    const series = text(point.series) ?? "";
    const value =
      typeof point.value === "string"
        ? point.value
        : typeof point.date === "string"
          ? point.date
          : "";
    for (const reference of records(point.evidence_refs)) {
      const parsed = evidenceReference(reference);
      if (parsed) {
        evidenceRefs.set(
          `${parsed.source_document_id}:${parsed.kind}:${parsed.evidence_key}`,
          parsed,
        );
      }
    }
    return label || value ? [{ label, series, value }] : [];
  });
  return {
    title: text(raw.title) ?? "",
    caption: text(raw.caption),
    categoryAxisLabel: text(raw.category_axis_label) ?? "",
    valueAxisLabel: text(raw.value_axis_label) ?? "",
    unit: text(raw.unit) ?? "",
    points,
    evidenceRefs: [...evidenceRefs.values()],
  };
}

interface DiagramSummary {
  title: string;
  caption: string | null;
  direction: string;
  nodes: { id: string; label: string }[];
  edges: { id: string; label: string }[];
}

function diagramSummary(raw: Record<string, unknown>): DiagramSummary {
  const nodeLabels = new Map<string, string>();
  const nodes = records(raw.nodes).flatMap((node) => {
    const id = text(node.node_id);
    if (!id) return [];
    const label = text(node.label) ?? id;
    nodeLabels.set(id, label);
    return [{ id, label }];
  });
  const edges = records(raw.edges).flatMap((edge) => {
    const source = text(edge.source_node_id);
    const target = text(edge.target_node_id);
    if (!source || !target) return [];
    const sourceLabel = nodeLabels.get(source) ?? source;
    const targetLabel = nodeLabels.get(target) ?? target;
    const label = text(edge.label);
    return [
      {
        id: `${source}->${target}`,
        label: `${sourceLabel} → ${targetLabel}${label ? ` (${label})` : ""}`,
      },
    ];
  });
  return {
    title: text(raw.title) ?? "",
    caption: text(raw.caption),
    direction: text(raw.direction) ?? "",
    nodes,
    edges,
  };
}

function FieldComparison({
  label,
  current,
  proposed,
}: {
  label: string;
  current: string;
  proposed: string;
}) {
  const changed = current !== proposed;
  return (
    <div className="visual-proposal-diff__field" data-changed={changed}>
      <dt>{label}</dt>
      <dd>
        <span className="visual-proposal-diff__current">{current || "—"}</span>
        {changed ? (
          <>
            <span className="visual-proposal-diff__marker">Modifié</span>
            <span className="visual-proposal-diff__proposed">
              {proposed || "—"}
            </span>
          </>
        ) : null}
      </dd>
    </div>
  );
}

function ChartProposalComparison({
  chart,
  currentElement,
}: {
  chart: ChartSummary;
  currentElement: EditorialWorkspaceBlock | null;
}) {
  const current = currentElement?.chart ?? null;
  const currentPoints = current
    ? current.points.map((point) => ({
        label: point.label,
        series: "",
        value: point.value,
      }))
    : [];
  // The snapshot projections do not expose a series per existing point; when
  // the current side has no series identity, compare points by label only.
  const currentHasSeries = currentPoints.some(
    (point) => point.series.length > 0,
  );
  const pointKey = (point: PointSummary) =>
    currentHasSeries && point.series
      ? `${point.series}\u0000${point.label}`
      : point.label;
  const pointKeys = Array.from(
    new Set([...currentPoints, ...chart.points].map(pointKey)),
  );
  const currentByKey = new Map(
    currentPoints.map((point) => [pointKey(point), point]),
  );
  const proposedByKey = new Map(
    chart.points.map((point) => [pointKey(point), point]),
  );

  return (
    <section
      className="visual-proposal-diff__chart"
      aria-label="Comparaison du graphique"
    >
      <dl className="visual-proposal-diff__fields">
        <FieldComparison
          label="Titre"
          current={current?.title ?? ""}
          proposed={chart.title}
        />
        <FieldComparison
          label="Légende"
          current={current?.caption ?? ""}
          proposed={chart.caption ?? ""}
        />
        <FieldComparison
          label="Unité"
          current={current?.unit ?? ""}
          proposed={chart.unit}
        />
        <FieldComparison
          label="Axe des catégories"
          current={current?.category_axis_label ?? ""}
          proposed={chart.categoryAxisLabel}
        />
        <FieldComparison
          label="Axe des valeurs"
          current={current?.value_axis_label ?? ""}
          proposed={chart.valueAxisLabel}
        />
      </dl>
      <ul
        className="visual-proposal-diff__points"
        aria-label="Valeurs du graphique proposé"
      >
        {pointKeys.map((key) => {
          const currentPoint = currentByKey.get(key);
          const proposedPoint = proposedByKey.get(key);
          const state = !currentPoint
            ? "Ajouté"
            : !proposedPoint
              ? "Retiré"
              : currentPoint.value !== proposedPoint.value
                ? "Modifié"
                : "Inchangé";
          const [first, second] = key.split("\u0000");
          const label = second ?? first ?? "";
          const series = proposedPoint?.series ?? currentPoint?.series ?? "";
          return (
            <li key={key} data-state={state}>
              <p className="visual-proposal-diff__point-label">
                {label}
                {series ? <span> · {series}</span> : null}
              </p>
              <p className="visual-proposal-diff__point-values">
                <span>
                  Valeur actuelle ·{" "}
                  <strong>{currentPoint?.value ?? "—"}</strong>
                </span>
                <span>
                  Valeur proposée ·{" "}
                  <strong>{proposedPoint?.value ?? "—"}</strong>
                </span>
                <span className="visual-proposal-diff__marker">{state}</span>
              </p>
            </li>
          );
        })}
      </ul>
      {chart.evidenceRefs.length > 0 ? (
        <details className="visual-proposal-diff__evidence">
          <summary>Preuves utilisées</summary>
          <ul>
            {chart.evidenceRefs.map((reference) => (
              <li
                key={`${reference.source_document_id}-${reference.kind}-${reference.evidence_key}`}
              >
                <span>
                  Source {reference.source_document_id} · {reference.kind}
                </span>
                <code>{reference.evidence_key}</code>
              </li>
            ))}
          </ul>
        </details>
      ) : (
        <p className="visual-proposal-diff__evidence-note">
          Les valeurs et l’unité seront revérifiées contre les preuves admises
          au moment de l’adoption ; aucune valeur ne peut être inventée.
        </p>
      )}
    </section>
  );
}

function StructureDifferenceList({
  label,
  current,
  proposed,
}: {
  label: string;
  current: Map<string, string>;
  proposed: Map<string, string>;
}) {
  const rows: { id: string; state: string; label: string }[] = [];
  for (const id of new Set([...current.keys(), ...proposed.keys()])) {
    const before = current.get(id);
    const after = proposed.get(id);
    if (before === undefined) {
      rows.push({ id, state: "Ajouté", label: after ?? id });
    } else if (after === undefined) {
      rows.push({ id, state: "Retiré", label: before });
    } else if (before !== after) {
      rows.push({ id, state: "Modifié", label: after });
    }
  }
  return (
    <div className="visual-proposal-diff__structure">
      <h5>{label}</h5>
      {rows.length === 0 ? (
        <p>Aucune différence.</p>
      ) : (
        <ul>
          {rows.map((row) => (
            <li key={row.id} data-state={row.state}>
              <span className="visual-proposal-diff__marker">{row.state}</span>
              <span>{row.label}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function DiagramProposalComparison({
  diagram,
  currentElement,
}: {
  diagram: DiagramSummary;
  currentElement: EditorialWorkspaceBlock | null;
}) {
  const current = currentElement?.diagram ?? null;
  return (
    <section
      className="visual-proposal-diff__diagram"
      aria-label="Comparaison du diagramme"
    >
      <dl className="visual-proposal-diff__fields">
        <FieldComparison
          label="Titre"
          current={current?.title ?? ""}
          proposed={diagram.title}
        />
        <FieldComparison
          label="Légende"
          current={current?.caption ?? ""}
          proposed={diagram.caption ?? ""}
        />
        <FieldComparison
          label="Direction"
          current={current?.direction ?? ""}
          proposed={diagram.direction}
        />
      </dl>
      <StructureDifferenceList
        label="Nœuds"
        current={
          new Map(
            (current?.nodes ?? []).map((node) => [node.node_id, node.label]),
          )
        }
        proposed={new Map(diagram.nodes.map((node) => [node.id, node.label]))}
      />
      <StructureDifferenceList
        label="Relations"
        current={
          new Map(
            (current?.edges ?? []).map((edge) => [
              `${edge.source_node_id}->${edge.target_node_id}`,
              edge.label
                ? `${edge.source_node_id} → ${edge.target_node_id} (${edge.label})`
                : `${edge.source_node_id} → ${edge.target_node_id}`,
            ]),
          )
        }
        proposed={new Map(diagram.edges.map((edge) => [edge.id, edge.label]))}
      />
    </section>
  );
}

export function VisualProposalDiff({
  patch,
  currentElement,
  currentGroup,
  anchorLabel,
  warnings,
}: VisualProposalDiffProps) {
  const visualPatch = patch.visual_patch;
  const title = currentElement
    ? `${visualElementKindLabel(currentElement)} · ${visualElementTitle(currentElement)}`
    : null;

  return (
    <section
      className="visual-proposal-diff"
      aria-label="Comparaison de la révision visuelle"
    >
      {title ? (
        <p className="visual-proposal-diff__target">
          <strong>Cible · </strong>
          {title}
        </p>
      ) : null}
      {visualPatch.kind === "update_visual_caption" ? (
        <EditorialDiffView
          baseText={
            currentElement?.caption?.trim()
              ? currentElement.caption
              : "Aucune légende"
          }
          proposedText={
            visualPatch.caption?.trim() ? visualPatch.caption : "Aucune légende"
          }
          baseLabel="Légende actuelle"
        />
      ) : visualPatch.kind === "revise_chart" ? (
        <ChartProposalComparison
          chart={chartSummary(visualPatch.chart)}
          currentElement={currentElement}
        />
      ) : visualPatch.kind === "revise_diagram" ? (
        <DiagramProposalComparison
          diagram={diagramSummary(visualPatch.diagram)}
          currentElement={currentElement}
        />
      ) : visualPatch.kind === "move_enrichment_element" ? (
        <div className="visual-proposal-diff__placement">
          <p>
            <strong>Emplacement actuel · </strong>
            {currentElement
              ? anchorLabel(visualElementAnchorId(currentElement) ?? "")
              : null}
          </p>
          <p>
            <strong>Emplacement proposé · </strong>
            {anchorLabel(visualPatch.anchor_block_id) ?? (
              <span className="visual-proposal-diff__unknown">
                Paragraphe d’ancrage introuvable dans cette version
              </span>
            )}
          </p>
        </div>
      ) : visualPatch.kind === "reorder_enrichment_elements" ? (
        <div className="visual-proposal-diff__order">
          <section aria-label="Ordre actuel">
            <h5>Ordre actuel</h5>
            <ol>
              {currentGroup.map((block) => (
                <li key={`current-${block.element_kind}-${block.element_key}`}>
                  {visualElementTitle(block)}
                </li>
              ))}
            </ol>
          </section>
          <section aria-label="Ordre proposé">
            <h5>Ordre proposé</h5>
            <ol>
              {visualPatch.order.map((ref) => {
                const block = currentGroup.find(
                  (candidate) =>
                    candidate.element_kind === ref.kind &&
                    candidate.element_key === ref.key,
                );
                return (
                  <li key={`proposed-${ref.kind}-${ref.key}`}>
                    {block ? visualElementTitle(block) : ref.key}
                  </li>
                );
              })}
            </ol>
          </section>
        </div>
      ) : visualPatch.kind === "remove_enrichment_element" ? (
        <div className="visual-proposal-diff__remove">
          <p>
            Retirer ce visuel du document ? Il restera dans les sources
            archivées et dans l’historique.
          </p>
          <p>
            Élément retiré :{" "}
            {currentElement
              ? visualElementTitle(currentElement)
              : visualPatch.element_key}
          </p>
        </div>
      ) : (
        <div className="visual-proposal-diff__replace">
          <p>
            Remplacement de la figure archivée par une autre figure éligible.
          </p>
          <p>
            <strong>Légende actuelle · </strong>
            {currentElement?.figure?.caption ?? currentElement?.caption ?? "—"}
          </p>
          <p>
            <strong>Légende proposée · </strong>
            {visualPatch.caption ??
              currentElement?.caption ??
              "Conservée à l’identique"}
          </p>
          <p>
            La figure choisie sera revérifiée à l’adoption : statut, blob,
            dimensions et usage unique.
          </p>
        </div>
      )}
      {warnings.length > 0 ? (
        <div className="visual-proposal-diff__warnings" role="status">
          <h5>Avertissements</h5>
          <ul>
            {warnings.map((warning, index) => (
              <li key={`${index}-${warning}`}>{warning}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
