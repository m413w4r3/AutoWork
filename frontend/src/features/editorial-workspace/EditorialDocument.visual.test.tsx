import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { EditorialDocument } from "./EditorialDocument";
import {
  ANCHOR_ONE_ID,
  ANCHOR_TWO_ID,
  CHART_BLOCK_ID,
  FIGURE_A_BLOCK_ID,
  chartBlock,
  figureBlock,
  paragraphBlock,
  visualSnapshot,
} from "./visualTestFixtures";

const heading = paragraphBlock("heading-1", "Section synthétique");
const paragraphOne = paragraphBlock(
  ANCHOR_ONE_ID,
  "Paragraphe un",
  "heading-1",
);
const paragraphTwo = paragraphBlock(
  ANCHOR_TWO_ID,
  "Paragraphe deux",
  "heading-1",
);
const paragraphThree = paragraphBlock(
  "anchor-three",
  "Paragraphe trois",
  "heading-1",
);
const chart = chartBlock();
const figureA = figureBlock({
  blockId: FIGURE_A_BLOCK_ID,
  elementKey: "figure-a",
  caption: "Figure A",
  order: 1,
});
const figureB = figureBlock({
  blockId: "figure-b-block",
  elementKey: "figure-b",
  caption: "Figure B",
  order: 2,
});

const blocks = [
  heading,
  paragraphOne,
  chart,
  paragraphTwo,
  figureA,
  figureB,
  paragraphThree,
];

function handlers() {
  return {
    onSelectBlock: vi.fn(),
    onDraftChange: vi.fn(),
    onResumeDraft: vi.fn(),
    onCancelDraft: vi.fn(),
    onStartCaption: vi.fn(),
    onSaveCaption: vi.fn(() => Promise.resolve()),
    onCancelCaption: vi.fn(),
    onStartMove: vi.fn(),
    onChooseMoveAnchor: vi.fn(),
    onConfirmMove: vi.fn(),
    onCancelMove: vi.fn(),
    onStartReorder: vi.fn(),
    onSaveReorder: vi.fn(() => Promise.resolve()),
    onCancelReorder: vi.fn(),
    onRequestRemove: vi.fn(),
    onRequestReplace: vi.fn(),
    onAskBridgeVisual: vi.fn(),
    onOpenSources: vi.fn(),
    onOpenHistory: vi.fn(),
  };
}

describe("EditorialDocument visual blocks", () => {
  it("selects a visual by snapshot block id and shows its allowed actions", async () => {
    const user = userEvent.setup();
    const props = handlers();
    render(
      <EditorialDocument
        snapshot={visualSnapshot({ blocks })}
        selectedBlockId={CHART_BLOCK_ID}
        drafts={{}}
        staleDraftIds={new Set()}
        editingBlockId={null}
        {...props}
      />,
    );

    const chartBlockElement = document.querySelector<HTMLElement>(
      `[data-block-id="${CHART_BLOCK_ID}"]`,
    );
    expect(chartBlockElement).toHaveAttribute("data-element-kind", "chart");
    expect(chartBlockElement).toHaveAttribute("data-element-key", "chart-a");

    expect(
      screen.getByRole("toolbar", {
        name: "Actions du visuel : Titre actuel du graphique",
      }),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Déplacer" }));
    expect(props.onStartMove).toHaveBeenCalledWith(CHART_BLOCK_ID);
    await user.click(screen.getByRole("button", { name: "Retirer" }));
    expect(props.onRequestRemove).toHaveBeenCalledWith(CHART_BLOCK_ID);
  });

  it("renders the inline caption editor instead of the visual content", () => {
    render(
      <EditorialDocument
        snapshot={visualSnapshot({ blocks })}
        selectedBlockId={FIGURE_A_BLOCK_ID}
        drafts={{}}
        staleDraftIds={new Set()}
        editingBlockId={null}
        captionEditingBlockId={FIGURE_A_BLOCK_ID}
        {...handlers()}
      />,
    );
    expect(screen.getByLabelText("Légende éditoriale")).toHaveValue("Figure A");
    expect(screen.getByText("Légende d’origine")).toBeVisible();
    expect(screen.getByText("Légende d’origine archivée")).toBeVisible();
  });

  it("offers paragraph anchors in move mode and shows the provisional preview", () => {
    render(
      <EditorialDocument
        snapshot={visualSnapshot({ blocks })}
        selectedBlockId={CHART_BLOCK_ID}
        drafts={{}}
        staleDraftIds={new Set()}
        editingBlockId={null}
        moveState={{
          elementBlockId: CHART_BLOCK_ID,
          draftAnchorId: ANCHOR_TWO_ID,
          error: null,
          saving: false,
        }}
        {...handlers()}
      />,
    );

    expect(
      screen.getByRole("region", { name: "Déplacement du visuel" }),
    ).toBeVisible();
    expect(
      screen.getByText("Insertion prévue : après « Paragraphe deux »"),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Enregistrer l’emplacement" }),
    ).toBeEnabled();
    expect(
      screen.getByText(
        "Emplacement actuel du visuel — choisissez un autre paragraphe.",
      ),
    ).toBeVisible();
  });

  it("lets a paragraph be chosen as move anchor by click and by Enter", async () => {
    const user = userEvent.setup();
    const props = handlers();
    render(
      <EditorialDocument
        snapshot={visualSnapshot({ blocks })}
        selectedBlockId={CHART_BLOCK_ID}
        drafts={{}}
        staleDraftIds={new Set()}
        editingBlockId={null}
        moveState={{
          elementBlockId: CHART_BLOCK_ID,
          draftAnchorId: null,
          error: null,
          saving: false,
        }}
        {...props}
      />,
    );

    const target = document.querySelector<HTMLElement>(
      `[data-block-id="anchor-three"]`,
    );
    if (!target) throw new Error("paragraph target missing");
    await user.click(target);
    expect(props.onChooseMoveAnchor).toHaveBeenCalledWith("anchor-three");

    target.focus();
    await user.keyboard("{Enter}");
    expect(props.onChooseMoveAnchor).toHaveBeenCalledTimes(2);
  });

  it("renders the reorder panel for elements sharing the anchor", () => {
    render(
      <EditorialDocument
        snapshot={visualSnapshot({ blocks })}
        selectedBlockId={FIGURE_A_BLOCK_ID}
        drafts={{}}
        staleDraftIds={new Set()}
        editingBlockId={null}
        reorderBlockId={FIGURE_A_BLOCK_ID}
        {...handlers()}
      />,
    );
    expect(
      screen.getByRole("group", { name: "Réorganiser les visuels" }),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Monter Figure A" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Descendre Figure B" }),
    ).toBeDisabled();
    expect(screen.getByText(/Ordre prévu :/)).toHaveTextContent(
      "1. Titre actuel du graphique",
    );
  });

  it("hides revision actions in read-only mode while keeping the block selectable", () => {
    render(
      <EditorialDocument
        snapshot={visualSnapshot({
          blocks,
          readOnlyReason: "historical_version",
        })}
        selectedBlockId={CHART_BLOCK_ID}
        drafts={{}}
        staleDraftIds={new Set()}
        editingBlockId={null}
        readOnly
        {...handlers()}
      />,
    );
    expect(
      screen.queryByRole("button", { name: "Modifier la légende" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "Lecture seule : les révisions de ce visuel sont indisponibles.",
      ),
    ).toBeVisible();
    expect(
      document.querySelector(`[data-block-id="${CHART_BLOCK_ID}"]`),
    ).toHaveAttribute("tabindex", "0");
  });
});
