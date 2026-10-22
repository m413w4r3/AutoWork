import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getEditionComposition } from "../../api/editions";
import {
  acceptEditionComposition,
  editionRulesUrl,
  getEditionReleaseLineage,
  getRenderInputInspection,
  getEditionRelease,
  pdfInspectionUrl,
  releasePdfUrl,
  renderInputInspectionUrl,
  retryEditionRender,
  versionedReleasePdfUrl,
  type EditionReleaseResponse,
} from "../../api/publication";
import type { EditionReleaseLineage } from "../../api/publication";
import { ApiError } from "../../api/editions";
import { publicationPollingInterval } from "./publicationPolling";

function readableDate(value: string): string {
  return new Intl.DateTimeFormat("fr-FR", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function isMissingRelease(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

function invalidatePublication(
  queryClient: ReturnType<typeof useQueryClient>,
  editionId: string,
) {
  void queryClient.invalidateQueries({
    queryKey: ["edition-release", editionId],
  });
  void queryClient.invalidateQueries({ queryKey: ["edition", editionId] });
  void queryClient.invalidateQueries({ queryKey: ["editions"] });
  void queryClient.invalidateQueries({
    queryKey: ["edition-composition", editionId],
  });
}

function RenderInspectionLinks({ renderId }: { renderId: string }) {
  const inspection = useQuery({
    queryKey: ["render-input-inspection", renderId],
    queryFn: () => getRenderInputInspection(renderId),
    retry: false,
  });
  const pdfSha256 =
    inspection.data?.hashes.pdf_sha256 ?? inspection.data?.pdf.sha256 ?? null;
  return (
    <nav aria-label="Inspections du rendu publié">
      <a href={renderInputInspectionUrl(renderId)}>
        Inspecter les entrées Typst
      </a>
      {inspection.isPending ? <span> · Lecture du hash PDF…</span> : null}
      {pdfSha256 ? (
        <a href={pdfInspectionUrl(pdfSha256)}>Inspecter le PDF</a>
      ) : inspection.isError ? (
        <span> · Hash PDF indisponible pour l’inspection</span>
      ) : null}
    </nav>
  );
}

function DownloadActions({
  editionId,
  releaseId,
  pdfAvailable,
}: {
  editionId: string;
  releaseId: string | null;
  pdfAvailable: boolean;
}) {
  const pdfHref = releaseId
    ? versionedReleasePdfUrl(editionId, releaseId)
    : releasePdfUrl(editionId);
  return (
    <div className="publication-console__downloads">
      {pdfAvailable ? (
        <a
          className="button publication-console__download"
          href={pdfHref}
          download
        >
          Télécharger le PDF
        </a>
      ) : null}
      <a
        className="button publication-console__download publication-console__download--rules"
        href={editionRulesUrl(editionId)}
        download
      >
        Télécharger les règles (ZIP)
      </a>
    </div>
  );
}

function ReleaseLineageStatus({
  editionId,
  releaseId,
}: {
  editionId: string;
  releaseId: string | null;
}) {
  const lineage = useQuery({
    queryKey: ["edition-release-lineage", editionId, releaseId ?? "none"],
    queryFn: () => getEditionReleaseLineage(editionId, releaseId ?? ""),
    enabled: Boolean(releaseId),
    retry: false,
  });
  if (!releaseId) return null;
  if (lineage.isPending) return <p role="status">Lecture du lignage PDF…</p>;
  if (lineage.isError) {
    return <p>Le manifeste de lignage du release est indisponible.</p>;
  }
  const value: EditionReleaseLineage = lineage.data;
  return (
    <section aria-label="Lignage du PDF publié">
      <p>
        Nature <strong>{value.nature}</strong> ·{" "}
        <strong>{value.is_current ? "actuel" : "historique"}</strong> · release{" "}
        <code>{value.edition_release_id}</code>
      </p>
      <p>
        PDF SHA-256 · <code>{value.pdf_sha256 ?? "inconnu"}</code> · render-data{" "}
        SHA-256 · <code>{value.render_data_sha256 ?? "inconnu"}</code>
      </p>
      {value.history_reasons.length > 0 ? (
        <p>Historique : {value.history_reasons.join(", ")}</p>
      ) : null}
      <ul>
        {value.publications.map((publication) => (
          <li
            key={`${publication.subject_id}-${publication.artifact?.id ?? "unknown"}`}
          >
            Artefact {publication.artifact?.id ?? "non identifiable"} v
            {publication.artifact?.version ?? "?"} · SHA-256{" "}
            {publication.artifact?.sha256 ?? "inconnu"}
            {publication.opinion ? (
              <>
                {" "}
                · avis {publication.opinion.id} v{publication.opinion.version} (
                {publication.opinion.status})
              </>
            ) : null}
            {publication.review_decision_id === null
              ? " · décision de revue non épinglée"
              : null}
            {publication.historical ? " · historique" : " · actuel"}
          </li>
        ))}
      </ul>
    </section>
  );
}

function RenderStatus({ release }: { release: EditionReleaseResponse }) {
  if (release.pdf_available) {
    return (
      <>
        <h3 role="status">Bulletin publié</h3>
        {release.published_at ? (
          <p>Publié le {readableDate(release.published_at)}</p>
        ) : null}
      </>
    );
  }
  if (release.render_status === "failed") {
    return (
      <>
        <p role="status">Le rendu PDF a échoué.</p>
        {release.render_error_code ? (
          <p className="publication-console__diagnostic-code">
            {release.render_error_code}
          </p>
        ) : null}
        {release.render_error_message ? (
          <p className="error-message">{release.render_error_message}</p>
        ) : null}
      </>
    );
  }
  return (
    <p role="status">
      {release.render_status === "not_started"
        ? "Le rendu PDF n’a pas encore démarré."
        : "Génération du PDF en cours"}
    </p>
  );
}

function ArchivedPublication({
  editionId,
  release,
}: {
  editionId: string;
  release: EditionReleaseResponse | null;
}) {
  return (
    <section
      className="workflow-placeholder publication-console"
      aria-live="polite"
    >
      <p className="eyebrow">Publication</p>
      <h2>Édition archivée</h2>
      <p>Cette édition est disponible en lecture seule.</p>
      {release?.json_available ? (
        <>
          <h3>Assemblage canonique</h3>
          <p>Release assemblé</p>
          <h3>Rendu PDF</h3>
          <RenderStatus release={release} />
          <DownloadActions
            editionId={editionId}
            releaseId={release.release_id}
            pdfAvailable={release.pdf_available}
          />
          {release.pdf_available ? (
            <ReleaseLineageStatus
              editionId={editionId}
              releaseId={release.release_id}
            />
          ) : null}
          {release.render_id ? (
            <RenderInspectionLinks renderId={release.render_id} />
          ) : null}
        </>
      ) : null}
    </section>
  );
}

export function PublicationConsole({
  editionId,
  readOnly = false,
}: {
  editionId: string;
  readOnly?: boolean;
}) {
  const queryClient = useQueryClient();
  const release = useQuery({
    queryKey: ["edition-release", editionId],
    queryFn: () => getEditionRelease(editionId),
    refetchInterval: (query) => publicationPollingInterval(query.state.data),
  });
  const composition = useQuery({
    queryKey: ["edition-composition", editionId],
    queryFn: () => getEditionComposition(editionId),
    enabled: release.data?.can_retry_assembly ?? false,
  });
  const retryAssembly = useMutation({
    mutationFn: () => {
      const version = composition.data?.composition?.version;
      if (version === undefined || version === null) {
        throw new Error("La composition courante est indisponible.");
      }
      return acceptEditionComposition(editionId, version);
    },
    retry: false,
    onSuccess: () => invalidatePublication(queryClient, editionId),
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        void queryClient.invalidateQueries({
          queryKey: ["edition-composition", editionId],
        });
      }
    },
  });
  const retryRender = useMutation({
    mutationFn: () => retryEditionRender(editionId),
    retry: false,
    onSuccess: () => invalidatePublication(queryClient, editionId),
  });

  if (release.isPending) {
    return <p role="status">Chargement de la publication…</p>;
  }
  if (release.isError && (!readOnly || !isMissingRelease(release.error))) {
    return (
      <p className="error-message" role="alert">
        La publication est inaccessible : {String(release.error)}
      </p>
    );
  }
  if (readOnly) {
    return (
      <ArchivedPublication
        editionId={editionId}
        release={release.data ?? null}
      />
    );
  }
  if (!release.data) return null;

  const current = release.data;
  if (current.release_id) {
    return (
      <section
        className="workflow-placeholder publication-console"
        aria-live="polite"
      >
        <p className="eyebrow">Publication</p>
        <h2>Assemblage canonique</h2>
        <p>Release assemblé</p>
        <h2>Rendu PDF</h2>
        <RenderStatus release={current} />
        {!readOnly && current.can_retry_render && !current.pdf_available ? (
          <button
            className="button"
            type="button"
            disabled={retryRender.isPending}
            onClick={() => retryRender.mutate()}
          >
            {retryRender.isPending ? "Relancement…" : "Relancer le rendu"}
          </button>
        ) : null}
        {!readOnly && retryRender.error ? (
          <p className="error-message" role="alert">
            {retryRender.error instanceof Error
              ? retryRender.error.message
              : "Le rendu PDF n’a pas pu être relancé."}
          </p>
        ) : null}
        <DownloadActions
          editionId={editionId}
          releaseId={current.release_id}
          pdfAvailable={current.pdf_available}
        />
        {current.pdf_available ? (
          <ReleaseLineageStatus
            editionId={editionId}
            releaseId={current.release_id}
          />
        ) : null}
        {current.render_id ? (
          <RenderInspectionLinks renderId={current.render_id} />
        ) : null}
      </section>
    );
  }

  const failed = current.assembly_status === "failed";
  const statusLabel =
    current.assembly_status === null
      ? "L'assemblage n'a pas pu être démarré."
      : current.assembly_status === "running"
        ? "Assemblage en cours"
        : current.assembly_status === "queued"
          ? "Assemblage en cours"
          : current.assembly_status === "waiting_human"
            ? "Intervention requise pour poursuivre l'assemblage."
            : current.assembly_status === "cancelled"
              ? "L'assemblage a été annulé."
              : current.assembly_status === "succeeded"
                ? "Assemblage terminé"
                : "L'assemblage a échoué.";

  return (
    <section
      className="workflow-placeholder publication-console"
      aria-live="polite"
    >
      <p className="eyebrow">Publication</p>
      <h2>Assemblage canonique</h2>
      <p>Manifest figé</p>
      <p role="status">{failed ? "L'assemblage a échoué." : statusLabel}</p>
      {failed && current.assembly_error_message ? (
        <p className="error-message">{current.assembly_error_message}</p>
      ) : null}
      {!readOnly && current.can_retry_assembly ? (
        <button
          className="button"
          type="button"
          disabled={
            retryAssembly.isPending ||
            composition.isPending ||
            !composition.data?.composition
          }
          onClick={() => retryAssembly.mutate()}
        >
          {retryAssembly.isPending ? "Relancement…" : "Relancer l'assemblage"}
        </button>
      ) : null}
      {!readOnly && retryAssembly.error ? (
        <p className="error-message" role="alert">
          {retryAssembly.error instanceof Error
            ? retryAssembly.error.message
            : "L’assemblage n’a pas pu être relancé."}
        </p>
      ) : null}
      {failed && current.assembly_error_code ? (
        <details className="publication-console__diagnostics">
          <summary>Diagnostics</summary>
          <p>{current.assembly_error_code}</p>
        </details>
      ) : null}
    </section>
  );
}
