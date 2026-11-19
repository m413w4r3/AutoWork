export const editorialWorkspaceQueryKeys = {
  snapshot: (subjectId: string) => ["editorial-workspace", subjectId] as const,
  thread: (subjectId: string, occurrenceId: string, snapshotId: string) =>
    ["editorial-thread", subjectId, occurrenceId, snapshotId] as const,
};
