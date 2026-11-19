export const editorialWorkspaceQueryKeys = {
  snapshot: (subjectId: string) => ["editorial-workspace", subjectId] as const,
  thread: (subjectId: string, occurrenceId: string, snapshotId: string) =>
    ["editorial-thread", subjectId, occurrenceId, snapshotId] as const,
  proposal: (subjectId: string, occurrenceId: string, proposalId: string) =>
    ["editorial-proposal", subjectId, occurrenceId, proposalId] as const,
};
