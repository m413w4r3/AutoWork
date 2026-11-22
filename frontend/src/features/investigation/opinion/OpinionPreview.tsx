import { analystOpinionPreviewPdfUrl } from "../../../api/analystOpinion";
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
  const previewUrl = analystOpinionPreviewPdfUrl(
    opinion,
    opinion.provenance.base_publication_sha256,
  );

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

      <div>
        <h4>Aperçu PDF réel</h4>
        <p>
          <a href={previewUrl} target="_blank" rel="noreferrer">
            Ouvrir l’aperçu PDF réel de l’avis v{opinion.version}
          </a>
        </p>
        <iframe
          title="Aperçu PDF réel de l’avis"
          src={previewUrl}
          width="100%"
          height="720"
        >
          Votre navigateur ne peut pas afficher l’aperçu PDF.
        </iframe>
      </div>
    </section>
  );
}
