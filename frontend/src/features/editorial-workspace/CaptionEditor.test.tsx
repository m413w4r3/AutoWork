import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CaptionEditor } from "./CaptionEditor";
import { figureBlock } from "./visualTestFixtures";

const captionBlock = figureBlock({
  blockId: "figure-block",
  elementKey: "figure-a",
  caption: "Légende actuelle",
  order: 0,
  originalCaption: "Légende archivée d’origine",
});

describe("CaptionEditor", () => {
  it("shows the editorial caption, the read-only original caption and a live preview", async () => {
    const user = userEvent.setup();
    render(
      <CaptionEditor
        block={captionBlock}
        onSave={vi.fn(() => Promise.resolve())}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByLabelText("Légende éditoriale")).toHaveValue(
      "Légende actuelle",
    );
    expect(screen.getByText("Légende d’origine")).toBeVisible();
    expect(screen.getByText("Légende archivée d’origine")).toBeVisible();

    const input = screen.getByLabelText("Légende éditoriale");
    await user.clear(input);
    await user.type(input, "Nouvelle légende éditoriale");
    expect(
      screen.getByText("Nouvelle légende éditoriale", {
        selector: ".visual-caption-editor__preview-text",
      }),
    ).toBeVisible();
  });

  it("saves the trimmed caption on Ctrl+S and keeps errors in place", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn<(caption: string | null) => Promise<void>>(() =>
      Promise.resolve(),
    );
    render(
      <CaptionEditor block={captionBlock} onSave={onSave} onCancel={vi.fn()} />,
    );

    const input = screen.getByLabelText("Légende éditoriale");
    await user.clear(input);
    await user.type(input, "  Légende corrigée  ");
    await user.keyboard("{Control>}s{/Control}");

    expect(onSave).toHaveBeenCalledWith("Légende corrigée");
  });

  it("keeps the typed caption and shows the closed-code message on failure", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(() =>
      Promise.reject(
        new Error(
          "La légende proposée ne respecte pas les règles de l’atelier.",
        ),
      ),
    );
    render(
      <CaptionEditor block={captionBlock} onSave={onSave} onCancel={vi.fn()} />,
    );

    const input = screen.getByLabelText("Légende éditoriale");
    await user.clear(input);
    await user.type(input, "Légende invalide");
    await user.click(screen.getByRole("button", { name: "Enregistrer" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "La légende proposée ne respecte pas les règles de l’atelier.",
    );
    expect(input).toHaveValue("Légende invalide");
  });

  it("asks before abandoning a dirty caption and supports cancel", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    render(
      <CaptionEditor
        block={captionBlock}
        onSave={vi.fn(() => Promise.resolve())}
        onCancel={onCancel}
      />,
    );

    const input = screen.getByLabelText("Légende éditoriale");
    await user.type(input, " modifiée");
    fireEvent.keyDown(input, { key: "Escape" });

    expect(
      screen.getByText("Abandonner la modification de la légende ?"),
    ).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Continuer l’édition" }),
    );
    expect(onCancel).not.toHaveBeenCalled();
    expect(input).toHaveFocus();

    fireEvent.keyDown(input, { key: "Escape" });
    await user.click(
      screen.getByRole("button", { name: "Abandonner la modification" }),
    );
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("disables saving when the caption is unchanged", () => {
    render(
      <CaptionEditor
        block={captionBlock}
        onSave={vi.fn(() => Promise.resolve())}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Enregistrer" })).toBeDisabled();
    expect(
      screen.getByText(
        "La légende est inchangée. Modifiez-la pour activer l’enregistrement.",
      ),
    ).toBeVisible();
  });
});
