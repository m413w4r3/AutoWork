import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RemoveElementDialog } from "./RemoveElementDialog";
import { figureBlock } from "./visualTestFixtures";

const block = figureBlock({
  blockId: "figure-block",
  elementKey: "figure-a",
  caption: "Figure à retirer",
  order: 0,
});

describe("RemoveElementDialog", () => {
  it("traps focus, explains archival and confirms removal", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <RemoveElementDialog
        block={block}
        saving={false}
        error={null}
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );

    const dialog = screen.getByRole("dialog", {
      name: "Retirer ce visuel du document ?",
    });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(
      screen.getByText(
        "Retirer ce visuel du document ? Il restera dans les sources archivées et dans l’historique.",
      ),
    ).toBeVisible();
    expect(screen.getByText(/Figure · Figure à retirer/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Annuler" })).toHaveFocus();

    await user.tab();
    expect(
      screen.getByRole("button", { name: "Retirer le visuel" }),
    ).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Annuler" })).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Retirer le visuel" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("cancels on Escape and shows a closed-code error without closing", () => {
    const onCancel = vi.fn();
    const { rerender } = render(
      <RemoveElementDialog
        block={block}
        saving={false}
        error={null}
        onConfirm={vi.fn()}
        onCancel={onCancel}
      />,
    );

    const dialog = screen.getByRole("dialog");
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);

    rerender(
      <RemoveElementDialog
        block={block}
        saving={false}
        error="Cet élément enrichi n’existe plus dans cette version."
        onConfirm={vi.fn()}
        onCancel={onCancel}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Cet élément enrichi n’existe plus dans cette version.",
    );
    expect(screen.getByRole("dialog")).toBeVisible();
  });

  it("disables both controls while the removal is being adopted", () => {
    render(
      <RemoveElementDialog
        block={block}
        saving
        error={null}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Retrait en cours…" }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "Annuler" })).toBeDisabled();
  });
});
