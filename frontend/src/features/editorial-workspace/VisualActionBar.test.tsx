import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { VisualActionBar } from "./VisualActionBar";
import type { VisualCapabilityFlags } from "./visualElementOperations";
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

function capabilities(
  overrides: Partial<VisualCapabilityFlags> = {},
): VisualCapabilityFlags {
  return {
    read_only: false,
    manual_edit: true,
    can_discuss: true,
    ...overrides,
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
        capabilities={capabilities()}
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
        capabilities={capabilities()}
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

  it("hides every manual mutation when manual_edit is false and says why", () => {
    const handlers = callbacks();
    const figure = figureBlock({
      blockId: "figure-block",
      elementKey: "figure-a",
      caption: "Figure A",
      order: 0,
    });
    render(
      <VisualActionBar
        block={figure}
        capabilities={capabilities({ manual_edit: false })}
        {...handlers}
      />,
    );

    for (const name of [
      "Modifier la légende",
      "Déplacer",
      "Réorganiser",
      "Remplacer",
      "Retirer",
    ]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
    // Bridge is governed by its own truthful capability.
    expect(
      screen.getByRole("button", { name: "Demander à Bridge" }),
    ).toBeVisible();
    expect(screen.getByRole("note")).toHaveTextContent(
      "La préparation des révisions visuelles n’est pas activée pour cet atelier.",
    );
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
        capabilities={capabilities()}
        {...handlers}
      />,
    );
    expect(screen.getByRole("button", { name: "Remplacer" })).toBeEnabled();

    rerender(
      <VisualActionBar
        block={figure}
        capabilities={capabilities({ read_only: true })}
        {...handlers}
      />,
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
        capabilities={capabilities({ can_discuss: false })}
        {...callbacks()}
      />,
    );
    expect(
      screen.queryByRole("button", { name: "Demander à Bridge" }),
    ).not.toBeInTheDocument();
  });
});
