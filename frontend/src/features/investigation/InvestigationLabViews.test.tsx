import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { InvestigationView } from "../../api/investigation";
import { InvestigationLabViews } from "./InvestigationLabViews";

const INVESTIGATION_ID = "11111111-1111-4111-8111-111111111111";
const CLAIM_ID = "claim-c2-tunnel";
const OBSERVATION_ID = "22222222-2222-4222-8222-222222222222";
const CANDIDATE_ID = "a".repeat(64);
const SAMPLE_SHA256 = "b".repeat(64);

const investigation: InvestigationView = {
  id: INVESTIGATION_ID,
  subject_id: "33333333-3333-4333-8333-333333333333",
  production_run_id: "44444444-4444-4444-8444-444444444444",
  synthesis_artifact_id: "55555555-5555-4555-8555-555555555555",
  synthesis_sha256: "c".repeat(64),
  status: "completed",
  version: 7,
  stop_reason: null,
  agenda: [],
  hypotheses: [
    {
      id: "hypothesis-c2",
      statement: "Le client ouvre un tunnel C2.",
      status: "open",
      evidence_refs: [OBSERVATION_ID],
      finding_refs: [],
    },
  ],
  findings: [],
  observations: [
    {
      id: OBSERVATION_ID,
      kind: "binary_analysis",
      sha256: "d".repeat(64),
      source: "ghidra",
      source_version: "11.2",
      observed_at: "2026-10-18T10:00:00Z",
      object_type: "sample",
      object_id: SAMPLE_SHA256,
      limitations: ["Analyse statique uniquement."],
      invocation_ref: null,
    },
  ],
  sample_associations: [
    {
      id: "66666666-6666-4666-8666-666666666666",
      sample_sha256: SAMPLE_SHA256,
      origin: "seed",
      qualification: "candidate",
      related_sample_sha256: [],
      version: 1,
    },
  ],
  opinion_draft: null,
  budget: {
    bridge_turns: { limit: 10, consumed: 4, reserved: 0, remaining: 6 },
    vt_units: { limit: 0, consumed: 0, reserved: 0, remaining: 0 },
    shodan_credits: { limit: 0, consumed: 0, reserved: 0, remaining: 0 },
    downloads: { limit: 0, consumed: 0, reserved: 0, remaining: 0 },
    tool_seconds: { limit: 120, consumed: 15, reserved: 0, remaining: 105 },
    context_tokens: {
      limit: 20_000,
      consumed: 5_000,
      reserved: 0,
      remaining: 15_000,
    },
    context_bytes: {
      limit: 262_144,
      consumed: 80_000,
      reserved: 0,
      remaining: 182_144,
    },
  },
  blocked_decisions: [],
  available_tools: [],
  unavailable_tools: [],
  unavailable_tool_reasons: {},
  envelope_approved: true,
  allowed_providers: [],
  acquire_hits_allowed: false,
};

function mockResponse(url: string): Response {
  if (url.includes("/operator/context/section/article_claims")) {
    return Response.json({
      schema: "investigation-operator-context-section.v1",
      investigation_id: INVESTIGATION_ID,
      section: "article_claims",
      offset: 0,
      limit: 20,
      total: 1,
      next_offset: null,
      items: [
        {
          id: CLAIM_ID,
          statement: "Le client contacte le serveur C2 par tunnel.",
          source_refs: ["article:p4:claim-12", "source:report-17"],
        },
      ],
    });
  }
  if (url.endsWith("/experiments")) {
    return Response.json({
      schema_version: "investigation-experiment-timeline.v1",
      investigation_id: INVESTIGATION_ID,
      stream_version: 2,
      experiments: [
        {
          schema_version: "investigation-experiment.v1",
          id: "77777777-7777-4777-8777-777777777777",
          investigation_id: INVESTIGATION_ID,
          version: 1,
          hypothesis_id: "hypothesis-c2",
          claim_ids: [CLAIM_ID],
          working_state_version: 6,
          g2_envelope_version: 2,
          prediction: "Une capture montrera une connexion au serveur cité.",
          falsification_criterion:
            "Aucune connexion dans une capture complète.",
          method: "Comparer le flux DNS observé aux références du rapport.",
          method_mode: "passive",
          input_type: "network_capture",
          input_refs: [
            {
              input_type: "pcap",
              input_id: "capture-01",
              sha256: SAMPLE_SHA256,
            },
          ],
          preconditions: ["Capture complète et horodatée."],
          availability: "available",
          tool_name: "protocol.validate",
          tool_version: "2.1.0",
          tool_schema_version: "protocol-validation.v1",
          tool_arguments: { split: "holdout", source: "offline_capture" },
          step_ids: ["step-1"],
          tool_invocation_refs: [
            {
              invocation_id: "88888888-8888-4888-8888-888888888888",
              version: 1,
            },
          ],
          observation_refs: [
            { observation_id: OBSERVATION_ID, sha256: "d".repeat(64) },
          ],
          expected: "Connexion DNS puis ouverture du flux C2.",
          actual:
            "Aucune connexion au serveur cité dans la capture de contrôle.",
          actual_outcome: "not_observed",
          observation_coverage: "complete",
          started_at: "2026-10-18T10:00:00Z",
          completed_at: "2026-10-18T10:01:00Z",
          recorded_at: "2026-10-18T10:02:00Z",
          scope: "Capture de contrôle, pas le trafic de la victime.",
          limitations: ["Ne décrit pas les communications hors capture."],
          verdict: "refutes",
          verdict_assertion_label:
            "Verdict enregistré : refutes (assertion, non conclusion)",
          implication: "Cette capture ne confirme pas le serveur indiqué.",
          next_pivots: [CANDIDATE_ID],
          author: "analyst-1",
          origin: "operator_tool",
        },
        {
          schema_version: "investigation-experiment.v1",
          id: "99999999-9999-4999-8999-999999999999",
          investigation_id: INVESTIGATION_ID,
          version: 2,
          hypothesis_id: "hypothesis-c2",
          claim_ids: [CLAIM_ID],
          working_state_version: 7,
          g2_envelope_version: 2,
          prediction: "Une résolution du domaine apparaîtra dans les journaux.",
          falsification_criterion:
            "Aucune résolution dans la fenêtre capturée.",
          method:
            "Vérifier passivement les requêtes DNS de la seconde capture.",
          method_mode: "passive",
          input_type: "network_capture",
          input_refs: [
            {
              input_type: "pcap",
              input_id: "capture-02",
              sha256: "e".repeat(64),
            },
          ],
          preconditions: [],
          availability: "limited",
          tool_name: "protocol.validate",
          tool_version: "2.1.0",
          tool_schema_version: "protocol-validation.v1",
          tool_arguments: { split: "train" },
          step_ids: [],
          tool_invocation_refs: [],
          observation_refs: [
            { observation_id: OBSERVATION_ID, sha256: "d".repeat(64) },
          ],
          expected: "DNS lookup observed.",
          actual: "No lookup observed in the available window.",
          actual_outcome: "not_observed",
          observation_coverage: "limited",
          started_at: "2026-10-18T11:00:00Z",
          completed_at: "2026-10-18T11:01:00Z",
          recorded_at: "2026-10-18T11:02:00Z",
          scope: "Fenêtre partielle de capture.",
          limitations: ["Une partie du trafic manque."],
          verdict: "supports",
          implication: "La capture partielle n’exclut pas le comportement.",
          next_pivots: [],
          author: "analyst-1",
          origin: "operator_tool",
        },
      ],
      claims: [
        {
          claim_id: CLAIM_ID,
          status: "qualified",
          verdicts: ["refutes", "supports"],
          experiment_ids: [
            "77777777-7777-4777-8777-777777777777",
            "99999999-9999-4999-8999-999999999999",
          ],
          scopes: [],
          contradictory: true,
        },
      ],
    });
  }
  if (url.endsWith("/hypotheses")) {
    return Response.json({
      investigation_id: INVESTIGATION_ID,
      hypotheses: [],
    });
  }
  if (url.includes("/pivots?")) {
    return Response.json({
      schema_version: "investigation-pivot-frontier.v1",
      investigation_id: INVESTIGATION_ID,
      source_sha256: "c".repeat(64),
      frontier_sha256: "f".repeat(64),
      nodes: [
        {
          id: CANDIDATE_ID,
          canonical_id: "domain:next.example",
          indicator_type: "domain",
          value: "next.example",
          parent_id: null,
          path: ["source", "next.example"],
          source_observation_id: OBSERVATION_ID,
          source: "protocol.validate",
          source_version: "2.1.0",
          source_sha256: "d".repeat(64),
          level: "candidate",
          reason:
            "Domaine extrait du certificat observé dans le flux candidat.",
          confidence: 0.62,
          limitations: [
            "Domaine absent de l’article; non confirmé par un échantillon.",
          ],
          novelty_vs_article: true,
          novelty_vs_verified: true,
          estimated_cost: 2,
          g2_state: "approved",
          disclosure_state: "allowed",
          priority_score: 71,
          priority_factors: {
            novelty_vs_article: 1,
            novelty_vs_verified: 1,
            depth: 2,
            cost: 2,
            formula: "test formula",
          },
          state: "queued",
          state_reason: "En attente de consultation passive.",
          cursor: null,
          pagination_complete: null,
          children_observed: [],
        },
      ],
      edges: [],
      paths: [
        {
          id: "1".repeat(64),
          root_id: CANDIDATE_ID,
          target_id: CANDIDATE_ID,
          node_ids: [CANDIDATE_ID],
          edge_ids: [],
        },
      ],
      truncated: false,
      next_cursor: null,
    });
  }
  if (url.endsWith("/detections")) {
    return Response.json({
      investigation_id: INVESTIGATION_ID,
      versions: [
        {
          candidate_id: "candidate-1",
          version_id: "version-1",
          version: 1,
          parent_version_id: null,
          investigation_id: INVESTIGATION_ID,
          source: "rule demo { condition: true }",
          source_sha256: "1".repeat(64),
          status: "candidate",
          validation_status: "real_partial",
          created_at: "2026-10-18T12:00:00Z",
          test_reports: (() => {
            const latestReport = {
              observation_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
              sha256: "2".repeat(64),
              source_sha256: "1".repeat(64),
              corpus_sha256: "3".repeat(64),
              corpus_sha256s_by_role: {},
              engine_version: "4.5.0",
              execution_status: "ok",
              validation_status: "real_partial",
              selection_bias: "Sélection manuelle.",
              coverage_statement: "Corpus réduit.",
              provenance_counts_by_role: {},
              false_positive_count: 0,
              false_positive_sha256s: [],
              false_negative_count: 0,
              false_negative_sha256s: [],
              roles: [
                {
                  role: "confirmed_positive",
                  corpus_sha256s: [],
                  test_cases: [],
                  matches: [],
                  non_matches: [],
                  non_measurable: [],
                  errors: [],
                },
                {
                  role: "out_of_cluster_candidate",
                  corpus_sha256s: [],
                  test_cases: [],
                  matches: [],
                  non_matches: [],
                  non_measurable: [],
                  errors: [],
                },
                {
                  role: "goodware",
                  corpus_sha256s: [],
                  test_cases: [],
                  matches: [],
                  non_matches: [],
                  non_measurable: [],
                  errors: [],
                },
                {
                  role: "holdout",
                  corpus_sha256s: [],
                  test_cases: [],
                  matches: [],
                  non_matches: [],
                  non_measurable: [],
                  errors: [],
                },
              ],
              limitations: ["Aucun positif certifié n’a été mesuré."],
              known_false_positives: [],
              known_false_positive_status: "not_tested",
              goodware_case_count: 0,
              metrics: {
                real_positive_sensitivity: {
                  status: "not_calculable",
                  numerator: 0,
                  denominator: 0,
                  estimate: null,
                  confidence_interval_95: null,
                  method: "wilson_score_two_sided_95",
                },
              },
              match_interpretation:
                "Un match est une correspondance d’octets, pas une confirmation de famille.",
              statistics_interpretation:
                "L’intervalle est un score de Wilson à 95 %.",
            };
            return [
              {
                ...latestReport,
                observation_id: "99999999-9999-4999-8999-999999999999",
                execution_status: "engine_crashed",
              },
              latestReport,
            ];
          })(),
          latest_decision: null,
          comparisons: [],
        },
      ],
    });
  }
  return Response.json(
    { detail: { code: "unexpected_request" } },
    { status: 500 },
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("InvestigationLabViews", () => {
  it("suit une affirmation contradictoire jusqu’au prochain pivot", async () => {
    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false, refetchInterval: false, staleTime: Infinity },
      },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url;
        return Promise.resolve(mockResponse(url));
      }),
    );
    render(
      <QueryClientProvider client={client}>
        <InvestigationLabViews
          investigation={investigation}
          editionId="edition-1"
        />
      </QueryClientProvider>,
    );

    expect(
      await screen.findByRole("heading", {
        name: "Le client contacte le serveur C2 par tunnel.",
      }),
    ).toBeInTheDocument();
    expect(screen.getAllByText(/article:p4:claim-12/).length).toBeGreaterThan(
      0,
    );
    expect(
      screen.getByText(/Des expériences soutiennent et réfutent/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Capture de contrôle, pas le trafic de la victime/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Fenêtre partielle de capture/),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Ouvrir le pivot/ }));

    expect(
      await screen.findByRole("heading", { name: "domain · next.example" }),
    ).toBeInTheDocument();
    expect(
      screen.getAllByText(/Domaine extrait du certificat observé/).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByRole("heading", { name: "Candidats non confirmés" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/coût estimé 2/)).toBeInTheDocument();
  });

  it("marque une expérience conservée dans l’historique comme remplacée", async () => {
    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false, refetchInterval: false, staleTime: Infinity },
      },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url;
        const response = mockResponse(url);
        if (!url.endsWith("/experiments")) return response;
        const timeline = (await response.json()) as {
          experiments: Array<{ id: string; superseded_by?: string[] }>;
          claims: Array<Record<string, unknown>>;
        };
        const previous = timeline.experiments[0];
        const replacement = timeline.experiments[1];
        const claim = timeline.claims[0];
        if (!previous || !replacement || !claim) {
          throw new Error("The experiment fixture is incomplete");
        }
        previous.superseded_by = [replacement.id];
        timeline.claims[0] = {
          ...claim,
          status: "supported",
          verdicts: ["supports"],
          experiment_ids: [replacement.id],
          contradictory: false,
        };
        return Response.json(timeline);
      }),
    );
    render(
      <QueryClientProvider client={client}>
        <InvestigationLabViews
          investigation={investigation}
          editionId="edition-1"
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByText(/Remplacée par/)).toBeInTheDocument();
    expect(
      screen.getByText("99999999-9999-4999-8999-999999999999"),
    ).toBeInTheDocument();
    expect(screen.getByText(/Expérience v1 · Réfute/)).toBeInTheDocument();
    expect(
      screen.getByText("Verdict enregistré : refutes (assertion, non conclusion)"),
    ).toHaveAttribute("role", "note");
    expect(screen.getByText(/Expérience v2 · Soutient/)).toBeInTheDocument();
  });

  it("affiche explicitement une sensibilité non calculable et les limites YARA", async () => {
    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false, refetchInterval: false, staleTime: Infinity },
      },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url;
        return Promise.resolve(mockResponse(url));
      }),
    );
    render(
      <QueryClientProvider client={client}>
        <InvestigationLabViews
          investigation={investigation}
          editionId="edition-1"
        />
      </QueryClientProvider>,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Réseau & détections" }),
    );

    expect(
      (await screen.findAllByText(/Sensibilité sur positifs réels/)).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByText(/Non calculable · 0\/0 mesuré/).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByText(/Les faux positifs goodware ne sont pas évalués/)
        .length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByText(/pas une confirmation de famille/).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByText(/Dernier résultat pertinent · ok/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Essai historique · engine_crashed/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /Candidate · version 1/ }),
    ).toBeInTheDocument();
  });
});
