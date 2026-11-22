import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useReplaceFigureFlow } from "./useReplaceFigureFlow";
import {
  ANCHOR_ONE_ID,
  figureBlock,
  paragraphBlock,
  visualSnapshot,
} from "./visualTestFixtures";

function snapshotWithManualEdit(manualEdit: boolean) {
  const snapshot = visualSnapshot({
    blocks: [
      paragraphBlock(ANCHOR_ONE_ID, "Paragraphe d’ancrage synthétique."),
      figureBlock({
        blockId: "figure-block",
        elementKey: "figure-a",
        caption: "Figure A",
        order: 0,
      }),
    ],
  });
  return {
    ...snapshot,
    capabilities: { ...snapshot.capabilities, manual_edit: manualEdit },
  };
}

describe("useReplaceFigureFlow", () => {
  it("refuses the entry when manual_edit is false instead of opening a dead dialog", () => {
    const { result } = renderHook(() =>
      useReplaceFigureFlow("subject-visual", snapshotWithManualEdit(false)),
    );

    act(() => result.current.requestReplace("figure-a"));

    expect(result.current.dialog).toBeNull();
    expect(result.current.unresolvedError).toBe(
      "La préparation des révisions visuelles n’est pas activée pour cet atelier. Aucune modification n’est possible.",
    );
  });
});
