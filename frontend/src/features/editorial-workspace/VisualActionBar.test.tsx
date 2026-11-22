import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { VisualActionBar } from "./VisualActionBar";
import { chartBlock, figureBlock, visualBlock } from "./visualTestFixtures";

function callbacks() {
  return {
    onEditCaption: vi.fn(),
    onAskBridge: vi.fn(),
    onStartMove: vi.fn(),
    onStartReorder: vi.fn(),
    onRequestReplace: vi.fn(),
    onRequestRemove: vi.fn(),
    onOpenSources: vi.fn(),
    onOpenHistory: vi.fn(),
  };
}

describe("VisualActionBar", () => {
  it("renders only the actions the snapshot allows", () => {
    const handlers = callbacks();
    const block = chartBlock({
      evidence_refs: [{ label: "Source synthétique" }],
    });
    render(
      <VisualActionBar
        block={block}
        readOnly={false}
        canDiscuss
        {...handlers}
      />,
    );

    expect(
      screen.getByRole("toolbar", {
        name: "Actions du visuel : Titre actuel du graphique",
      }),
    ).toBeVisible();
    for (const name of [
      "Modifier la légende",
      "Demander à Bridge",
      "Déplacer",
      "Réorganiser",
      "Retirer",
      "Sources",
      "Historique",
    ]) {
      expect(screen.getByRole("button", { name })).toBeEnabled();
    }
    expect(
      screen.queryByRole("button", { name: "Remplacer" }),
    ).not.toBeInTheDocument();
  });

  it("hides every revision action not declared by allowed_operations", () => {
    const handlers = callbacks();
    const block = visualBlock({
      block_id: "locked-chart",
      kind: "chart",
      element_kind: "chart",
      element_key: "chart-locked",
      chart: chartBlock().chart,
      allowed_operations: [],
      evidence_refs: [{ label: "Source synthétique" }],
    });
    render(
      <VisualActionBar
        block={block}
        readOnly={false}
        canDiscuss
        {...handlers}
      />,
    );

    expect(
      screen.queryByRole("button", { name: "Modifier la légende" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Retirer" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sources" })).toBeVisible();
  });

  it("exposes Remplacer only for an allowed figure and keeps read-only mode honest", () => {
    const handlers = callbacks();
    const figure = figureBlock({
      blockId: "figure-block",
      elementKey: "figure-a",
      caption: "Figure A",
      order: 0,
    });
    const { rerender } = render(
      <VisualActionBar
        block={figure}
        readOnly={false}
        canDiscuss
        {...handlers}
      />,
    );
    expect(screen.getByRole("button", { name: "Remplacer" })).toBeEnabled();

    rerender(
      <VisualActionBar block={figure} readOnly canDiscuss {...handlers} />,
    );
    expect(
      screen.queryByRole("button", { name: "Modifier la légende" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Remplacer" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "Lecture seule : les révisions de ce visuel sont indisponibles.",
      ),
    ).toBeVisible();
  });

  it("does not offer a Bridge correction when the discussion capability is off", () => {
    render(
      <VisualActionBar
        block={chartBlock()}
        readOnly={false}
        canDiscuss={false}
        {...callbacks()}
      />,
    );
    expect(
      screen.queryByRole("button", { name: "Demander à Bridge" }),
    ).not.toBeInTheDocument();
  });
});
