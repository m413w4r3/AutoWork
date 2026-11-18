import type {
  PublicationDocument,
  PublicationTableV1,
} from "../../api/production";
import { PublicationSemanticContent } from "./PublicationSemanticContent";

export function PublicationTablesView({
  document,
  tables,
}: {
  document: PublicationDocument;
  tables: PublicationTableV1[];
}) {
  if (tables.length === 0) return null;
  return (
    <section>
      <h4>Tableaux</h4>
      {tables.map((table) => (
        <article key={table.key}>
          <h5>
            <PublicationSemanticContent
              document={document}
              text={table.title}
              anchor={`table:${table.key}:title`}
            />
          </h5>
          {table.caption && (
            <p>
              <PublicationSemanticContent
                document={document}
                text={table.caption}
                anchor={`table:${table.key}:caption`}
              />
            </p>
          )}
          <table>
            <thead>
              <tr>
                {table.columns.map((column, columnIndex) => (
                  <th key={column.key}>
                    <PublicationSemanticContent
                      document={document}
                      text={column.label}
                      anchor={`table:${table.key}:column:${String(columnIndex + 1).padStart(4, "0")}`}
                    />
                  </th>
                ))}
                <th>Preuves</th>
              </tr>
            </thead>
            <tbody>
              {table.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.cells.map((cell, cellIndex) => (
                    <td key={cellIndex}>
                      <PublicationSemanticContent
                        document={document}
                        text={cell}
                        anchor={`table:${table.key}:row:${String(rowIndex + 1).padStart(4, "0")}:cell:${String(cellIndex + 1).padStart(4, "0")}`}
                      />
                    </td>
                  ))}
                  <td>{row.evidence_refs.length} preuves</td>
                </tr>
              ))}
            </tbody>
          </table>
        </article>
      ))}
    </section>
  );
}
