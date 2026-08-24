import { describe, expect, it } from "vitest";

import { renderDiscoveryMarkdown } from "./discoveryMarkdownExport";
import type { CandidateTopic, SourceCandidate } from "./api/discovery";

const baseSource: SourceCandidate = {
  id: "source-1",
  url: "https://example.test/report",
  canonical_url: "https://example.test/report",
  raw_url: "https://example.test/report",
  local_ref: "P1",
  source_ref: "P1",
  title: "Rapport principal",
  publisher: "Vendor",
  role: "primary",
  published_at: "2026-05-12",
  event_date: null,
  citation: null,
  period_relation: "in_period",
  ioc_presence: "visible",
  ioc_declared_count: null,
  ioc_visible_count: 1,
  parsing_warnings: [],
  verification_status: "unverified",
  relationship_status: "provisional",
  verification_changed_at: null,
  verification_changed_by: null,
};

function candidate(overrides: Partial<CandidateTopic> = {}): CandidateTopic {
  return {
    id: "candidate-1",
    batch_id: "batch-1",
    title: "Campagne exemple",
    summary: "Résumé sur\nplusieurs lignes.",
    novelty: "n/a",
    technical_potential: 3,
    event_date: null,
    uncertainties: ["attribution provisoire"],
    relevance_reasons: [],
    actors: ["ExampleActor"],
    campaigns: [],
    malware: [],
    cves: [],
    victims: [],
    sectors: [],
    countries: [],
    likely_artifacts: ["ioc", "yara"],
    iocs: [],
    provisional_iocs: [
      {
        id: "ioc-1",
        raw_value: "203.0.113.5",
        normalized_value: null,
        declared_type: "ipv4",
        proposed_type: "ipv4",
        status: "provisional_visible",
        publication_refs: ["P1"],
        publication_ids: ["source-1"],
        warnings: [],
      },
    ],
    editorial_status: "proposed",
    sources: [baseSource],
    incomplete_sources: [
      {
        id: "incomplete-1",
        title: "Mention sans URL",
        publisher: "unknown",
        raw_url: null,
        local_ref: "P2",
        published_at: null,
        period_relation: "unknown",
        role: "unknown",
        ioc_presence: "none",
        ioc_declared_count: null,
        ioc_visible_count: null,
        parsing_warnings: [],
      },
    ],
    local_ref: "S1",
    actor_or_campaign: "ExampleActor",
    technical_potential_reason: "Chaîne documentée.",
    parsing_warnings: [],
    context_only: false,
    selectable: true,
    valid_publication_count: 1,
    incomplete_publication_count: 1,
    // Merge/consolidation-only fields — must NOT leak into the export.
    member_references: [{ batch_id: "batch-1", candidate_id: "candidate-1" }],
    contribution_count: 3,
    duplicate_publication_count: 2,
    merge_warnings: ["dropped a duplicate"],
    ...overrides,
  };
}

describe("renderDiscoveryMarkdown", () => {
  it("emits the SUBJECT/PUBLICATION schema the backend parser expects", () => {
    const markdown = renderDiscoveryMarkdown([candidate()]);

    expect(markdown).toContain("## SUBJECT S1");
    expect(markdown).toContain("title: Campagne exemple");
    // Multiline fields are collapsed so the parser doesn't mistake the
    // continuation for a new field.
    expect(markdown).toContain("presentation: Résumé sur plusieurs lignes.");
    expect(markdown).toContain("actor_or_campaign: ExampleActor");
    expect(markdown).toContain("technical_potential: 3");
    expect(markdown).toContain("artifacts: ioc, yara");
    expect(markdown).toContain("### PUBLICATION P1");
    expect(markdown).toContain("url: https://example.test/report");
    expect(markdown).toContain("visible_iocs: 203.0.113.5");
    // Incomplete sources round-trip too, with no URL so re-parsing keeps
    // them incomplete rather than dropping them.
    expect(markdown).toContain("### PUBLICATION P2");
    expect(markdown).toContain("url: \n");
  });

  it("never leaks merge/consolidation bookkeeping into the export", () => {
    const markdown = renderDiscoveryMarkdown([candidate()]);

    expect(markdown).not.toContain("member_references");
    expect(markdown).not.toContain("contribution_count");
    expect(markdown).not.toContain("duplicate_publication_count");
    expect(markdown).not.toContain("dropped a duplicate");
  });

  it("falls back to a generated subject ref when local_ref is missing", () => {
    const markdown = renderDiscoveryMarkdown([candidate({ local_ref: null })]);

    expect(markdown).toContain("## SUBJECT S1");
  });

  it("renumbers subjects and publications so consolidated batches never collide", () => {
    // After merging several research batches, local_ref is only unique
    // within its own batch — two merged subjects both carrying "S1" (and
    // sources both carrying "P1") is the normal, expected shape here.
    const first = candidate({
      id: "candidate-1",
      local_ref: "S1",
      title: "Sujet du lot A",
      sources: [
        { ...baseSource, id: "source-a", local_ref: "P1", title: "Source A" },
      ],
      incomplete_sources: [],
      provisional_iocs: [
        {
          id: "ioc-a",
          raw_value: "a.example.test",
          normalized_value: null,
          declared_type: "domain",
          proposed_type: "domain",
          status: "provisional_visible",
          publication_refs: ["P1"],
          publication_ids: ["source-a"],
          warnings: [],
        },
      ],
    });
    const second = candidate({
      id: "candidate-2",
      local_ref: "S1",
      title: "Sujet du lot B",
      sources: [
        { ...baseSource, id: "source-b", local_ref: "P1", title: "Source B" },
      ],
      incomplete_sources: [],
      provisional_iocs: [
        {
          id: "ioc-b",
          raw_value: "b.example.test",
          normalized_value: null,
          declared_type: "domain",
          proposed_type: "domain",
          status: "provisional_visible",
          publication_refs: ["P1"],
          publication_ids: ["source-b"],
          warnings: [],
        },
      ],
    });

    const markdown = renderDiscoveryMarkdown([first, second]);
    const subjectHeaders = markdown.match(/^## SUBJECT .+$/gm) ?? [];

    expect(subjectHeaders).toEqual(["## SUBJECT S1", "## SUBJECT S2"]);
    // Each subject's own IOC stays attached to its own (renumbered)
    // publication, not leaked across the collision.
    const subjectA = markdown.slice(
      markdown.indexOf("## SUBJECT S1"),
      markdown.indexOf("## SUBJECT S2"),
    );
    const subjectB = markdown.slice(markdown.indexOf("## SUBJECT S2"));
    expect(subjectA).toContain("visible_iocs: a.example.test");
    expect(subjectA).not.toContain("b.example.test");
    expect(subjectB).toContain("visible_iocs: b.example.test");
    expect(subjectB).not.toContain("a.example.test");
  });

  it("attaches IOCs by source id, not by the collision-prone local_ref, within one merged subject", () => {
    // Two sources consolidated into the SAME subject from different research
    // batches can independently carry local_ref "P1" — this is the exact
    // shape that caused IOCs to leak onto the wrong publication before
    // publication_ids existed.
    const base = baseSource;
    const merged = candidate({
      sources: [
        {
          ...base,
          id: "source-real",
          local_ref: "P1",
          title: "Vrai porteur des IOC",
        },
        {
          ...base,
          id: "source-decoy",
          local_ref: "P1",
          title: "Ne doit recevoir aucun IOC",
          url: "https://decoy.test/roundup",
          canonical_url: "https://decoy.test/roundup",
          ioc_presence: "unknown",
        },
      ],
      incomplete_sources: [],
      provisional_iocs: [
        {
          id: "ioc-1",
          raw_value: "only-for-real-source.example",
          normalized_value: null,
          declared_type: "domain",
          proposed_type: "domain",
          status: "provisional_visible",
          publication_refs: ["P1"],
          publication_ids: ["source-real"],
          warnings: [],
        },
      ],
    });

    const markdown = renderDiscoveryMarkdown([merged]);
    const publications = markdown.split(/^### PUBLICATION /m).slice(1);

    expect(publications).toHaveLength(2);
    expect(publications[0]).toContain("title: Vrai porteur des IOC");
    expect(publications[0]).toContain(
      "visible_iocs: only-for-real-source.example",
    );
    expect(publications[1]).toContain("title: Ne doit recevoir aucun IOC");
    expect(publications[1]).toContain("visible_iocs: none");
  });
});
