import type {
  PublicationDiagramV1,
  PublicationDocument,
} from "../../api/production";
import { PublicationSemanticContent } from "./PublicationSemanticContent";

export function PublicationDiagramsView({
  document,
  subjectId,
  diagrams,
}: {
  document: PublicationDocument;
  subjectId: string;
  diagrams: PublicationDiagramV1[];
}) {
  if (diagrams.length === 0) return null;
  return (
    <section>
      <h4>Diagrammes</h4>
      {diagrams.map((diagram) => (
        <article key={diagram.key}>
          <h5>
            <PublicationSemanticContent
              document={document}
              text={diagram.title}
              anchor={`diagram:${diagram.key}:title`}
            />
          </h5>
          <figure>
            <img
              alt={diagram.title}
              loading="lazy"
              src={`/api/subjects/${encodeURIComponent(subjectId)}/publication/assets/${encodeURIComponent(diagram.asset_id)}`}
            />
            {diagram.caption ? (
              <figcaption>
                <PublicationSemanticContent
                  document={document}
                  text={diagram.caption}
                  anchor={`diagram:${diagram.key}:caption`}
                />
              </figcaption>
            ) : null}
          </figure>
        </article>
      ))}
    </section>
  );
}
