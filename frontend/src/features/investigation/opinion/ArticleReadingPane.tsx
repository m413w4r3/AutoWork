import { useMemo, useRef } from "react";

import type {
  ArticleAnchorCatalogViewV1,
  ArticleAnchorViewV1,
  ArticleElementViewV1,
} from "../api/articleAnchors";
import type { NoteProposalViewV1 } from "../api/opinionProposals";
import { shortHash } from "./labels";
import {
  anchorOccurrences,
  groupAnchors,
  type AnchorOccurrenceV1,
} from "./AnchorPicker";
import type { AnalystNoteV1 } from "./types";

function sectionIndex(groupKey: string): number | null {
  if (!groupKey.startsWith("section:")) return null;
  const index = Number(groupKey.slice("section:".length));
  return Number.isInteger(index) ? index : null;
}

function targetDescription(item: ArticleAnchorViewV1): string {
  if (item.section_index === null) {
    return `Chapô, paragraphe ${(item.paragraph_index ?? 0) + 1}`;
  }
  return `Section ${item.section_index + 1}, §${(item.paragraph_index ?? 0) + 1}`;
}

function ArticleElement({ element }: { element: ArticleElementViewV1 }) {
  return (
    <aside className="article-reading__element">
      <span className="article-reading__element-kind">
        Élément M23 · {element.kind}
      </span>
      {element.title ? <strong>{element.title}</strong> : null}
      {element.caption ? <p>{element.caption}</p> : null}
      {!element.proposable ? (
        <span className="article-reading__warning">
          Cet élément n’est pas référençable dans une Note.
        </span>
      ) : null}
    </aside>
  );
}

export function ArticleReadingPane({
  catalog,
  isPending,
  errorMessage,
  notes,
  proposals,
  selectedAnchor,
  onSelectAnchor,
}: {
  catalog: ArticleAnchorCatalogViewV1 | null;
  isPending: boolean;
  errorMessage: string | null;
  notes: AnalystNoteV1[];
  proposals: Pick<NoteProposalViewV1, "anchor" | "status">[];
  selectedAnchor: string;
  onSelectAnchor: (anchor: string) => void;
}) {
  const buttonRefs = useRef(new Map<string, HTMLButtonElement>());
  const groups = useMemo(
    () => (catalog === null ? [] : groupAnchors(catalog.anchors)),
    [catalog],
  );
  const occurrences = useMemo(
    (): Map<string, AnchorOccurrenceV1> =>
      catalog === null
        ? new Map<string, AnchorOccurrenceV1>()
        : anchorOccurrences(catalog.anchors),
    [catalog],
  );
  const noteAnchors = useMemo(
    () => new Set(notes.map((note) => note.anchor)),
    [notes],
  );
  const waitingAnchors = useMemo(
    () =>
      new Set(
        proposals
          .filter((proposal) => proposal.status === "proposed")
          .map((proposal) => proposal.anchor),
      ),
    [proposals],
  );
  const orderedAnchors = useMemo(
    () => groups.flatMap((group) => group.anchors),
    [groups],
  );
  const groupByKey = useMemo(
    () => new Map(groups.map((group) => [group.key, group] as const)),
    [groups],
  );
  const sectionsByIndex = useMemo(
    () =>
      new Map(
        (catalog?.sections ?? []).map(
          (section) => [section.section_index, section.heading] as const,
        ),
      ),
    [catalog?.sections],
  );
  const elementsByAnchor = useMemo(() => {
    const result = new Map<string, ArticleElementViewV1[]>();
    for (const element of catalog?.elements ?? []) {
      if (element.anchor === null) continue;
      const bucket = result.get(element.anchor) ?? [];
      bucket.push(element);
      result.set(element.anchor, bucket);
    }
    return result;
  }, [catalog?.elements]);
  const unplacedElements = useMemo(
    () =>
      (catalog?.elements ?? []).filter((element) => element.anchor === null),
    [catalog?.elements],
  );

  function moveSelection(currentAnchor: string, delta: -1 | 1) {
    const currentIndex = orderedAnchors.findIndex(
      (item) => item.anchor === currentAnchor,
    );
    if (currentIndex < 0 || orderedAnchors.length < 2) return;
    const nextIndex = Math.min(
      Math.max(currentIndex + delta, 0),
      orderedAnchors.length - 1,
    );
    const next = orderedAnchors[nextIndex];
    if (next === undefined || next.anchor === currentAnchor) return;
    onSelectAnchor(next.anchor);
    buttonRefs.current.get(next.anchor)?.focus();
  }

  function renderParagraph(item: ArticleAnchorViewV1) {
    const occurrence = occurrences.get(item.anchor);
    const noteExists = noteAnchors.has(item.anchor);
    const proposalPending = waitingAnchors.has(item.anchor);
    const elements = elementsByAnchor.get(item.anchor) ?? [];
    return (
      <article
        key={item.anchor}
        className="article-reading__paragraph"
        data-selected={selectedAnchor === item.anchor ? "true" : undefined}
      >
        <button
          ref={(element) => {
            if (element === null) buttonRefs.current.delete(item.anchor);
            else buttonRefs.current.set(item.anchor, element);
          }}
          type="button"
          className="article-reading__select"
          aria-pressed={selectedAnchor === item.anchor}
          aria-label={`${targetDescription(item)}. ${item.text} + Insérer une Note`}
          tabIndex={
            selectedAnchor === item.anchor ||
            (selectedAnchor === "" && orderedAnchors[0]?.anchor === item.anchor)
              ? 0
              : -1
          }
          onClick={() => onSelectAnchor(item.anchor)}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              moveSelection(item.anchor, 1);
            } else if (event.key === "ArrowUp") {
              event.preventDefault();
              moveSelection(item.anchor, -1);
            }
          }}
        >
          <span className="article-reading__text">{item.text}</span>
          <span className="article-reading__action">+ Insérer une Note</span>
        </button>
        {occurrence !== undefined || noteExists || proposalPending ? (
          <ul
            className="article-reading__markers"
            aria-label="État du paragraphe"
          >
            {occurrence === undefined ? null : (
              <li className="badge">
                occurrence {occurrence.index} sur {occurrence.total}
              </li>
            )}
            {noteExists ? <li className="badge">Note existante</li> : null}
            {proposalPending ? (
              <li className="badge badge--tlp-amber">
                Proposition Bridge en attente
              </li>
            ) : null}
          </ul>
        ) : null}
        {elements.map((element) => (
          <ArticleElement
            key={`${element.kind}:${element.key}`}
            element={element}
          />
        ))}
      </article>
    );
  }

  if (isPending) {
    return (
      <section
        className="job-card article-reading"
        aria-labelledby="article-reading-heading"
      >
        <h3 id="article-reading-heading">Article source</h3>
        <p role="status">Chargement de l’article canonique…</p>
      </section>
    );
  }

  if (catalog === null) {
    return (
      <section
        className="job-card article-reading"
        aria-labelledby="article-reading-heading"
      >
        <h3 id="article-reading-heading">Article source</h3>
        <p className="verification-warning" role="alert">
          {errorMessage ?? "Le catalogue de l’article est indisponible."}
        </p>
      </section>
    );
  }

  const sectionGroups = groups.filter((group) => group.key !== "lead");

  return (
    <section
      className="job-card article-reading"
      aria-labelledby="article-reading-heading"
    >
      <header className="article-reading__header">
        <h3 id="article-reading-heading">
          {catalog.article_title ?? "Article source"}
        </h3>
        <p>
          Base de publication · v{catalog.base_publication_version} · SHA-256{" "}
          <code title={catalog.base_publication_sha256}>
            {shortHash(catalog.base_publication_sha256)}
          </code>
        </p>
        <p>
          Lecture seule · sélectionnez un paragraphe pour préparer une Note.
        </p>
        {catalog.truncated ? (
          <p className="verification-warning" role="note">
            Catalogue borné : certains paragraphes ou éléments ne sont pas
            affichés.
          </p>
        ) : null}
      </header>

      <div className="article-reading__body">
        {groupByKey.has("lead") ? (
          <section aria-labelledby="article-reading-lead-heading">
            <h4 id="article-reading-lead-heading">Chapô</h4>
            {groupByKey.get("lead")?.anchors.map(renderParagraph)}
          </section>
        ) : null}

        {sectionGroups.map((group) => {
          const index = sectionIndex(group.key);
          const fallbackHeading =
            index === null ? "Section" : `Section ${index + 1}`;
          const heading =
            index === null
              ? fallbackHeading
              : (sectionsByIndex.get(index) ?? fallbackHeading);
          return (
            <section key={group.key} aria-label={heading}>
              <h4>{heading}</h4>
              {group.anchors.map(renderParagraph)}
            </section>
          );
        })}

        {unplacedElements.length > 0 ? (
          <section aria-labelledby="article-reading-unplaced-heading">
            <h4 id="article-reading-unplaced-heading">Éléments de l’article</h4>
            {unplacedElements.map((element) => (
              <ArticleElement
                key={`${element.kind}:${element.key}`}
                element={element}
              />
            ))}
          </section>
        ) : null}
      </div>
    </section>
  );
}
