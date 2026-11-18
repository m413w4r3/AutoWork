import type {
  ArtifactResponse,
  PublicationDocument,
} from "../../api/production";

export function PublicationDiagnosticsPanel({
  diagnostics,
  document,
  editorialEnrichment,
}: {
  diagnostics: unknown;
  document: PublicationDocument;
  editorialEnrichment: ArtifactResponse | undefined;
}) {
  const mediaAssets = [
    ...document.diagrams.map((diagram) => ({
      asset_id: diagram.asset_id,
      key: diagram.key,
      kind: diagram.kind,
      mime_type: "image/svg+xml",
      placement: diagram.placement,
    })),
    ...document.figures.map((figure) => ({
      asset_id: figure.asset_id,
      key: figure.key,
      kind: "source_figure",
      sha256: figure.sha256,
      mime_type: figure.mime_type,
      byte_size: figure.byte_size,
      source_document_id: figure.source_document_id,
      source_url: figure.source_url,
      provenance: figure.provenance,
      locator: figure.locator,
      placement: figure.placement,
    })),
  ];
  const serialized = JSON.stringify(
    {
      diagnostics: diagnostics ?? {},
      editorial_enrichment: editorialEnrichment
        ? {
            artifact_id: editorialEnrichment.artifact_id,
            version: editorialEnrichment.version,
            metadata: editorialEnrichment.metadata,
            canonical_content: editorialEnrichment.canonical_content,
          }
        : null,
      media_assets: mediaAssets,
    },
    null,
    2,
  );
  const diagnosticsValue = diagnostics ?? {};
  const hasDiagnostics =
    typeof diagnosticsValue === "object" &&
    diagnosticsValue !== null &&
    Object.keys(diagnosticsValue).length > 0;
  return (
    <section
      className="publication-diagnostics"
      aria-label="Diagnostics de publication"
    >
      <h3>Diagnostics techniques</h3>
      {!hasDiagnostics && mediaAssets.length === 0 ? (
        <p>Aucun diagnostic technique.</p>
      ) : (
        <pre>{serialized}</pre>
      )}
    </section>
  );
}
