import { expect, it } from "vitest";

import {
  PRODUCTION_STAGE_LABELS,
  PRODUCTION_STAGE_ORDER,
  RETRYABLE_PRODUCTION_STAGES,
} from "./productionStages";

it("exposes the ordered production stages and their labels", () => {
  expect(PRODUCTION_STAGE_ORDER).toEqual([
    "sources",
    "references",
    "extraction",
    "synthesis",
    "assembly",
  ]);
  expect(
    PRODUCTION_STAGE_ORDER.map((stage) => PRODUCTION_STAGE_LABELS[stage]),
  ).toEqual(["Sources", "Références", "Extraction", "Synthèse", "Assemblage"]);
  expect(RETRYABLE_PRODUCTION_STAGES).toBe(PRODUCTION_STAGE_ORDER);
});
