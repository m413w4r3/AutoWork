import type { NoteAnchorGroup } from "./anchor";
import {
  BLOCK_LABELS,
  NOTE_RELATION_LABELS,
  NOTE_STATE_LABELS,
  shortHash,
} from "./labels";
import type { AnalystNoteV1 } from "./types";

function domId(anchor: string): string {
  return anchor.replace(/[^a-zA-Z0-9_-]/g, "-");
}

export function NoteAnchorList({
  groups,
  selectedAnchor,
  onSelectNote,
}: {
  groups: NoteAnchorGroup[];
  selectedAnchor: string;
  onSelectNote: (note: AnalystNoteV1) => void;
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
                Ancre <code>{group.anchor}</code> ·{" "}
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
                        Examiner la note {shortHash(note.id)}
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
                        {note.bridge_draft_ref ? (
                          <li>
                            Invocation Bridge{" "}
                            <code>{note.bridge_draft_ref.invocation_id}</code> ·
                            texte SHA-256{" "}
                            <code>
                              {shortHash(note.bridge_draft_ref.text_sha256)}
                            </code>
                          </li>
                        ) : null}
                        <li>
                          Findings cités :{" "}
                          {note.finding_refs.length === 0
                            ? "aucun"
                            : note.finding_refs.join(", ")}
                        </li>
                        <li>
                          Observations citées :{" "}
                          {note.evidence_refs.length === 0
                            ? "aucune"
                            : note.evidence_refs.join(", ")}
                        </li>
                      </ul>
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
