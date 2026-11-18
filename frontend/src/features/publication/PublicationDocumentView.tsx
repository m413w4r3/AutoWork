import { useState } from "react";

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

export function PublicationDocumentView({
  document,
  publicationKind,
}: {
  document: PublicationDocument;
  publicationKind?: "article" | "brief";
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
          publicationKind={publicationKind}
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
