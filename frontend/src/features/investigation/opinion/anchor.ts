import type { NoteTargetBlockType } from "../api/opinionProposals";
import type { AnalystNoteV1 } from "./types";

const LEAD_ANCHOR = /^lead:[0-9]{4}$/;
const SECTION_ANCHOR = /^section:[0-9]+:paragraph:[0-9]{4}$/;

/** The only two anchor spaces accepted by the note-proposal service. */
export function classifyAnchor(anchor: string): NoteTargetBlockType | null {
  if (LEAD_ANCHOR.test(anchor)) return "lead";
  if (SECTION_ANCHOR.test(anchor)) return "section";
  return null;
}

export const ANCHOR_EXAMPLES =
  "lead:0001 ou section:2:paragraph:0007 (identifiants d’ancre de l’article)";

export interface NoteAnchorGroup {
  anchor: string;
  notes: AnalystNoteV1[];
}

export function groupNotesByAnchor(notes: AnalystNoteV1[]): NoteAnchorGroup[] {
  const groups = new Map<string, AnalystNoteV1[]>();
  for (const note of notes) {
    const bucket = groups.get(note.anchor);
    if (bucket === undefined) {
      groups.set(note.anchor, [note]);
    } else {
      bucket.push(note);
    }
  }
  return [...groups.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([anchor, bucket]) => ({ anchor, notes: bucket }));
}

export function notesWithoutBridgeProvenance(
  notes: AnalystNoteV1[],
): AnalystNoteV1[] {
  return notes.filter(
    (note) => note.bridge_authored !== true || note.bridge_draft_ref == null,
  );
}
