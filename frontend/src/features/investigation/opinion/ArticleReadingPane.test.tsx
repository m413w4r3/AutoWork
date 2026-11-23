import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ArticleAnchorCatalogViewV1 } from "../api/articleAnchors";
import { ArticleReadingPane } from "./ArticleReadingPane";
import type { AnalystNoteV1 } from "./types";

const BASE_SHA = "a".repeat(64);
const DUPLICATE_SHA = "b".repeat(64);

function makeCatalog(): ArticleAnchorCatalogViewV1 {
  return {
    base_publication_artifact_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    base_publication_version: 4,
    base_publication_sha256: BASE_SHA,
    source: "pinned_opinion_base",
    article_title: "Campagne de test",
    sections: [
      { section_index: 0, heading: "Analyse" },
      { section_index: 1, heading: "Infrastructure" },
    ],
    anchors: [
      {
        anchor: "lead:0001",
        text: "Le chapô situe la campagne.",
        text_sha256: "1".repeat(64),
        section_index: null,
        paragraph_index: 0,
      },
      {
        anchor: "lead:0002",
        text: "Le même paragraphe apparaît deux fois.",
        text_sha256: DUPLICATE_SHA,
        section_index: null,
        paragraph_index: 1,
      },
      {
        anchor: "section:0:paragraph:0001",
        text: "Le même paragraphe apparaît deux fois.",
        text_sha256: DUPLICATE_SHA,
        section_index: 0,
        paragraph_index: 0,
      },
      {
        anchor: "section:0:paragraph:0002",
        text: "Le premier élément est visible.",
        text_sha256: "2".repeat(64),
        section_index: 0,
        paragraph_index: 1,
      },
      {
        anchor: "section:1:paragraph:0001",
        text: "Le serveur reçoit une commande.",
        text_sha256: "3".repeat(64),
        section_index: 1,
        paragraph_index: 0,
      },
    ],
    elements: [
      {
        kind: "table",
        key: "observed-commands",
        title: "Commandes observées",
        caption: "Une table M23 placée après le premier élément.",
        element_sha256: "4".repeat(64),
        anchor: "section:0:paragraph:0002",
        proposable: true,
        refusal_reason: null,
      },
    ],
    truncated: false,
  };
}

function makeNote(anchor: string): AnalystNoteV1 {
  return {
    id: "44444444-4444-4444-8444-444444444444",
    anchor,
    text: "Une note existe à cette position.",
    state: "observed",
    relation: "confirms",
    finding_refs: [],
    evidence_refs: [],
  };
}

function renderPane({
  selectedAnchor = "lead:0001",
  notes = [],
  proposals = [],
  onSelectAnchor = vi.fn(),
}: {
  selectedAnchor?: string;
  notes?: AnalystNoteV1[];
  proposals?: Array<{ anchor: string; status: "proposed" | "accepted" }>;
  onSelectAnchor?: (anchor: string) => void;
} = {}) {
  return render(
    <ArticleReadingPane
      catalog={makeCatalog()}
      isPending={false}
      errorMessage={null}
      notes={notes}
      proposals={proposals}
      selectedAnchor={selectedAnchor}
      onSelectAnchor={onSelectAnchor}
    />,
  );
}

describe("ArticleReadingPane", () => {
  it("rend le titre, le chapô et les sections dans l’ordre canonique", () => {
    const { container } = renderPane();
    const buttons = screen.getAllByRole("button", {
      name: /Insérer une Note/,
    });

    expect(
      screen.getByRole("heading", { name: "Campagne de test" }),
    ).toBeInTheDocument();
    expect(
      within(container)
        .getAllByRole("heading", { level: 4 })
        .map((heading) => heading.textContent),
    ).toEqual(["Chapô", "Analyse", "Infrastructure"]);
    expect(buttons.map((button) => button.getAttribute("aria-label"))).toEqual([
      expect.stringContaining("Le chapô situe la campagne."),
      expect.stringContaining("Le même paragraphe apparaît deux fois."),
      expect.stringContaining("Le même paragraphe apparaît deux fois."),
      expect.stringContaining("Le premier élément est visible."),
      expect.stringContaining("Le serveur reçoit une commande."),
    ]);
    expect(screen.getByText("Commandes observées")).toBeInTheDocument();
    expect(
      screen.getByText("Une table M23 placée après le premier élément."),
    ).toBeInTheDocument();
  });

  it("garde deux ancres distinctes pour deux paragraphes identiques", () => {
    renderPane();

    expect(
      screen.getByRole("button", { name: /Chapô, paragraphe 2/ }),
    ).toHaveAccessibleName(/Le même paragraphe apparaît deux fois/);
    expect(
      screen.getByRole("button", { name: /Section 1, §1/ }),
    ).toHaveAccessibleName(/Le même paragraphe apparaît deux fois/);
    expect(screen.getByText("occurrence 1 sur 2")).toBeInTheDocument();
    expect(screen.getByText("occurrence 2 sur 2")).toBeInTheDocument();
    expect(screen.queryByText(/(?:lead|section):\d/)).toBeNull();
  });

  it("sélectionne par clic, Entrée/Espace et flèches haut/bas", async () => {
    const user = userEvent.setup();
    const onSelectAnchor = vi.fn();
    renderPane({ onSelectAnchor });

    const firstSectionParagraph = screen.getByRole("button", {
      name: /Section 1, §1/,
    });
    await user.click(firstSectionParagraph);
    expect(onSelectAnchor).toHaveBeenLastCalledWith("section:0:paragraph:0001");

    await user.keyboard("{ArrowDown}");
    expect(onSelectAnchor).toHaveBeenLastCalledWith("section:0:paragraph:0002");
    const secondSectionParagraph = screen.getByRole("button", {
      name: /Section 1, §2/,
    });
    expect(secondSectionParagraph).toHaveFocus();

    await user.keyboard("{ArrowUp}");
    expect(onSelectAnchor).toHaveBeenLastCalledWith("section:0:paragraph:0001");
    await user.keyboard("{Enter}");
    expect(onSelectAnchor).toHaveBeenLastCalledWith("section:0:paragraph:0001");
    await user.keyboard(" ");
    expect(onSelectAnchor).toHaveBeenCalledTimes(5);
  });

  it("signale une note existante et une proposition Bridge en attente", () => {
    renderPane({
      notes: [makeNote("section:0:paragraph:0001")],
      proposals: [{ anchor: "section:0:paragraph:0002", status: "proposed" }],
    });

    expect(screen.getByText("Note existante")).toBeInTheDocument();
    expect(
      screen.getByText("Proposition Bridge en attente"),
    ).toBeInTheDocument();
  });
});
