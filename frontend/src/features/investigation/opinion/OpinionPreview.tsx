import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import {
  AnalystOpinionPreviewError,
  fetchAnalystOpinionPreviewPdf,
} from "../../../api/analystOpinion";
import type {
  InvestigationFinding,
  InvestigationObservation,
} from "../../../api/investigation";
import { groupNotesByAnchor } from "./anchor";
import {
  BLOCK_LABELS,
  BLOCK_ORDER,
  NOTE_RELATION_LABELS,
  NOTE_STATE_LABELS,
  OPINION_STATUS_LABELS,
  PROTOCOL_FIGURE_LABELS,
  QUALIFICATION_LABELS,
  shortHash,
} from "./labels";
import { ReferenceList } from "./ReferenceList";
import { findingLabel, observationLabel } from "./references";
import type { OpinionVersionV1 } from "./types";

const STALE_PREVIEW_CODES = new Set([
  "stale_version",
  "version_hash_mismatch",
  "stale_base",
  "base_identity_mismatch",
]);

const PREVIEW_STATUS_LABELS = {
  generating: "Génération en cours",
  ready: "PDF prêt",
  stale: "Obsolète",
  failed: "Échec de la génération",
} as const;

function pdfSizeLabel(size: number): string {
  if (size < 1024) return `${size} octets`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} Ko`;
  return `${(size / (1024 * 1024)).toFixed(1)} Mo`;
}

function isStalePreviewError(error: unknown): boolean {
  return (
    error instanceof AnalystOpinionPreviewError &&
    error.code !== null &&
    STALE_PREVIEW_CODES.has(error.code)
  );
}

export function OpinionPreview({
  opinion,
  findings,
  observations,
}: {
  opinion: OpinionVersionV1;
  findings: InvestigationFinding[];
  observations: InvestigationObservation[];
}) {
  const notes = opinion.observations ?? [];
  const noteGroups = groupNotesByAnchor(notes);
  const basePublicationSha256 = opinion.provenance.base_publication_sha256;
  const previewQuery = useQuery({
    queryKey: [
      "analyst-opinion-preview-pdf",
      opinion.id,
      opinion.version,
      opinion.content_sha256,
      basePublicationSha256,
    ],
    queryFn: ({ signal }) =>
      fetchAnalystOpinionPreviewPdf(opinion, basePublicationSha256, signal),
    placeholderData: (previous) => previous,
    retry: false,
    staleTime: Infinity,
  });
  const fetchedPdf = previewQuery.data;
  const pdfIsOutOfDate =
    fetchedPdf !== undefined &&
    (fetchedPdf.opinionId !== opinion.id ||
      fetchedPdf.opinionVersion !== opinion.version ||
      fetchedPdf.opinionSha256 !== opinion.content_sha256 ||
      fetchedPdf.basePublicationSha256 !== basePublicationSha256);
  const staleError = isStalePreviewError(previewQuery.error);
  const status =
    pdfIsOutOfDate || staleError
      ? "stale"
      : previewQuery.isError
        ? "failed"
        : previewQuery.isPending || fetchedPdf === undefined
          ? "generating"
          : "ready";
  const currentPdf = status === "ready" ? fetchedPdf : undefined;
  const [objectUrl, setObjectUrl] = useState<string | null>(null);

  useEffect(() => {
    if (currentPdf === undefined) {
      setObjectUrl(null);
      return;
    }
    const nextUrl = URL.createObjectURL(currentPdf.blob);
    setObjectUrl(nextUrl);
    return () => URL.revokeObjectURL(nextUrl);
  }, [currentPdf]);

  return (
    <section className="job-card" aria-labelledby="opinion-render-heading">
      <h3 id="opinion-render-heading">Aperçu du rendu de l’avis courant</h3>
      <p>
        Version {opinion.version} · {OPINION_STATUS_LABELS[opinion.status]} ·
        SHA-256 <code>{opinion.content_sha256}</code>. L’aperçu PDF réel est
        rendu par le serveur avec les tableaux, graphiques, figures, légendes et
        placements de l’article.
      </p>

      <div>
        <h4>Blocs éditoriaux</h4>
        {BLOCK_ORDER.map((type) => {
          const block = opinion.blocks.find(
            (item) => item.type === type && item.text.trim() !== "",
          );
          if (block === undefined) return null;
          return (
            <section key={type} aria-labelledby={`opinion-preview-${type}`}>
              <h5 id={`opinion-preview-${type}`}>{BLOCK_LABELS[type]}</h5>
              <p>{block.text}</p>
              <p>
                Qualification :{" "}
                {QUALIFICATION_LABELS[block.qualification] ??
                  block.qualification}{" "}
                · confiance : {Math.round(block.certainty * 100)} %
              </p>
              <ReferenceList
                title="Findings cités"
                ids={block.finding_refs}
                resolve={(id) => findingLabel(findings, id)}
              />
              <ReferenceList
                title="Observations citées"
                ids={block.evidence_refs}
                resolve={(id) => observationLabel(observations, id)}
              />
            </section>
          );
        })}
      </div>

      <div>
        <h4>Notes de l’analyste par ancre</h4>
        {noteGroups.length === 0 ? (
          <p>Aucune note ancrée dans cette version.</p>
        ) : (
          noteGroups.map((group) => (
            <section
              key={group.anchor}
              aria-label={`Notes à l’ancre ${group.anchor}`}
            >
              <h5>
                Ancre <code>{group.anchor}</code>
              </h5>
              {group.notes.map((note) => (
                <article key={note.id}>
                  <p>{note.text}</p>
                  <p>
                    État : {NOTE_STATE_LABELS[note.state]} · relation :{" "}
                    {NOTE_RELATION_LABELS[note.relation]} ·{" "}
                    {note.bridge_authored === true
                      ? `rédigée par Bridge (invocation ${
                          note.bridge_draft_ref?.invocation_id ?? "inconnue"
                        })`
                      : "sans provenance Bridge"}
                    {note.bridge_modified_by_operator === true
                      ? " · prose Bridge modifiée"
                      : ""}
                  </p>
                  <ReferenceList
                    title="Findings cités"
                    ids={note.finding_refs}
                    resolve={(id) => findingLabel(findings, id)}
                  />
                  <ReferenceList
                    title="Observations citées"
                    ids={note.evidence_refs}
                    resolve={(id) => observationLabel(observations, id)}
                  />
                </article>
              ))}
            </section>
          ))
        )}
      </div>

      {opinion.protocol_figures !== undefined &&
      opinion.protocol_figures.length > 0 ? (
        <div>
          <h4>Figures, légendes et placement</h4>
          {opinion.protocol_figures.map((figure) => (
            <figure key={`${figure.kind}-${figure.asset_sha256}`}>
              <figcaption>{figure.caption}</figcaption>
              <p>
                {PROTOCOL_FIGURE_LABELS[figure.kind] ?? figure.kind} · asset{" "}
                <code>{shortHash(figure.asset_sha256)}</code> · compilé par{" "}
                {figure.compiler_name} {figure.compiler_version}
              </p>
            </figure>
          ))}
        </div>
      ) : null}

      {opinion.implant_materials_section !== null &&
      opinion.implant_materials_section !== undefined ? (
        <div>
          <h4>Matériel relatif à l’implant</h4>
          <p>{opinion.implant_materials_section.text}</p>
          <p>
            {opinion.implant_materials_section.bridge_authored
              ? "Section rédigée par Bridge"
              : "Section sans provenance Bridge"}
          </p>
        </div>
      ) : null}

      {opinion.unresolved_points.length > 0 ? (
        <div>
          <h4>Points non résolus</h4>
          <ul>
            {opinion.unresolved_points.map((point) => (
              <li key={point}>{point}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <section
        className="opinion-pdf-preview"
        aria-labelledby="opinion-pdf-preview-heading"
      >
        <div className="opinion-pdf-preview__header">
          <h4 id="opinion-pdf-preview-heading">Aperçu PDF réel</h4>
          <span
            className={`opinion-pdf-preview__status is-${status}`}
            role="status"
            aria-live="polite"
          >
            {PREVIEW_STATUS_LABELS[status]}
          </span>
        </div>
        <dl className="opinion-pdf-preview__metadata">
          <div>
            <dt>Version de l’avis</dt>
            <dd>v{opinion.version}</dd>
          </div>
          <div>
            <dt>Disponibilité</dt>
            <dd>
              {currentPdf === undefined
                ? status === "generating"
                  ? "PDF en préparation"
                  : "PDF indisponible"
                : `PDF disponible · ${pdfSizeLabel(currentPdf.blob.size)}`}
            </dd>
          </div>
        </dl>

        {status === "generating" ? (
          <p className="opinion-pdf-preview__loading" aria-busy="true">
            Chargement de l’aperçu… Le serveur génère le PDF.
          </p>
        ) : null}

        {status === "stale" ? (
          <p className="opinion-pdf-preview__warning" role="alert">
            {pdfIsOutOfDate
              ? `Le PDF chargé correspond à l’avis v${fetchedPdf?.opinionVersion} et à une autre version de l’avis ou de sa base de publication. Il est obsolète et n’est pas affiché.`
              : "L’avis ou sa base de publication a changé depuis cette demande. Actualisez l’avis avant de générer un nouvel aperçu."}
          </p>
        ) : null}

        {status === "failed" ? (
          <div className="opinion-pdf-preview__failure">
            <p className="error-message" role="alert">
              Impossible de charger l’aperçu PDF : {String(previewQuery.error)}
            </p>
            <button type="button" onClick={() => void previewQuery.refetch()}>
              Réessayer
            </button>
          </div>
        ) : null}

        {currentPdf !== undefined && objectUrl === null ? (
          <p className="opinion-pdf-preview__loading">
            Préparation du lecteur PDF…
          </p>
        ) : null}

        {currentPdf !== undefined && objectUrl !== null ? (
          <>
            <p className="opinion-pdf-preview__actions">
              <a href={objectUrl} target="_blank" rel="noreferrer">
                Ouvrir le PDF
              </a>
              <a href={objectUrl} download={`avis-v${opinion.version}.pdf`}>
                Télécharger
              </a>
            </p>
            <object
              aria-label={`PDF de l’avis version ${opinion.version}`}
              className="opinion-pdf-preview__viewer"
              data={objectUrl}
              data-testid="opinion-pdf-viewer"
              type="application/pdf"
            >
              <p>
                Le lecteur PDF de ce navigateur ne peut pas afficher ce
                document. <a href={objectUrl}>Ouvrir le PDF</a>
              </p>
            </object>
          </>
        ) : null}
      </section>
    </section>
  );
}
