import { useMemo } from "react";

import {
  useArticleAnchors,
  type ArticleAnchorViewV1,
} from "../api/articleAnchors";
import type { NoteProposalViewV1 } from "../api/opinionProposals";
import { classifyAnchor } from "./anchor";
import { describeApiError } from "./errors";
import { TARGET_BLOCK_LABELS, shortHash, truncate } from "./labels";
import type { AnalystNoteV1 } from "./types";

/** Option labels keep a readable prefix; the detail below carries more text. */
const OPTION_TEXT_MAX = 120;
const DETAIL_TEXT_MAX = 800;

export interface AnchorGroupV1 {
  key: string;
  label: string;
  anchors: ArticleAnchorViewV1[];
}

export interface AnchorOccurrenceV1 {
  index: number;
  total: number;
}

/**
 * Group the canonical anchors the way the reader renders them: the lead
 * paragraphs first, then each section by its numeric index. Identical
 * paragraphs keep their distinct anchors (V9/V10 render duplicates).
 */
export function groupAnchors(anchors: ArticleAnchorViewV1[]): AnchorGroupV1[] {
  const lead: ArticleAnchorViewV1[] = [];
  const sections = new Map<number, ArticleAnchorViewV1[]>();
  for (const item of anchors) {
    if (item.anchor.startsWith("lead:")) {
      lead.push(item);
      continue;
    }
    const index = item.section_index ?? 0;
    const bucket = sections.get(index);
    if (bucket === undefined) {
      sections.set(index, [item]);
    } else {
      bucket.push(item);
    }
  }
  const groups: AnchorGroupV1[] = [];
  if (lead.length > 0) {
    groups.push({
      key: "lead",
      label: "Chapô de l’article",
      anchors: [...lead].sort(
        (left, right) =>
          (left.paragraph_index ?? 0) - (right.paragraph_index ?? 0),
      ),
    });
  }
  for (const [index, items] of [...sections.entries()].sort(
    ([left], [right]) => left - right,
  )) {
    groups.push({
      key: `section:${index}`,
      label: `Section ${index}`,
      anchors: [...items].sort(
        (left, right) =>
          (left.paragraph_index ?? 0) - (right.paragraph_index ?? 0),
      ),
    });
  }
  return groups;
}

/**
 * 1-based occurrence of each paragraph among anchors that share its exact
 * text. Anchors with a unique text are absent from the map.
 */
export function anchorOccurrences(
  anchors: ArticleAnchorViewV1[],
): Map<string, AnchorOccurrenceV1> {
  const byText = new Map<string, ArticleAnchorViewV1[]>();
  for (const item of anchors) {
    const bucket = byText.get(item.text_sha256);
    if (bucket === undefined) {
      byText.set(item.text_sha256, [item]);
    } else {
      bucket.push(item);
    }
  }
  const occurrences = new Map<string, AnchorOccurrenceV1>();
  for (const bucket of byText.values()) {
    if (bucket.length < 2) continue;
    bucket.forEach((item, index) => {
      occurrences.set(item.anchor, { index: index + 1, total: bucket.length });
    });
  }
  return occurrences;
}

function optionLabel(
  item: ArticleAnchorViewV1,
  occurrences: Map<string, AnchorOccurrenceV1>,
  noteAnchors: Set<string>,
): string {
  const markers: string[] = [];
  const occurrence = occurrences.get(item.anchor);
  if (occurrence !== undefined) {
    markers.push(`occurrence ${occurrence.index} sur ${occurrence.total}`);
  }
  if (noteAnchors.has(item.anchor)) {
    markers.push("remplace la note existante");
  }
  const suffix = markers.length === 0 ? "" : ` · ${markers.join(" · ")}`;
  return `${item.anchor} — ${truncate(item.text, OPTION_TEXT_MAX)}${suffix}`;
}

/**
 * Visual selector of the canonical publication anchors. The server stays the
 * only authority: the catalog is a read, every drafting request pins its base
 * and paragraph hash server-side, and an anchor that moved or disappeared is
 * refused with a typed code. The catalog's element verdicts are surfaced
 * beside the selected anchor so an element the note-block validator would
 * refuse is never mistaken for an available block target. The free-text field
 * is a diagnostic escape hatch behind `<details>`, never a suggested path.
 */
export function AnchorPicker({
  investigationId,
  anchor,
  onAnchorChange,
  notes,
  proposal,
}: {
  investigationId: string;
  anchor: string;
  onAnchorChange: (anchor: string) => void;
  notes: AnalystNoteV1[];
  proposal: NoteProposalViewV1 | null;
}) {
  const query = useArticleAnchors(investigationId);
  const catalog = query.data ?? null;
  const groups = useMemo(
    () => (catalog === null ? [] : groupAnchors(catalog.anchors)),
    [catalog],
  );
  const occurrences = useMemo<Map<string, AnchorOccurrenceV1>>(
    () =>
      catalog === null
        ? new Map<string, AnchorOccurrenceV1>()
        : anchorOccurrences(catalog.anchors),
    [catalog],
  );
  const noteAnchors = useMemo(
    () => new Set(notes.map((note) => note.anchor)),
    [notes],
  );
  /**
   * M23 elements the catalog places at each anchor, split by the server's
   * note-block verdict: an anchor carrying only refused elements cannot
   * support a block reference, so the choice is visible before drafting.
   */
  const elementCounts = useMemo(() => {
    const counts = new Map<string, { proposable: number; refused: number }>();
    for (const element of catalog?.elements ?? []) {
      if (element.anchor === null) continue;
      const entry = counts.get(element.anchor) ?? {
        proposable: 0,
        refused: 0,
      };
      if (element.proposable) {
        entry.proposable += 1;
      } else {
        entry.refused += 1;
      }
      counts.set(element.anchor, entry);
    }
    return counts;
  }, [catalog]);

  const selected =
    catalog?.anchors.find((item) => item.anchor === anchor) ?? null;
  const occurrence =
    selected === null ? undefined : occurrences.get(selected.anchor);
  const selectedElements =
    selected === null ? undefined : elementCounts.get(selected.anchor);
  const targetType = classifyAnchor(anchor);
  const missingFromCatalog =
    catalog !== null &&
    anchor !== "" &&
    targetType !== null &&
    selected === null;
  const proposalBaseMoved =
    proposal !== null &&
    catalog !== null &&
    proposal.base_publication_artifact_id !== null &&
    (proposal.base_publication_artifact_id !==
      catalog.base_publication_artifact_id ||
      (proposal.base_publication_sha256 !== null &&
        proposal.base_publication_sha256 !== catalog.base_publication_sha256));

  function reloadAnchors() {
    void query.refetch();
  }

  return (
    <div className="anchor-picker">
      {query.isPending ? (
        <p role="status">Chargement des ancres canoniques…</p>
      ) : null}
      {query.isError ? (
        <div className="verification-warning" role="alert">
          <p>
            Ancres de publication indisponibles :{" "}
            {describeApiError(query.error).message} · code :{" "}
            {describeApiError(query.error).code}
          </p>
          <button type="button" onClick={reloadAnchors}>
            Recharger les ancres
          </button>
        </div>
      ) : null}

      {catalog === null ? null : (
        <>
          <p>
            Base de publication :{" "}
            {catalog.source === "pinned_opinion_base"
              ? "base épinglée de l’avis"
              : "base courante vérifiée"}{" "}
            · artefact <code>{catalog.base_publication_artifact_id}</code> ·
            version {catalog.base_publication_version} · SHA-256{" "}
            <code title={catalog.base_publication_sha256}>
              {shortHash(catalog.base_publication_sha256)}
            </code>
          </p>
          {catalog.truncated ? (
            <p className="verification-warning" role="note">
              Liste bornée : certaines ancres ne sont pas proposées ici. Le
              serveur reste l’autorité et peut accepter une ancre saisie en mode
              diagnostic.
            </p>
          ) : null}

          <label htmlFor="opinion-note-anchor">Ancre de publication</label>
          <select
            id="opinion-note-anchor"
            value={selected === null ? "" : anchor}
            onChange={(event) => onAnchorChange(event.currentTarget.value)}
            aria-describedby="opinion-note-anchor-detail"
          >
            <option value="">— Choisir une ancre de publication —</option>
            {groups.map((group) => (
              <optgroup key={group.key} label={group.label}>
                {group.anchors.map((item) => (
                  <option key={item.anchor} value={item.anchor}>
                    {optionLabel(item, occurrences, noteAnchors)}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>

          <div id="opinion-note-anchor-detail">
            {selected === null ? null : (
              <>
                <p title={selected.text}>
                  {truncate(selected.text, DETAIL_TEXT_MAX)}
                </p>
                <ul
                  className="badge-row"
                  aria-label="Caractéristiques de l’ancre sélectionnée"
                >
                  {occurrence === undefined ? null : (
                    <li className="badge">
                      occurrence {occurrence.index} sur {occurrence.total}
                    </li>
                  )}
                  {noteAnchors.has(selected.anchor) ? (
                    <li className="badge badge--tlp-amber">
                      remplace la note existante
                    </li>
                  ) : null}
                  {targetType === null ? null : (
                    <li className="badge">{TARGET_BLOCK_LABELS[targetType]}</li>
                  )}
                  {selectedElements === undefined ||
                  selectedElements.proposable === 0 ? null : (
                    <li className="badge">
                      {selectedElements.proposable} élément(s) M23
                      référençable(s)
                    </li>
                  )}
                  {selectedElements === undefined ||
                  selectedElements.refused === 0 ? null : (
                    <li className="badge badge--tlp-amber">
                      {selectedElements.refused} élément(s) M23 non
                      proposable(s)
                    </li>
                  )}
                </ul>
              </>
            )}
          </div>

          {missingFromCatalog ? (
            <div className="verification-warning" role="alert">
              <p>
                L’ancre <code>{anchor}</code> n’apparaît pas dans la base de
                publication affichée. Le serveur reste l’autorité : la demande
                peut être refusée (<code>invalid_note_anchor</code>).
              </p>
              <button type="button" onClick={reloadAnchors}>
                Recharger les ancres
              </button>
            </div>
          ) : null}

          {proposalBaseMoved ? (
            <div className="verification-warning" role="alert">
              <p>
                Base périmée : l’article a été révisé depuis le chargement des
                ancres. Rechargez les ancres avant de redemander une
                proposition.
              </p>
              <button type="button" onClick={reloadAnchors}>
                Recharger les ancres
              </button>
            </div>
          ) : null}
        </>
      )}

      <details>
        <summary>Diagnostic — saisie manuelle d’ancre</summary>
        <p id="opinion-note-anchor-manual-help">
          La saisie manuelle est réservée au diagnostic : aucune suggestion
          n’est fournie et le serveur reste l’autorité. Une ancre absente de la
          liste canonique peut être refusée (<code>invalid_note_anchor</code>).
        </p>
        <label htmlFor="opinion-note-anchor-manual">Ancre manuelle</label>
        <input
          id="opinion-note-anchor-manual"
          value={anchor}
          onChange={(event) => onAnchorChange(event.currentTarget.value)}
          aria-describedby="opinion-note-anchor-manual-help"
        />
      </details>
    </div>
  );
}
