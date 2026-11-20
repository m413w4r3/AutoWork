import { useCallback, useMemo, useState } from "react";

import type { EditorialWorkspaceSnapshot } from "../../api/editorialWorkspace";

export type EditorialLocalDrafts = Readonly<Record<string, string>>;

type EditorialDraftBase = Pick<
  EditorialWorkspaceSnapshot,
  | "subject_id"
  | "production_run_id"
  | "synthesis_artifact_id"
  | "synthesis_version"
  | "enrichment_artifact_id"
  | "enrichment_version"
  | "publication_artifact_id"
  | "publication_version"
  | "synthesis_canonical_sha256"
>;

interface StoredEditorialDraft {
  text: string;
  baseIdentity: string;
}

export interface EditorialDraftController {
  drafts: EditorialLocalDrafts;
  staleDraftIds: ReadonlySet<string>;
  hasDrafts: boolean;
  hasStaleDrafts: boolean;
  updateDraft: (blockId: string, value: string, canonicalText: string) => void;
  clearDraft: (blockId: string) => void;
}

function baseIdentity(base: EditorialDraftBase | undefined): string | null {
  if (!base) return null;
  return JSON.stringify([
    base.subject_id,
    base.production_run_id,
    base.synthesis_artifact_id,
    base.synthesis_version,
    base.enrichment_artifact_id,
    base.enrichment_version,
    base.publication_artifact_id,
    base.publication_version,
    base.synthesis_canonical_sha256,
  ]);
}

export function useEditorialDrafts(
  snapshot: EditorialWorkspaceSnapshot | undefined,
): EditorialDraftController {
  const currentBaseIdentity = baseIdentity(snapshot);
  const [storedDrafts, setStoredDrafts] = useState<
    Record<string, StoredEditorialDraft>
  >({});

  const updateDraft = useCallback(
    (blockId: string, value: string, canonicalText: string) => {
      if (currentBaseIdentity === null) return;
      setStoredDrafts((current) => {
        const existing = current[blockId];
        if (
          value === canonicalText &&
          (!existing || existing.baseIdentity === currentBaseIdentity)
        ) {
          if (!existing) return current;
          const next = { ...current };
          delete next[blockId];
          return next;
        }
        return {
          ...current,
          [blockId]: {
            text: value,
            baseIdentity: existing?.baseIdentity ?? currentBaseIdentity,
          },
        };
      });
    },
    [currentBaseIdentity],
  );

  const clearDraft = useCallback((blockId: string) => {
    setStoredDrafts((current) => {
      if (!Object.prototype.hasOwnProperty.call(current, blockId)) {
        return current;
      }
      const next = { ...current };
      delete next[blockId];
      return next;
    });
  }, []);

  const drafts = useMemo(() => {
    const next: Record<string, string> = {};
    for (const [blockId, draft] of Object.entries(storedDrafts)) {
      next[blockId] = draft.text;
    }
    return next;
  }, [storedDrafts]);

  const staleDraftIds = useMemo(
    () =>
      new Set(
        Object.entries(storedDrafts)
          .filter(([, draft]) => draft.baseIdentity !== currentBaseIdentity)
          .map(([blockId]) => blockId),
      ),
    [currentBaseIdentity, storedDrafts],
  );
  const hasDrafts = Object.keys(storedDrafts).length > 0;

  return {
    drafts,
    staleDraftIds,
    hasDrafts,
    hasStaleDrafts: staleDraftIds.size > 0,
    updateDraft,
    clearDraft,
  };
}
