import type { Ref } from "react";

import type { AdmittedEvidence } from "./ManualTableEditor";
import {
  ManualBarChartPreview,
  type BarChartPreviewEvidence,
} from "./ManualBarChartPreview";

export interface BarChartPointDraft {
  label: string;
  value: string;
  evidenceIdentities: string[];
}

interface ManualBarChartEditorProps {
  instruction: string;
  instructionRef: Ref<HTMLTextAreaElement>;
  title: string;
  legend: string;
  categoryAxisLabel: string;
  valueAxisLabel: string;
  unit: string;
  points: BarChartPointDraft[];
  admitted: AdmittedEvidence[];
  onInstructionChange: (value: string) => void;
  onTitleChange: (value: string) => void;
  onLegendChange: (value: string) => void;
  onCategoryAxisLabelChange: (value: string) => void;
  onValueAxisLabelChange: (value: string) => void;
  onUnitChange: (value: string) => void;
  onPointLabelChange: (index: number, value: string) => void;
  onPointValueChange: (index: number, value: string) => void;
  onTogglePointEvidence: (
    index: number,
    identity: string,
    checked: boolean,
  ) => void;
  onAddPoint: () => void;
  onRemovePoint: (index: number) => void;
}

function previewEvidence(item: AdmittedEvidence): BarChartPreviewEvidence {
  return {
    label: item.label,
    passage: item.passage,
    detail: `${item.reference.source_document_id} · ${item.reference.kind} · ${item.reference.evidence_key}`,
  };
}

export function ManualBarChartEditor({
  instruction,
  instructionRef,
  title,
  legend,
  categoryAxisLabel,
  valueAxisLabel,
  unit,
  points,
  admitted,
  onInstructionChange,
  onTitleChange,
  onLegendChange,
  onCategoryAxisLabelChange,
  onValueAxisLabelChange,
  onUnitChange,
  onPointLabelChange,
  onPointValueChange,
  onTogglePointEvidence,
  onAddPoint,
  onRemovePoint,
}: ManualBarChartEditorProps) {
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
        <span>Titre du graphique</span>
        <input
          value={title}
          onChange={(event) => onTitleChange(event.currentTarget.value)}
          maxLength={1000}
        />
      </label>
      <label className="editorial-visual-insertion__field">
        <span>Légende</span>
        <input
          value={legend}
          onChange={(event) => onLegendChange(event.currentTarget.value)}
          maxLength={120}
        />
      </label>
      <div className="editorial-visual-insertion__chart-axes">
        <label className="editorial-visual-insertion__field">
          <span>Intitulé des catégories</span>
          <input
            value={categoryAxisLabel}
            onChange={(event) =>
              onCategoryAxisLabelChange(event.currentTarget.value)
            }
            maxLength={120}
          />
        </label>
        <label className="editorial-visual-insertion__field">
          <span>Intitulé des valeurs</span>
          <input
            value={valueAxisLabel}
            onChange={(event) =>
              onValueAxisLabelChange(event.currentTarget.value)
            }
            maxLength={120}
          />
        </label>
        <label className="editorial-visual-insertion__field">
          <span>Unité</span>
          <input
            value={unit}
            onChange={(event) => onUnitChange(event.currentTarget.value)}
            maxLength={120}
          />
        </label>
      </div>
      <fieldset className="editorial-visual-insertion__chart-points">
        <legend>Points et preuves associées</legend>
        {points.map((point, pointIndex) => (
          <section
            className="editorial-visual-insertion__chart-point"
            key={pointIndex}
            aria-labelledby={`editorial-chart-point-${pointIndex + 1}`}
          >
            <header>
              <h4 id={`editorial-chart-point-${pointIndex + 1}`}>
                Point {pointIndex + 1}
              </h4>
              <button
                className="button button--secondary"
                type="button"
                disabled={points.length <= 2}
                aria-label={`Supprimer le point ${pointIndex + 1}`}
                onClick={() => onRemovePoint(pointIndex)}
              >
                Supprimer le point
              </button>
            </header>
            <label>
              <span>Libellé du point {pointIndex + 1}</span>
              <input
                value={point.label}
                maxLength={500}
                onChange={(event) =>
                  onPointLabelChange(pointIndex, event.currentTarget.value)
                }
              />
            </label>
            <label>
              <span>Valeur du point {pointIndex + 1}</span>
              <input
                type="number"
                step="any"
                value={point.value}
                onChange={(event) =>
                  onPointValueChange(pointIndex, event.currentTarget.value)
                }
              />
            </label>
            <fieldset
              className="editorial-visual-insertion__evidence"
              aria-label={`Preuves du point ${pointIndex + 1}`}
            >
              <legend>Preuves admises pour ce point</legend>
              {admitted.length > 0 ? (
                admitted.map((item) => (
                  <label key={item.identity}>
                    <input
                      type="checkbox"
                      checked={point.evidenceIdentities.includes(item.identity)}
                      onChange={(event) =>
                        onTogglePointEvidence(
                          pointIndex,
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
          disabled={points.length >= 24}
          onClick={onAddPoint}
        >
          Ajouter un point
        </button>
      </fieldset>
      <section
        className="editorial-visual-insertion__chart-preview"
        aria-labelledby="editorial-bar-chart-preview-heading"
      >
        <h4 id="editorial-bar-chart-preview-heading">Aperçu du graphique</h4>
        <ManualBarChartPreview
          title={title}
          legend={legend.trim() ? [legend.trim()] : []}
          categoryAxisLabel={categoryAxisLabel}
          valueAxisLabel={valueAxisLabel}
          unit={unit}
          points={points.map((point) => ({
            label: point.label,
            value: point.value,
            evidence: admitted
              .filter((item) =>
                point.evidenceIdentities.includes(item.identity),
              )
              .map(previewEvidence),
          }))}
        />
      </section>
    </>
  );
}
