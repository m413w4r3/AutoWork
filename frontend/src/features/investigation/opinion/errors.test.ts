import { describe, expect, it } from "vitest";

import { InvestigationApiError } from "../../../api/investigation";
import { WorkshopApiError } from "../api/client";
import { describeApiError, isAnchorReloadError } from "./errors";

/**
 * Refusals that invalidate the displayed anchor space or its pinned base: the
 * message must be French and carry the reload action.
 */
const ANCHOR_RELOAD_CASES: Array<[string, RegExp]> = [
  ["invalid_note_anchor", /Rechargez les ancres/],
  ["invalid_observation_anchor", /Rechargez les ancres/],
  ["note_anchor_text_changed", /Rechargez les ancres/],
  ["note_block_ref_stale", /Rechargez les ancres/],
  ["note_code_ref_stale", /Rechargez la proposition/],
  ["stale_base", /Rechargez les ancres/],
];

/** B5a note-content refusals: readable French plus the reload action. */
const NOTE_CONTENT_CASES: Array<[string, RegExp]> = [
  ["note_chart_without_evidence", /ne porte pas de preuve/],
  ["note_figure_source_missing", /n’a pas de source connue/],
  ["note_ioc_ref_unresolved", /ne résout aucune observation/],
];

describe("errors — refus d’ancre canonique", () => {
  it.each(ANCHOR_RELOAD_CASES)(
    "traduit %s et demande le rechargement des ancres",
    (code, expected) => {
      const error = new WorkshopApiError("backend message", 409, code);

      const described = describeApiError(error);
      expect(described.code).toBe(code);
      expect(described.status).toBe(409);
      expect(described.message).toMatch(expected);
      expect(isAnchorReloadError(error)).toBe(true);
    },
  );

  it.each(NOTE_CONTENT_CASES)(
    "traduit %s avec l’action de rechargement",
    (code, expected) => {
      const error = new InvestigationApiError("backend message", code, 422);

      const described = describeApiError(error);
      expect(described.code).toBe(code);
      expect(described.message).toMatch(expected);
      expect(described.message).toMatch(/Rechargez la proposition/);
      expect(isAnchorReloadError(error)).toBe(false);
    },
  );

  it("ne demande aucun rechargement d’ancre pour une attente Bridge", () => {
    const error = new WorkshopApiError(
      "backend message",
      202,
      "bridge_response_pending",
    );
    expect(isAnchorReloadError(error)).toBe(false);
  });
});
