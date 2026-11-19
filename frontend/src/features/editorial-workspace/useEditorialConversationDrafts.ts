import { useCallback, useState } from "react";

export function useEditorialConversationDrafts(occurrenceId: string) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  const setDraft = useCallback(
    (value: string) => {
      setDrafts((current) => ({ ...current, [occurrenceId]: value }));
    },
    [occurrenceId],
  );

  const clearDraft = useCallback((targetOccurrenceId: string) => {
    setDrafts((current) => {
      const next = { ...current };
      delete next[targetOccurrenceId];
      return next;
    });
  }, []);

  return { draft: drafts[occurrenceId] ?? "", setDraft, clearDraft };
}
