export interface TableColumnDraft {
  label: string;
}

export interface TableRowDraft {
  cells: string[];
  evidenceIdentities: string[];
}

interface ManualTablePreviewProps {
  title: string;
  caption: string;
  columns: TableColumnDraft[];
  rows: TableRowDraft[];
}

export function ManualTablePreview({
  title,
  caption,
  columns,
  rows,
}: ManualTablePreviewProps) {
  return (
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
                  <td key={columnIndex}>{row.cells[columnIndex] || "—"}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
