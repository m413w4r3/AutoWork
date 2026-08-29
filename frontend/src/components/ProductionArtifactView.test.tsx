import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { ProductionArtifactView } from "./ProductionArtifactView";

afterEach(() => vi.unstubAllGlobals());

it("construit la preview de publication depuis le JSON canonique", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      Response.json({
        artifact_id: "publication-1",
        stage: "publication",
        version: 1,
        status: "verified",
        metadata: {},
        rendered_content: '::: {custom-style="publication"}\ncontenu\n:::',
        canonical_content: {
          schema_version: "1",
          title: "[Cavern Manticore] Un framework modulaire",
          timeline: [],
          synthesis: [
            [
              { kind: "actor", text: "Cavern Manticore", source_ids: [] },
              { kind: "text", text: " utilise ", source_ids: [] },
              { kind: "tool", text: "WinDirStat", source_ids: [] },
            ],
          ],
          indicators: [
            {
              artifact_type: "domain",
              values: [
                {
                  value: "example[.]com",
                  normalized_value: "example.com",
                  artifact_type: "domain",
                  source_ids: ["S1"],
                },
              ],
            },
          ],
          sources: [],
          uncertainties: [],
        },
      }),
    ),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ProductionArtifactView subjectId="subject-1" stage="publication" />
    </QueryClientProvider>,
  );

  expect(
    await screen.findByRole("heading", {
      name: "[Cavern Manticore] Un framework modulaire",
    }),
  ).toBeInTheDocument();
  expect(screen.getByText("WinDirStat")).toHaveClass("semantic-tool");
  expect(screen.getByText("example.com")).toBeInTheDocument();
  expect(
    screen.getByRole("link", { name: "Télécharger le Markdown Pandoc" }),
  ).toHaveAttribute("download", "publication-pandoc.md");
  expect(screen.queryByText(/custom-style/)).not.toBeInTheDocument();
});
