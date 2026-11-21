const threadPrefix = (subjectId: string, occurrenceId: string) =>
  ["editorial-thread", subjectId, occurrenceId] as const;
const proposalPrefix = (subjectId: string, occurrenceId: string) =>
  ["editorial-proposal", subjectId, occurrenceId] as const;

export const editorialWorkspaceQueryKeys = {
  snapshot: (subjectId: string) => ["editorial-workspace", subjectId] as const,
  pdfStatus: (subjectId: string, snapshotId: string) =>
    ["editorial-pdf-status", subjectId, snapshotId] as const,
  compositionStatus: (subjectId: string, snapshotId: string) =>
    ["editorial-composition-status", subjectId, snapshotId] as const,
  history: (subjectId: string) => ["editorial-history", subjectId] as const,
  threadsForOccurrence: threadPrefix,
  thread: (subjectId: string, occurrenceId: string, snapshotId: string) =>
    [...threadPrefix(subjectId, occurrenceId), snapshotId] as const,
  proposalsForOccurrence: proposalPrefix,
  proposal: (subjectId: string, occurrenceId: string, proposalId: string) =>
    [...proposalPrefix(subjectId, occurrenceId), proposalId] as const,
  serverDraft: (subjectId: string, occurrenceId: string) =>
    ["editorial-server-draft", subjectId, occurrenceId] as const,
  figureCatalog: (
    subjectId: string,
    status: string,
    source: string,
    search: string,
  ) => ["editorial-figure-catalog", subjectId, status, source, search] as const,
  figurePreview: (subjectId: string, figureId: string) =>
    ["editorial-figure-preview", subjectId, figureId] as const,
};
