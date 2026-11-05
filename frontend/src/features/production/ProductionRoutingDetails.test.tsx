import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ProductionRoutingProjection } from "../../api/production";
import { ProductionRoutingDetails } from "./ProductionRoutingDetails";

function routing(
  overrides: Partial<ProductionRoutingProjection> = {},
): ProductionRoutingProjection {
  return {
    status: "decided",
    publication_kind: "article",
    reason_code: "subject_ioc_confirmed",
    policy_version: "routing-policy-v1",
    confirmed_ioc_count: 2,
    contextual_ioc_count: 0,
    ambiguous_ioc_count: 0,
    extraction_verification_state: "verified",
    sources: [],
    ...overrides,
  };
}

describe("ProductionRoutingDetails", () => {
  it("indicates that the format is undetermined when no routing decision exists", () => {
    render(<ProductionRoutingDetails />);

    expect(screen.getByText("Format : à déterminer")).toBeInTheDocument();
  });

  it.each([
    ["article", "Article"],
    ["brief", "Brève"],
  ] as const)("shows a decided %s format", (publicationKind, label) => {
    render(
      <ProductionRoutingDetails
        routing={routing({
          publication_kind: publicationKind,
          sources: [
            {
              source_id: "source-1",
              source_url: "https://source.example/report",
              failure_code: "critical_source_incomplete",
              tier: "core",
              criticality: "critical",
              criticality_reason: "required_source_missing",
              policy_version: "source-policy-v1",
            },
          ],
        })}
      />,
    );

    expect(screen.getByText(`Format : ${label}`)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Détails du format"));
    expect(screen.getByText("subject_ioc_confirmed")).toBeInTheDocument();
    expect(screen.getByText("routing-policy-v1")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "https://source.example/report" }),
    ).toHaveAttribute("href", "https://source.example/report");
  });

  it("marks a route that needs review without predicting a publication kind", () => {
    render(
      <ProductionRoutingDetails
        routing={routing({
          status: "needs_review",
          publication_kind: null,
          reason_code: "incomplete_extraction",
          confirmed_ioc_count: 0,
        })}
      />,
    );

    expect(
      screen.getByText("Format : Qualification à revoir"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByText("Détails du format"));
    expect(screen.getByText("incomplete_extraction")).toBeInTheDocument();
    expect(screen.getByText("0")).toBeInTheDocument();
  });
});
