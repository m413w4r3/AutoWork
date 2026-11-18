import type {
  PublicationDocument,
  PublicationSourceFigureV1,
} from "../../api/production";
import { PublicationSemanticContent } from "./PublicationSemanticContent";

export function PublicationFiguresView({
  document,
  subjectId,
  figures,
}: {
  document: PublicationDocument;
  subjectId: string;
  figures: PublicationSourceFigureV1[];
}) {
  if (figures.length === 0) return null;
  return (
    <section>
      <h4>Figures source</h4>
      {figures.map((figure) => (
        <article key={figure.key}>
          <figure>
            <img
              alt={figure.caption || "Figure issue de la source"}
              loading="lazy"
              src={`/api/subjects/${encodeURIComponent(subjectId)}/publication/assets/${encodeURIComponent(figure.asset_id)}`}
            />
            {figure.caption ? (
              <figcaption>
                <PublicationSemanticContent
                  document={document}
                  text={figure.caption}
                  anchor={`figure:${figure.key}:caption`}
                />
              </figcaption>
            ) : null}
          </figure>
        </article>
      ))}
    </section>
  );
}
