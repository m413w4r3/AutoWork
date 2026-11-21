export interface BarChartPreviewEvidence {
  label: string;
  passage: string | null;
  detail: string;
}

export interface BarChartPreviewPoint {
  label: string;
  value: string;
  evidence?: BarChartPreviewEvidence[];
}

interface ManualBarChartPreviewProps {
  title: string;
  caption?: string | null;
  legend: readonly string[];
  categoryAxisLabel: string;
  valueAxisLabel: string;
  unit: string;
  points: readonly BarChartPreviewPoint[];
}

function parsedValue(value: string): number | null {
  if (!value.trim()) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function displayValue(value: string): string {
  return value.trim();
}

export function ManualBarChartPreview({
  title,
  caption,
  legend,
  categoryAxisLabel,
  valueAxisLabel,
  unit,
  points,
}: ManualBarChartPreviewProps) {
  const magnitudes = points.flatMap((point) => {
    const value = parsedValue(point.value);
    return value === null ? [] : [Math.abs(value)];
  });
  const maximumMagnitude = Math.max(0, ...magnitudes);

  return (
    <figure
      className="editorial-bar-chart"
      aria-label={`Graphique : ${title || "Aperçu du graphique"}`}
    >
      <figcaption className="editorial-bar-chart__caption">
        <h4>{title || "Titre du graphique"}</h4>
        {caption ? <p>{caption}</p> : null}
      </figcaption>
      <div
        className="editorial-bar-chart__legend"
        role="group"
        aria-label="Légende"
      >
        {legend.length > 0 ? (
          <ul>
            {legend.map((entry) => (
              <li key={entry}>
                <span aria-hidden="true" />
                {entry}
              </li>
            ))}
          </ul>
        ) : (
          <p>Aucune légende renseignée.</p>
        )}
      </div>
      <p className="editorial-bar-chart__axis">
        {categoryAxisLabel || "Catégories"} · {valueAxisLabel || "Valeurs"}
        {unit ? ` (${unit})` : ""}
      </p>
      <ol className="editorial-bar-chart__points">
        {points.map((point, index) => {
          const numericValue = parsedValue(point.value);
          const magnitude = numericValue === null ? 0 : Math.abs(numericValue);
          const width =
            maximumMagnitude === 0 ? 0 : (magnitude / maximumMagnitude) * 50;
          const barStyle =
            numericValue !== null && numericValue < 0
              ? { right: "50%", width: `${width}%` }
              : { left: "50%", width: `${width}%` };

          return (
            <li
              className="editorial-bar-chart__point"
              key={`${point.label}-${index}`}
            >
              <span className="editorial-bar-chart__point-label">
                {point.label || `Point ${index + 1}`}
              </span>
              <span className="editorial-bar-chart__track" aria-hidden="true">
                <span className="editorial-bar-chart__bar" style={barStyle} />
              </span>
              <span className="editorial-bar-chart__value">
                {displayValue(point.value)} {unit}
              </span>
              {point.evidence && point.evidence.length > 0 ? (
                <details className="editorial-bar-chart__evidence">
                  <summary>
                    Sources du point {index + 1} ({point.evidence.length})
                  </summary>
                  <ul>
                    {point.evidence.map((source, sourceIndex) => (
                      <li key={`${source.detail}-${sourceIndex}`}>
                        <span>{source.label}</span>
                        {source.passage ? <p>{source.passage}</p> : null}
                        <code>{source.detail}</code>
                      </li>
                    ))}
                  </ul>
                </details>
              ) : null}
            </li>
          );
        })}
      </ol>
    </figure>
  );
}
