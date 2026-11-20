import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EditorialWorkspaceStatus } from "./EditorialWorkspaceStatus";

describe("EditorialWorkspaceStatus", () => {
  it("announces a pending impact and stale PDF accessibly", () => {
    render(
      <EditorialWorkspaceStatus impactStatus="pending" pdfStatus="STALE" />,
    );

    expect(screen.getByText("Impact en attente")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: /en attente/i })).toHaveAttribute(
      "aria-label",
      "La mise à jour des éléments dépendants est en attente.",
    );
    expect(
      screen.getByRole("status", { name: "PDF obsolète" }),
    ).toBeInTheDocument();
  });

  it("leaves the stale PDF announcement to the top bar when present", () => {
    render(
      <EditorialWorkspaceStatus
        impactStatus="pending"
        pdfStatus="STALE"
        showPdfStatus={false}
      />,
    );

    expect(screen.getAllByRole("status")).toHaveLength(1);
    expect(screen.queryByText("PDF obsolète")).not.toBeInTheDocument();
  });

  it("does not duplicate the current-state notice when there is no impact", () => {
    const { container } = render(
      <EditorialWorkspaceStatus impactStatus="none" pdfStatus="CURRENT" />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("keeps a stale PDF visible after the impact is complete", () => {
    render(
      <EditorialWorkspaceStatus impactStatus="up_to_date" pdfStatus="STALE" />,
    );

    expect(screen.getByText("Impact à jour")).toBeInTheDocument();
    expect(
      screen.getByRole("status", { name: "PDF obsolète" }),
    ).toBeInTheDocument();
  });

  it.each([
    ["running", "Impact en cours"],
    ["superseded", "Impact périmé"],
    ["up_to_date", "Impact à jour"],
    ["failed", "Échec de l’impact"],
  ] as const)("shows %s as %s", (impactStatus, label) => {
    render(
      <EditorialWorkspaceStatus
        impactStatus={impactStatus}
        pdfStatus="CURRENT"
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(label);
    expect(screen.queryByText("PDF obsolète")).not.toBeInTheDocument();
    if (impactStatus === "failed") {
      expect(screen.getByRole("status")).toHaveAttribute(
        "aria-label",
        expect.stringContaining("automatiquement"),
      );
      expect(
        screen.getByText(/nouvel essai sera lancé automatiquement/i),
      ).toBeInTheDocument();
    }
  });
});
