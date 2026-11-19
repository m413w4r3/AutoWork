import { useState } from "react";

interface EditorialDiffViewProps {
  baseText: string;
  proposedText: string;
  baseLabel?: string;
}

type ParagraphChange = { kind: "same" | "removed" | "added"; text: string };

function paragraphs(text: string): string[] {
  return text.split(/\n\s*\n/).filter((paragraph) => paragraph.length > 0);
}

function paragraphChanges(
  baseText: string,
  proposedText: string,
): ParagraphChange[] {
  const base = paragraphs(baseText);
  const proposed = paragraphs(proposedText);
  const lengths = Array.from({ length: base.length + 1 }, () =>
    Array<number>(proposed.length + 1).fill(0),
  );

  for (let baseIndex = base.length - 1; baseIndex >= 0; baseIndex -= 1) {
    const row = lengths[baseIndex];
    const nextRow = lengths[baseIndex + 1];
    if (!row || !nextRow) continue;
    for (
      let proposedIndex = proposed.length - 1;
      proposedIndex >= 0;
      proposedIndex -= 1
    ) {
      const baseParagraph = base[baseIndex] ?? "";
      const proposedParagraph = proposed[proposedIndex] ?? "";
      row[proposedIndex] =
        baseParagraph === proposedParagraph
          ? (nextRow[proposedIndex + 1] ?? 0) + 1
          : Math.max(nextRow[proposedIndex] ?? 0, row[proposedIndex + 1] ?? 0);
    }
  }

  const changes: ParagraphChange[] = [];
  let baseIndex = 0;
  let proposedIndex = 0;
  while (baseIndex < base.length || proposedIndex < proposed.length) {
    if (
      baseIndex < base.length &&
      proposedIndex < proposed.length &&
      (base[baseIndex] ?? "") === (proposed[proposedIndex] ?? "")
    ) {
      changes.push({ kind: "same", text: base[baseIndex] ?? "" });
      baseIndex += 1;
      proposedIndex += 1;
    } else if (
      baseIndex < base.length &&
      (proposedIndex >= proposed.length ||
        (lengths[baseIndex + 1]?.[proposedIndex] ?? 0) >=
          (lengths[baseIndex]?.[proposedIndex + 1] ?? 0))
    ) {
      changes.push({ kind: "removed", text: base[baseIndex] ?? "" });
      baseIndex += 1;
    } else {
      changes.push({ kind: "added", text: proposed[proposedIndex] ?? "" });
      proposedIndex += 1;
    }
  }
  return changes;
}

export function EditorialDiffView({
  baseText,
  proposedText,
  baseLabel = "Version actuelle",
}: EditorialDiffViewProps) {
  const [showDifferences, setShowDifferences] = useState(false);
  const changes = paragraphChanges(baseText, proposedText);

  return (
    <section
      className="editorial-diff-view"
      aria-label="Comparaison éditoriale"
    >
      <div
        className="editorial-diff-view__toolbar"
        aria-label="Mode de comparaison"
      >
        <button
          type="button"
          aria-pressed={!showDifferences}
          onClick={() => setShowDifferences(false)}
        >
          Comparaison
        </button>
        <button
          type="button"
          aria-pressed={showDifferences}
          onClick={() => setShowDifferences(true)}
        >
          Différences
        </button>
      </div>
      {showDifferences ? (
        <div
          className="editorial-diff-view__changes"
          role="group"
          aria-label="Différences du paragraphe"
        >
          {changes.map((change, index) => (
            <p key={`${change.kind}-${index}`}>
              {change.kind === "removed" ? (
                <del>
                  <span className="editorial-diff-view__marker">
                    Supprimé ·{" "}
                  </span>
                  {change.text}
                </del>
              ) : change.kind === "added" ? (
                <ins>
                  <span className="editorial-diff-view__marker">Ajouté · </span>
                  {change.text}
                </ins>
              ) : (
                change.text
              )}
            </p>
          ))}
        </div>
      ) : (
        <div className="editorial-diff-view__panels">
          <section aria-label={baseLabel}>
            <h5>{baseLabel}</h5>
            {paragraphs(baseText).map((paragraph, index) => (
              <p key={`base-${index}`}>{paragraph}</p>
            ))}
          </section>
          <section aria-label="Proposition">
            <h5>Proposition</h5>
            {paragraphs(proposedText).map((paragraph, index) => (
              <p key={`proposal-${index}`}>{paragraph}</p>
            ))}
          </section>
        </div>
      )}
    </section>
  );
}
