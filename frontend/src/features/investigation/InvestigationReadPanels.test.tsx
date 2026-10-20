import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InvestigationReadPanels } from "./InvestigationReadPanels";

const INVESTIGATION_ID = "11111111-1111-4111-8111-111111111111";
const COMPARISON_ID = "22222222-2222-4222-8222-222222222222";
const MODEL_ID = "33333333-3333-4333-8333-333333333333";
const SHA256 = "a".repeat(64);

function mockResponse(url: string): Response {
  if (url.endsWith("/sample-comparisons")) {
    return Response.json({
      investigation_id: INVESTIGATION_ID,
      comparisons: [
        {
          blob_id: COMPARISON_ID,
          sha256: "b".repeat(64),
          schema_version: "family-feature-comparison-v1",
          status: "complete",
          sample_sha256s: [SHA256],
          sample_count: 1,
          feature_count: 1,
          limits: {},
          warnings: [],
        },
      ],
      truncated: false,
      omitted_comparisons: [
        {
          observation_id: "44444444-4444-4444-8444-444444444444",
          size_bytes: 70_843_573,
          reason: "exceeds_read_limit",
        },
      ],
    });
  }
  if (url.endsWith(`/sample-comparisons/${COMPARISON_ID}`)) {
    return Response.json({
      blob_id: COMPARISON_ID,
      sha256: "b".repeat(64),
      schema_version: "family-feature-comparison-v1",
      investigation_id: INVESTIGATION_ID,
      corpus_snapshot_sha256: "c".repeat(64),
      status: "complete",
      sample_sha256s: [SHA256],
      sample_count: 1,
      features: [
        {
          feature_key: "d".repeat(64),
          kind: "api",
          value: "CreateFileW",
          encoding: null,
          feature_class: "core_strongly_shared",
          rank: 1,
          support: {
            positive: 1,
            positive_sample_count: 1,
            corpus: 1,
            corpus_sample_count: 1,
            goodware_occurrence_count: 3,
            goodware_prevalence_status: "measured",
            comparison_exclusive: true,
          },
          sample_presence: [
            {
              sample_sha256: SHA256,
              present: true,
              measured: true,
              selected: true,
              qualification: "confirmed",
              offsets: [16],
              function_offsets: [],
              sources: [{ source_kind: "pe_imports", extractor_version: "2" }],
            },
          ],
          rejection_reasons: [],
          rationale: ["Mesuré dans l’échantillon sélectionné."],
        },
      ],
      feature_count: 1,
      features_truncated: false,
      limits: {},
      provenance: { extractor_versions: [], policy: {} },
      warnings: [],
      recommended_next_tests: [],
    });
  }
  if (url.endsWith("/hypotheses")) {
    return Response.json({
      investigation_id: INVESTIGATION_ID,
      hypotheses: [
        {
          id: "hypothesis-c2",
          statement: "Le client contacte le serveur observé.",
          status: "open",
          findings: [
            {
              id: "finding-refuted",
              kind: "network_behaviour",
              assessment: "refuted",
              statement:
                "La capture de contrôle ne montre pas cette connexion.",
              observation_refs: [],
              limitations: [],
              contradictory: true,
            },
          ],
          observations: [],
          contradictions: [
            {
              id: "finding-refuted",
              kind: "network_behaviour",
              assessment: "refuted",
              statement:
                "La capture de contrôle ne montre pas cette connexion.",
              observation_refs: [],
              limitations: [],
              contradictory: true,
            },
          ],
        },
      ],
    });
  }
  if (url.endsWith("/protocol-models")) {
    return Response.json({
      investigation_id: INVESTIGATION_ID,
      models: [
        {
          blob_id: MODEL_ID,
          sha256: "e".repeat(64),
          schema_version: "ProtocolModelV1",
          model_version: "1",
          protocol: "protocole-échantillon",
          transport: "tcp",
          validation_status: "observed",
          family_assurance: "medium",
          sample_sha256s: [SHA256],
          message_count: 1,
          field_count: 1,
          transition_count: 0,
        },
      ],
      truncated: false,
    });
  }
  if (url.endsWith(`/protocol-models/${MODEL_ID}`)) {
    return Response.json({
      blob_id: MODEL_ID,
      sha256: "e".repeat(64),
      model: {
        protocol: "protocole-échantillon",
        transport: "tcp",
        validation_status: "observed",
        messages: [
          {
            message_id: "m1",
            name: "Handshake observé",
            validation_status: "observed",
            min_length: 4,
            max_length: 4,
          },
        ],
        fields: [
          {
            field_id: "f1",
            name: "Longueur",
            validation_status: "inferred",
            offset: 0,
            length: 2,
            endianness: "big",
          },
        ],
        transitions: [],
        limitations: ["La charge utile reste opaque."],
      },
      truncated: {},
    });
  }
  if (url.endsWith(`/protocol-models/${MODEL_ID}/diagrams`)) {
    const svg = "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=";
    const png = "data:image/png;base64,iVBORw0KGgo=";
    return Response.json({
      investigation_id: INVESTIGATION_ID,
      model_blob_id: MODEL_ID,
      model_sha256: "e".repeat(64),
      diagrams: ["sequence", "state_machine", "message_format"].map((kind) => ({
        kind,
        status: kind === "state_machine" ? "insufficient_evidence" : "ready",
        title: `Vue ${kind}`,
        limitations: [],
        asset_sha256: "f".repeat(64),
        svg: {
          mime_type: "image/svg+xml",
          sha256: "f".repeat(64),
          data_url: svg,
        },
        png_preview: {
          mime_type: "image/png",
          sha256: "1".repeat(64),
          data_url: png,
        },
      })),
    });
  }
  if (url.endsWith("/retrohunt")) {
    return Response.json({
      investigation_id: INVESTIGATION_ID,
      subject_id: "44444444-4444-4444-8444-444444444444",
      maximum_slots: 2,
      consumed_slots: 1,
      remaining_slots: 1,
      jobs: [
        {
          reservation_id: "55555555-5555-4555-8555-555555555555",
          slot: 1,
          status: "submitted",
          version: 2,
          updated_at: "2026-10-14T10:00:00Z",
          has_provider_job: true,
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

describe("InvestigationReadPanels", () => {
  it("affiche comparaisons, contradictions, prévisualisations et compteurs bornés", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
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
        <InvestigationReadPanels investigationId={INVESTIGATION_ID} />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("CreateFileW")).toBeInTheDocument();
    expect(
      await screen.findByText(/Comparaison trop volumineuse, omise/),
    ).toBeInTheDocument();
    expect(screen.getByText("3 occurrence(s) mesurée(s)")).toBeInTheDocument();
    expect(
      await screen.findByText("Contradictions conservées"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Éléments insuffisants : aucune séquence générique n’est présentée comme observée.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: "Machine à états : Vue state_machine" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("1/2 emplacements consommés"),
    ).toBeInTheDocument();
  });
});
