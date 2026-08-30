import type { EditorialGroup } from "../../api/editorial";

/** An editorial group that is eligible for the production batch: it carries
 * a resolved subject. `EditorialGroup.status === "selected"` is the
 * editorial-eligibility notion; it is distinct from whether the operator has
 * checked this subject for the *next* batch. */
export type EligibleGroup = EditorialGroup & { subject_id: string };

export function isEligibleSubject(
  group: EditorialGroup,
): group is EligibleGroup {
  return group.status === "selected" && group.subject_id !== null;
}

/**
 * Build the `subject_ids` payload in current editorial board order, not in
 * click order or Set insertion order — the sequential batch must preserve
 * the editorial order regardless of the order the operator checked boxes.
 */
export function orderedSelection(
  eligibleGroups: readonly EligibleGroup[],
  selected: ReadonlySet<string>,
): string[] {
  return eligibleGroups
    .map((group) => group.subject_id)
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
