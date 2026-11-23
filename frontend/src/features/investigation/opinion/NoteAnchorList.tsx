import type {
  InvestigationFinding,
  InvestigationObservation,
} from "../../../api/investigation";
import type {
  ArticleAnchorViewV1,
  ArticleSectionViewV1,
} from "../api/articleAnchors";
import type { NoteAnchorGroup } from "./anchor";
import {
  BLOCK_LABELS,
  EVIDENCE_KIND_LABELS,
  FINDING_KIND_LABELS,
  NOTE_RELATION_LABELS,
  NOTE_STATE_LABELS,
  truncate,
} from "./labels";
import type { AnalystNoteV1 } from "./types";

function domId(anchor: string): string {
  return anchor.replace(/[^a-zA-Z0-9_-]/g, "-");
}

function articleAnchorLocationLabel(
  anchor: string,
  anchors: ArticleAnchorViewV1[],
  sections: ArticleSectionViewV1[],
): string {
  const item = anchors.find((candidate) => candidate.anchor === anchor);
  if (item === undefined) return "Emplacement de l’article non disponible";
  const paragraph = (item.paragraph_index ?? 0) + 1;
  if (item.section_index === null) return `Chapô, paragraphe ${paragraph}`;
  const heading = sections.find(
    (section) => section.section_index === item.section_index,
  )?.heading;
  return `Section ${item.section_index + 1}${heading ? ` · ${heading}` : ""}, paragraphe ${paragraph}`;
}

export function NoteAnchorList({
  groups,
  selectedAnchor,
  onSelectNote,
  anchors,
  sections,
  findings,
  observations,
}: {
  groups: NoteAnchorGroup[];
  selectedAnchor: string;
  onSelectNote: (note: AnalystNoteV1) => void;
  anchors: ArticleAnchorViewV1[];
  sections: ArticleSectionViewV1[];
  findings: InvestigationFinding[];
  observations: InvestigationObservation[];
}) {
  return (
    <section className="job-card" aria-labelledby="opinion-notes-list-heading">
      <h3 id="opinion-notes-list-heading">Notes par ancre</h3>
      <p>
        Plusieurs blocs « Note de l’analyste » peuvent viser des endroits
        différents de l’article. Chaque acceptation ne remplace que la note de
        son ancre.
      </p>
      {groups.length === 0 ? (
        <p>Aucune note n’est encore ancrée dans cet avis.</p>
      ) : (
        <div role="list">
          {groups.map((group) => (
            <section
              key={group.anchor}
              role="listitem"
              aria-labelledby={`opinion-note-anchor-${domId(group.anchor)}`}
            >
              <h4 id={`opinion-note-anchor-${domId(group.anchor)}`}>
                {articleAnchorLocationLabel(group.anchor, anchors, sections)} ·{" "}
                {group.notes.length === 1
                  ? "1 note"
                  : `${group.notes.length} notes`}
              </h4>
              <ul>
                {group.notes.map((note) => {
                  const bridgeAuthored = note.bridge_authored === true;
                  return (
                    <li key={note.id}>
                      <button
                        type="button"
                        className="button button--secondary"
                        aria-pressed={selectedAnchor === note.anchor}
                        onClick={() => onSelectNote(note)}
                      >
                        Examiner la note
                      </button>
                      <p>{note.text}</p>
                      <ul aria-label="Qualification et provenance de la note">
                        <li>
                          État : {NOTE_STATE_LABELS[note.state]} · relation :{" "}
                          {NOTE_RELATION_LABELS[note.relation]}
                        </li>
                        <li>
                          {bridgeAuthored
                            ? "Rédigée par Bridge"
                            : "Sans provenance Bridge"}
                          {note.bridge_block_type
                            ? ` · bloc ${BLOCK_LABELS[note.bridge_block_type]}`
                            : ""}
                        </li>
                        {note.bridge_modified_by_operator === true ? (
                          <li className="verification-warning">
                            Prose Bridge modifiée par un opérateur : une
                            nouvelle proposition Bridge est requise.
                          </li>
                        ) : null}
                        <li>
                          Findings cités :{" "}
                          {note.finding_refs.length === 0
                            ? "aucun"
                            : note.finding_refs
                                .map((id) => {
                                  const finding = findings.find(
                                    (item) => item.id === id,
                                  );
                                  return finding === undefined
                                    ? "Finding indisponible"
                                    : `${FINDING_KIND_LABELS[finding.kind] ?? finding.kind} · ${truncate(finding.statement)}`;
                                })
                                .join(" ; ")}
                        </li>
                        <li>
                          Observations citées :{" "}
                          {note.evidence_refs.length === 0
                            ? "aucune"
                            : note.evidence_refs
                                .map((id) => {
                                  const observation = observations.find(
                                    (item) => item.id === id,
                                  );
                                  return observation === undefined
                                    ? "Observation indisponible"
                                    : `${EVIDENCE_KIND_LABELS[observation.kind] ?? observation.kind} · ${observation.source} · ${observation.object_type} ${observation.object_id}`;
                                })
                                .join(" ; ")}
                        </li>
                      </ul>
                      <details>
                        <summary>Provenance technique</summary>
                        <p>
                          Ancre <code>{note.anchor}</code> · note{" "}
                          <code>{note.id}</code>
                        </p>
                        <p>
                          Findings cités :{" "}
                          {note.finding_refs.length === 0
                            ? "aucun"
                            : note.finding_refs.map((id) => (
                                <code key={id}>{id} </code>
                              ))}
                        </p>
                        <p>
                          Observations citées :{" "}
                          {note.evidence_refs.length === 0
                            ? "aucune"
                            : note.evidence_refs.map((id) => (
                                <code key={id}>{id} </code>
                              ))}
                        </p>
                        {note.bridge_draft_ref ? (
                          <p>
                            Invocation Bridge{" "}
                            <code>{note.bridge_draft_ref.invocation_id}</code> ·
                            texte SHA-256{" "}
                            <code>{note.bridge_draft_ref.text_sha256}</code>
                          </p>
                        ) : null}
                      </details>
                    </li>
                  );
                })}
              </ul>
            </section>
          ))}
        </div>
      )}
    </section>
  );
}
