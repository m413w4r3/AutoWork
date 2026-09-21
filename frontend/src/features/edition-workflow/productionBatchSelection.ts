import type { SelectionItem } from "../../api/selection";

/** A selected board item with a resolved subject identity. */
export type EligibleSubject = Pick<SelectionItem, "title" | "subject_id"> & {
  subject_id: string;
};

export function isEligibleSubject(
  item: SelectionItem,
): item is SelectionItem & EligibleSubject {
  return item.state === "selected" && item.subject_id !== null;
}

/**
 * Build the `subject_ids` payload in current selection board order, not in
 * click order or Set insertion order — the sequential batch must preserve
 * the board order regardless of the order the operator checked boxes.
 */
export function orderedSelection(
  eligibleSubjects: readonly EligibleSubject[],
  selected: ReadonlySet<string>,
): string[] {
  return eligibleSubjects
    .map((subject) => subject.subject_id)
    .filter((subjectId) => selected.has(subjectId));
}

/**
 * Drop any selected id that is no longer part of the current eligible set —
 * called whenever TanStack Query hands back a fresh board so a subject that
 * became ineligible (or disappeared) is never sent to production. Never adds
 * ids: only removal happens here, so a board refresh can't silently re-arm a
 * subject the operator hadn't checked.
 */
export function pruneToEligible(
  selected: ReadonlySet<string>,
  eligibleIds: ReadonlySet<string>,
): Set<string> {
  const next = new Set<string>();
  for (const id of selected) {
    if (eligibleIds.has(id)) next.add(id);
  }
  return next;
}
