import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ReorderElements } from "./ReorderElements";
import { ANCHOR_ONE_ID, figureBlock } from "./visualTestFixtures";

const first = figureBlock({
  blockId: "figure-a-block",
  elementKey: "figure-a",
  caption: "Figure A",
  order: 0,
});
const second = figureBlock({
  blockId: "figure-b-block",
  elementKey: "figure-b",
  caption: "Figure B",
  order: 1,
});

describe("ReorderElements", () => {
  it("moves elements with Monter/Descendre, disables the ends with a reason and saves the exact permutation", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    render(
      <ReorderElements
        anchorBlockId={ANCHOR_ONE_ID}
        elements={[first, second]}
        saving={false}
        error={null}
        onSave={onSave}
        onCancel={vi.fn()}
      />,
    );

    const firstRow = screen.getByText("Figure A").closest("li");
    const secondRow = screen.getByText("Figure B").closest("li");
    if (!firstRow || !secondRow) throw new Error("rows missing");

    expect(
      within(firstRow).getByRole("button", { name: "Monter Figure A" }),
    ).toBeDisabled();
    expect(
      within(firstRow).getByText("Déjà en première position"),
    ).toBeVisible();
    expect(
      within(secondRow).getByRole("button", { name: "Descendre Figure B" }),
    ).toBeDisabled();
    expect(
      within(secondRow).getByText("Déjà en dernière position"),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Enregistrer l’ordre" }),
    ).toBeDisabled();

    await user.click(
      within(firstRow).getByRole("button", { name: "Descendre Figure A" }),
    );

    const preview = screen.getByText(/Ordre prévu :/);
    expect(preview).toHaveTextContent("1. Figure B");
    expect(preview).toHaveTextContent("2. Figure A");
    expect(
      screen.getByRole("button", { name: "Enregistrer l’ordre" }),
    ).toBeEnabled();

    await user.click(
      screen.getByRole("button", { name: "Enregistrer l’ordre" }),
    );
    expect(onSave).toHaveBeenCalledWith([
      { kind: "figure", key: "figure-b" },
      { kind: "figure", key: "figure-a" },
    ]);
  });

  it("shows the refusal message and keeps the panel open", () => {
    render(
      <ReorderElements
        anchorBlockId={ANCHOR_ONE_ID}
        elements={[first, second]}
        saving={false}
        error="L’ordre demandé ne correspond pas aux éléments de ce paragraphe."
        onSave={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "L’ordre demandé ne correspond pas aux éléments de ce paragraphe.",
    );
  });

  it("cancels without saving", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    render(
      <ReorderElements
        anchorBlockId={ANCHOR_ONE_ID}
        elements={[first, second]}
        saving={false}
        error={null}
        onSave={vi.fn()}
        onCancel={onCancel}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Annuler" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
