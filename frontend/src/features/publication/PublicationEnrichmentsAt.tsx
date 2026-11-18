import type { PublicationDocument } from "../../api/production";
import { PublicationDiagramsView } from "./PublicationDiagramView";
import { PublicationFiguresView } from "./PublicationFigureView";
import { PublicationTablesView } from "./PublicationTableView";

export function PublicationEnrichmentsAt({
  document,
  kind,
  sectionIndex,
}: {
  document: PublicationDocument;
  kind: string;
  sectionIndex: number | null;
}) {
  return (
    <>
      <PublicationTablesView
        document={document}
        tables={document.tables.filter(
          (item) =>
            item.placement.kind === kind &&
            item.placement.section_index === sectionIndex,
        )}
      />
      <PublicationDiagramsView
        document={document}
        subjectId={document.subject_id}
        diagrams={document.diagrams.filter(
          (item) =>
            item.placement.kind === kind &&
            item.placement.section_index === sectionIndex,
        )}
      />
      <PublicationFiguresView
        document={document}
        subjectId={document.subject_id}
        figures={document.figures.filter(
          (item) =>
            item.placement.kind === kind &&
            item.placement.section_index === sectionIndex,
        )}
      />
    </>
  );
}
