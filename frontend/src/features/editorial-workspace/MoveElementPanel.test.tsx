import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MoveElementPanel } from "./MoveElementMode";
import {
  ANCHOR_ONE_ID,
  ANCHOR_TWO_ID,
  chartBlock,
  paragraphBlock,
  visualBlock,
  visualSnapshot,
} from "./visualTestFixtures";

const heading = visualBlock({
  block_id: "heading-1",
  kind: "section_heading",
  text: "Section synthétique",
  allowed_operations: ["rewrite_section"],
});
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
const snapshot = visualSnapshot({
  blocks: [heading, paragraphOne, paragraphTwo, paragraphThree, chartBlock()],
});

function renderPanel(
  overrides: {
    draftAnchorId?: string | null;
    onChooseAnchor?: (anchorBlockId: string) => void;
    onConfirm?: () => void;
  } = {},
) {
  const onChooseAnchor = overrides.onChooseAnchor ?? vi.fn();
  const onConfirm = overrides.onConfirm ?? vi.fn();
  render(
    <MoveElementPanel
      snapshot={snapshot}
      element={chartBlock()}
      readOnly={false}
      draftAnchorId={overrides.draftAnchorId ?? null}
      saving={false}
      error={null}
      onChooseAnchor={onChooseAnchor}
      onConfirm={onConfirm}
      onCancel={vi.fn()}
    />,
  );
  return { onChooseAnchor, onConfirm };
}

describe("MoveElementPanel", () => {
  it("searches paragraphs and disables the current anchor with a reason", () => {
    renderPanel();
    expect(
      screen.getByText(/Emplacement actuel : après « Paragraphe un »/),
    ).toBeVisible();
    expect(screen.getByRole("radio", { name: /Paragraphe un/ })).toBeDisabled();
    expect(screen.getByText("Emplacement actuel")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Enregistrer l’emplacement" }),
    ).toBeDisabled();
  });

  it("filters by section and search, then reports the future position", async () => {
    const user = userEvent.setup();
    const { onChooseAnchor } = renderPanel();

    await user.type(screen.getByLabelText("Rechercher un paragraphe"), "deux");
    expect(
      screen.queryByRole("radio", { name: /Paragraphe trois/ }),
    ).toBeNull();
    await user.click(screen.getByRole("radio", { name: /Paragraphe deux/ }));
    expect(onChooseAnchor).toHaveBeenCalledWith(ANCHOR_TWO_ID);
  });

  it("shows the drafted future position and confirms it", async () => {
    const user = userEvent.setup();
    const { onConfirm } = renderPanel({ draftAnchorId: ANCHOR_TWO_ID });
    expect(
      screen.getByText(/Emplacement prévu : après « Paragraphe deux »/),
    ).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Enregistrer l’emplacement" }),
    );
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("renders no anchor control when manual_edit is false", () => {
    const mutable = visualSnapshot({
      blocks: [heading, paragraphOne, paragraphTwo, chartBlock()],
    });
    render(
      <MoveElementPanel
        snapshot={{
          ...mutable,
          capabilities: { ...mutable.capabilities, manual_edit: false },
        }}
        element={chartBlock()}
        readOnly={false}
        draftAnchorId={null}
        saving={false}
        error={null}
        onChooseAnchor={vi.fn()}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(
      screen.queryByRole("radio", { name: /Paragraphe un/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Enregistrer l’emplacement" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent(
      "La préparation des révisions visuelles n’est pas activée pour cet atelier.",
    );
  });
});
