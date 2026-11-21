import { useState } from "react";

import type { EditorialReadOnlyReason } from "../../api/editorialReadOnly";
import type {
  PublicationDocument,
  PublicationEvidenceRefV1,
} from "../../api/production";
import {
  PublicationIndicatorsSection,
  PublicationReferencesSection,
  PublicationSynthesisSection,
} from "./PublicationDocumentSections";
import {
  PublicationLineagePanel,
  type PublicationPassage,
} from "./PublicationProvenance";
import { PublicationSemanticContent } from "./PublicationSemanticContent";
import { editorialReadOnlyReasonText } from "./publicationReadOnly";
import { Link } from "../../routing";

export function PublicationDocumentView({
  document,
  publicationKind,
  readOnlyReason = null,
  currentVersionHref,
  historicalWorkspaceHref,
}: {
  document: PublicationDocument;
  publicationKind: "article" | "brief" | null;
  readOnlyReason?: EditorialReadOnlyReason | null;
  currentVersionHref?: string;
  historicalWorkspaceHref?: string;
}) {
  const [selectedPassage, setSelectedPassage] =
    useState<PublicationPassage | null>(null);
  const sources = new Map(
    document.sources.map((source) => [source.source_document_id, source]),
  );
  const selectPassage = (passage: {
    text: string;
    evidenceRefs: PublicationEvidenceRefV1[];
  }) => setSelectedPassage(passage);

  return (
    <div className="publication-preview__layout">
      <article className="publication-preview">
        {readOnlyReason ? (
          <aside className="editorial-readonly-banner" role="note">
            <strong>
              {readOnlyReason === "historical_version"
                ? "Version historique — lecture seule"
                : "Publication en lecture seule"}
            </strong>
            <p>{editorialReadOnlyReasonText(readOnlyReason)}</p>
            {readOnlyReason === "historical_version" &&
            historicalWorkspaceHref ? (
              <p>
                <Link to={historicalWorkspaceHref}>
                  Ouvrir en lecture seule dans l’atelier
                </Link>
              </p>
            ) : null}
            {readOnlyReason === "historical_version" && currentVersionHref ? (
              <a href={currentVersionHref}>Revenir à la version courante</a>
            ) : null}
          </aside>
        ) : null}
        {publicationKind ? (
          <p className="publication-preview__kind">
            Format : {publicationKind === "brief" ? "Brève" : "Article"}
          </p>
        ) : null}
        <h3>
          <PublicationSemanticContent
            document={document}
            text={document.title}
            anchor="title"
          />
        </h3>
        <PublicationReferencesSection
          document={document}
          sources={sources}
          onSelectPassage={selectPassage}
        />
        <PublicationSynthesisSection
          document={document}
          sources={sources}
          onSelectPassage={selectPassage}
        />
        <PublicationIndicatorsSection
          document={document}
          publicationKind={publicationKind ?? undefined}
          sources={sources}
        />
      </article>
      {selectedPassage ? (
        <PublicationLineagePanel
          passage={selectedPassage}
          sources={sources}
          onClose={() => setSelectedPassage(null)}
        />
      ) : null}
    </div>
  );
}
