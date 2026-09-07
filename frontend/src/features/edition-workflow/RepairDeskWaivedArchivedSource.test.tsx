/**
 * LOT 35 — a waived source that was finally archived stays applicable.
 *
 * The payloads below are not hand-written: they are the exact JSON the backend
 * returned in `test_lot35_waived_source_rearchived.py` after the analyst waived
 * the source and then archived it. Building the object here by hand is what
 * hid the defect in the first place -- a frontend fixture can be made
 * consistent in a way the real read model was not.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import fixture from "../../test-utils/fixtures/lot35WaivedArchivedSource.json";
import type { EditionRepairPage, EditionReview } from "../../api/publication";
import { ReviewConsole } from "./ReviewConsole";

const REPAIR_PAGE = fixture.repair_page as unknown as EditionRepairPage;
const REVIEW = fixture.review as unknown as EditionReview;
const EDITION_ID = REVIEW.edition_id;

function stubServer() {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    if (url.includes("/review/repairs?")) {
      return Promise.resolve(Response.json(REPAIR_PAGE));
    }
    if (url.includes("/review/repairs/")) {
      return Promise.resolve(Response.json(REPAIR_PAGE.items[0]));
    }
    if (url.includes("/workbench")) {
      return Promise.resolve(
        Response.json({
          subject_id: REVIEW.items[0]?.subject_id,
          sources: [],
          claims: [],
          indicators: [],
        }),
      );
    }
    if (url.endsWith("/review")) return Promise.resolve(Response.json(REVIEW));
    return Promise.resolve(Response.json({}));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** A full page load: a brand-new QueryClient with no carried-over state. */
function mountConsole() {
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <ReviewConsole editionId={EDITION_ID} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Repair Desk — source waivée puis archivée", () => {
  it("expose bien le read model attendu", () => {
    const item = REPAIR_PAGE.items[0];
    expect(item?.effective_action).toBe("continue_without_source");
    expect(item?.repair_state).toBe("archived_pending_references");
    expect(item?.execution_plan.impact_kind).toBe("source_corpus");
    expect(REPAIR_PAGE.articles[0]?.execution_plan.impact_kind).toBe(
      "source_corpus",
    );
  });

  it("garde l’article, son action et son sous-texte après un rechargement", async () => {
    stubServer();

    mountConsole();
    expect(
      await screen.findByRole("button", { name: "Réintégrer la source" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Les références, l’extraction et la synthèse peuvent changer.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Appliquer 1 article" }),
    ).toBeInTheDocument();
    // The rebuild debt is what refuses the sign-off.
    expect(
      screen.getByRole("button", { name: "Accepter la production" }),
    ).toBeDisabled();

    // F5: everything React held in memory is gone.
    cleanup();
    mountConsole();

    expect(
      await screen.findByRole("button", { name: "Réintégrer la source" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Accepter la production" }),
    ).toBeDisabled();
  });
});
