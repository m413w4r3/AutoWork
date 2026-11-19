import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EditorialDiffView } from "./EditorialDiffView";

describe("EditorialDiffView", () => {
  it("shows paragraph text changes in an accessible difference mode", () => {
    render(
      <EditorialDiffView
        baseText={
          "Le premier paragraphe reste intact.\n\nAncienne formulation."
        }
        proposedText={
          "Le premier paragraphe reste intact.\n\nNouvelle formulation."
        }
      />,
    );

    expect(
      screen.getByRole("heading", { name: "Version actuelle" }),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: "Proposition" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Différences" }));

    const diff = screen.getByRole("group", {
      name: "Différences du paragraphe",
    });
    expect(diff.querySelector("del")).toHaveTextContent(
      "Ancienne formulation.",
    );
    expect(diff.querySelector("ins")).toHaveTextContent(
      "Nouvelle formulation.",
    );
    expect(diff).toHaveTextContent("Supprimé");
    expect(diff).toHaveTextContent("Ajouté");
    expect(diff).not.toHaveTextContent(
      /editorial-patch|schema_version|target_id/,
    );
  });
});
