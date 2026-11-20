const threadPrefix = (subjectId: string, occurrenceId: string) =>
  ["editorial-thread", subjectId, occurrenceId] as const;
const proposalPrefix = (subjectId: string, occurrenceId: string) =>
  ["editorial-proposal", subjectId, occurrenceId] as const;

export const editorialWorkspaceQueryKeys = {
  snapshot: (subjectId: string) => ["editorial-workspace", subjectId] as const,
  threadsForOccurrence: threadPrefix,
  thread: (subjectId: string, occurrenceId: string, snapshotId: string) =>
    [...threadPrefix(subjectId, occurrenceId), snapshotId] as const,
  proposalsForOccurrence: proposalPrefix,
  proposal: (subjectId: string, occurrenceId: string, proposalId: string) =>
    [...proposalPrefix(subjectId, occurrenceId), proposalId] as const,
};
