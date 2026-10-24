import type { Page, Route } from "@playwright/test";

const ids = {
  edition: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  subject: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  run: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
  synthesis: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  investigation: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
  opinion: "ffffffff-ffff-4fff-8fff-ffffffffffff",
  finding: "11111111-1111-4111-8111-111111111111",
  evidence: "22222222-2222-4222-8222-222222222222",
  association: "55555555-5555-4555-8555-555555555555",
  publication: "33333333-3333-4333-8333-333333333333",
} as const;

const hash = (letter: string) => letter.repeat(64);

interface OpinionPayload {
  schema: "analyst-opinion.v1";
  id: string;
  version: number;
  investigation_id: string;
  synthesis_artifact_id: string;
  synthesis_sha256: string;
  base_publication_artifact_id: string;
  base_publication_sha256: string;
  blocks: Array<{
    type: "key_findings";
    text: string;
    finding_refs: string[];
    evidence_refs: string[];
    qualification: "fact" | "inference" | "hypothesis";
    certainty: number;
  }>;
  unresolved_points: string[];
  author_id: string;
  reviewer_id: string | null;
  status: "draft" | "approved" | "rejected";
  created_at: string;
  content_sha256: string;
  renderable_blocks: OpinionPayload["blocks"];
  provenance: {
    investigation_id: string;
    synthesis_artifact_id: string;
    synthesis_sha256: string;
    base_publication_artifact_id: string;
    base_publication_sha256: string;
  };
}

interface InvestigationPayload {
  id: string;
  subject_id: string;
  production_run_id: string;
  synthesis_artifact_id: string;
  synthesis_sha256: string;
  status: "running";
  version: number;
  stop_reason: null;
  agenda: [];
  hypotheses: [];
  findings: Array<{
    id: string;
    version: number;
    kind: "new_sample";
    assessment: "supported";
    novelty: "new_to_article";
    statement: string;
    scope: string;
    limitations: string[];
    observation_refs: string[];
    claim_refs: [];
    created_at: string;
  }>;
  observations: Array<{
    id: string;
    kind: "vt_relationship";
    sha256: string;
    source: string;
    source_version: string;
    observed_at: string;
    object_type: string;
    object_id: string;
    limitations: string[];
    invocation_ref: null;
  }>;
  sample_associations: Array<{
    id: string;
    sample_sha256: string;
    origin: "seed" | "vt_pivot" | "relationship" | "behavior" | "manual";
    qualification:
      "candidate" | "related" | "confirmed" | "rejected" | "uncertain";
    related_sample_sha256: string[];
    version: number;
  }>;
  opinion_draft: null | {
    id: string;
    version: number;
    status: "draft" | "approved" | "rejected";
    content_sha256: string;
    blocks: OpinionPayload["blocks"];
    unresolved_points: string[];
    created_at: string;
  };
  budget: Record<
    | "bridge_turns"
    | "vt_units"
    | "shodan_credits"
    | "downloads"
    | "tool_seconds"
    | "context_tokens"
    | "context_bytes",
    { limit: number | null; consumed: number; remaining: number | null }
  >;
  blocked_decisions: [];
  available_tools: ["article.get_claims"];
  unavailable_tools: ["shodan.host"];
  unavailable_tool_reasons: Record<string, string>;
  envelope_approved: false;
  allowed_providers: [];
  acquire_hits_allowed: false;
}

export function createAnalystInvestigationApi() {
  const requests: Array<{
    method: string;
    path: string;
    body: unknown;
    idempotencyKey: string | null;
  }> = [];
  let investigation: InvestigationPayload | null = null;
  let opinion: OpinionPayload | null = null;
  let envelope = {
    investigation_id: ids.investigation,
    approved: false,
    version: 0,
    allowed_providers: [] as string[],
    available_providers: ["vt", "shodan"],
    acquire_hits_allowed: false,
    max_acquire_volume: 0,
    budget: {
      bridge_turns: { limit: 8, consumed: 1, remaining: 7 },
      vt_units: { limit: 20, consumed: 1, remaining: 19 },
      shodan_credits: { limit: 5, consumed: 0, remaining: 5 },
      downloads: { limit: 0, consumed: 0, remaining: 0 },
      tool_seconds: { limit: 300, consumed: 1, remaining: 299 },
      context_tokens: { limit: 20_000, consumed: 500, remaining: 19_500 },
      context_bytes: { limit: 100_000, consumed: 3_000, remaining: 97_000 },
    },
    disclosure: {
      tlp: "amber" as "amber" | null,
      do_not_submit: true,
      external_llm_allowed: false,
    },
    approval_decision_id: null as string | null,
    approval_actor_id: null as string | null,
    approved_at: null as string | null,
  };
  let sample = {
    id: ids.association,
    sample_sha256: hash("d"),
    origin: "vt_pivot" as const,
    qualification: "candidate" as
      "candidate" | "related" | "confirmed" | "rejected" | "uncertain",
    related_sample_sha256: [hash("a")],
    version: 1,
    changes: [] as Array<{
      from_qualification:
        "candidate" | "related" | "confirmed" | "rejected" | "uncertain";
      to_qualification:
        "candidate" | "related" | "confirmed" | "rejected" | "uncertain";
      actor_kind: "human";
      actor_id: string;
      reason: string;
      changed_at: string;
    }>,
  };

  function opinionPayload(): OpinionPayload | null {
    return opinion;
  }

  function opinionResponse() {
    return {
      draft: opinionPayload(),
      history: opinion ? [opinion] : [],
      citation_violations: [],
      renderable_blocks: opinion?.renderable_blocks ?? [],
      provenance: opinion?.provenance ?? null,
    };
  }

  function toInvestigationView(): InvestigationPayload | null {
    if (!investigation) return null;
    return {
      ...investigation,
      opinion_draft: opinion
        ? {
            id: opinion.id,
            version: opinion.version,
            status: opinion.status,
            content_sha256: opinion.content_sha256,
            blocks: opinion.blocks,
            unresolved_points: opinion.unresolved_points,
            created_at: opinion.created_at,
          }
        : null,
    };
  }

  function summary() {
    if (!investigation) return [];
    return [
      {
        id: investigation.id,
        subject_id: investigation.subject_id,
        production_run_id: investigation.production_run_id,
        synthesis_artifact_id: investigation.synthesis_artifact_id,
        synthesis_sha256: investigation.synthesis_sha256,
        status: investigation.status,
        version: investigation.version,
        stop_reason: investigation.stop_reason,
        updated_at: "2026-10-13T10:00:00Z",
      },
    ];
  }

  function newOpinion(
    blocks: OpinionPayload["blocks"],
    version: number,
    status: OpinionPayload["status"],
  ): OpinionPayload {
    const contentHash = hash(String.fromCharCode(97 + version));
    const provenance = {
      investigation_id: ids.investigation,
      synthesis_artifact_id: ids.synthesis,
      synthesis_sha256: hash("a"),
      base_publication_artifact_id: ids.publication,
      base_publication_sha256: hash("b"),
    };
    return {
      schema: "analyst-opinion.v1",
      id: ids.opinion,
      version,
      investigation_id: ids.investigation,
      synthesis_artifact_id: ids.synthesis,
      synthesis_sha256: hash("a"),
      base_publication_artifact_id: ids.publication,
      base_publication_sha256: hash("b"),
      blocks,
      unresolved_points: [],
      author_id: "analyst:dev",
      reviewer_id: status === "approved" ? "dev-analyst" : null,
      status,
      created_at: "2026-10-13T10:00:00Z",
      content_sha256: contentHash,
      renderable_blocks: blocks.filter((block) => block.text.trim()),
      provenance,
    };
  }

  async function handle(route: Route) {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    let body: unknown = null;
    if (request.postData()) {
      try {
        body = request.postDataJSON() as unknown;
      } catch {
        body = request.postData();
      }
    }
    const idempotencyKey = request.headers()["idempotency-key"] ?? null;
    requests.push({ method, path, body, idempotencyKey });

    if (path === `/api/subjects/${ids.subject}` && method === "GET") {
      return fulfill(route, {
        id: ids.subject,
        edition_id: ids.edition,
        title: "Sujet de test CTI",
        slug: "sujet-test-cti",
        tlp: "AMBER",
        version: 1,
        created_at: "2026-10-13T09:00:00Z",
        updated_at: "2026-10-13T09:00:00Z",
      });
    }
    if (path === `/api/subjects/${ids.subject}/content` && method === "GET") {
      return fulfill(
        route,
        { detail: { code: "subject_content_not_found" } },
        404,
      );
    }
    if (
      path === `/api/subjects/${ids.subject}/production/artifacts/synthesis` &&
      method === "GET"
    ) {
      return fulfill(route, {
        artifact_id: ids.synthesis,
        production_run_id: ids.run,
        stage: "synthesis",
        version: 4,
        input_hash: hash("c"),
        canonical_sha256: hash("a"),
        status: "verified",
        metadata: {},
        canonical_content: { schema_version: 2 },
        caption: "Synthèse vérifiée",
        provenance: "fixture",
      });
    }
    if (
      path === `/api/subjects/${ids.subject}/investigations` &&
      method === "GET"
    ) {
      return fulfill(route, summary());
    }
    if (
      path === `/api/subjects/${ids.subject}/investigations` &&
      method === "POST"
    ) {
      investigation = {
        id: ids.investigation,
        subject_id: ids.subject,
        production_run_id: ids.run,
        synthesis_artifact_id: ids.synthesis,
        synthesis_sha256: hash("a"),
        status: "running",
        version: 1,
        stop_reason: null,
        agenda: [],
        hypotheses: [],
        findings: [
          {
            id: ids.finding,
            version: 1,
            kind: "new_sample",
            assessment: "supported",
            novelty: "new_to_article",
            statement: "Une variante complémentaire est étayée.",
            scope: "Article étudié",
            limitations: ["La famille reste à confirmer."],
            observation_refs: [ids.evidence],
            claim_refs: [],
            created_at: "2026-10-13T10:00:00Z",
          },
        ],
        observations: [
          {
            id: ids.evidence,
            kind: "vt_relationship",
            sha256: hash("d"),
            source: "VirusTotal",
            source_version: "2026-10-13",
            observed_at: "2026-10-13T10:00:00Z",
            object_type: "sample",
            object_id: "sample-fixture",
            limitations: [],
            invocation_ref: null,
          },
        ],
        sample_associations: [
          {
            id: ids.association,
            sample_sha256: hash("d"),
            origin: "vt_pivot",
            qualification: "candidate",
            related_sample_sha256: [hash("a")],
            version: 1,
          },
        ],
        opinion_draft: null,
        budget: {
          bridge_turns: { limit: 8, consumed: 1, remaining: 7 },
          vt_units: { limit: 20, consumed: 1, remaining: 19 },
          shodan_credits: { limit: 5, consumed: 0, remaining: 5 },
          downloads: { limit: 2, consumed: 0, remaining: 2 },
          tool_seconds: { limit: 300, consumed: 1, remaining: 299 },
          context_tokens: { limit: 20_000, consumed: 500, remaining: 19_500 },
          context_bytes: { limit: 100_000, consumed: 3_000, remaining: 97_000 },
        },
        blocked_decisions: [],
        available_tools: ["article.get_claims"],
        unavailable_tools: ["shodan.host"],
        unavailable_tool_reasons: {
          "shodan.host": "g2_disclosure_denied",
        },
        envelope_approved: false,
        allowed_providers: [],
        acquire_hits_allowed: false,
      };
      return fulfill(route, {
        investigation: toInvestigationView(),
        job_id: "99999999-9999-4999-8999-999999999999",
        idempotent_replay: false,
      });
    }
    if (
      path === `/api/investigations/${ids.investigation}` &&
      method === "GET"
    ) {
      return fulfill(route, toInvestigationView());
    }
    if (
      path === `/api/investigations/${ids.investigation}/envelope` &&
      method === "GET"
    ) {
      return fulfill(route, envelope);
    }
    if (
      path === `/api/investigations/${ids.investigation}/envelope/approve` &&
      method === "POST"
    ) {
      const input = body as {
        allowed_providers: string[];
        acquire_hits_allowed: boolean;
        max_acquire_volume: number;
        reason: string;
      };
      envelope = {
        ...envelope,
        approved: true,
        version: 1,
        allowed_providers: input.allowed_providers,
        acquire_hits_allowed: input.acquire_hits_allowed,
        max_acquire_volume: input.max_acquire_volume,
        budget: {
          ...envelope.budget,
          downloads: {
            limit: input.max_acquire_volume,
            consumed: 0,
            remaining: input.max_acquire_volume,
          },
        },
        approval_decision_id: "66666666-6666-4666-8666-666666666666",
        approval_actor_id: "analyst:e2e",
        approved_at: "2026-10-13T10:05:00Z",
      };
      return fulfill(route, envelope);
    }
    if (
      path === `/api/investigations/${ids.investigation}/envelope/extend` &&
      method === "POST"
    ) {
      envelope = { ...envelope, version: envelope.version + 1 };
      return fulfill(route, envelope);
    }
    if (
      path === `/api/investigations/${ids.investigation}/envelope/revoke` &&
      method === "POST"
    ) {
      envelope = {
        ...envelope,
        version: envelope.version + 1,
        allowed_providers: [],
        acquire_hits_allowed: false,
        max_acquire_volume: 0,
        budget: {
          ...envelope.budget,
          vt_units: { limit: 0, consumed: 1, remaining: 0 },
          shodan_credits: { limit: 0, consumed: 0, remaining: 0 },
          downloads: { limit: 0, consumed: 0, remaining: 0 },
        },
        disclosure: { ...envelope.disclosure, external_llm_allowed: false },
      };
      return fulfill(route, envelope);
    }
    if (
      path === `/api/investigations/${ids.investigation}/samples` &&
      method === "GET"
    ) {
      return fulfill(route, [sample]);
    }
    if (
      path ===
        `/api/investigations/${ids.investigation}/samples/${ids.association}/qualify` &&
      method === "POST"
    ) {
      const input = body as {
        qualification: typeof sample.qualification;
        reason: string;
      };
      sample = {
        ...sample,
        qualification: input.qualification,
        version: sample.version + 1,
        changes: [
          ...sample.changes,
          {
            from_qualification: sample.qualification,
            to_qualification: input.qualification,
            actor_kind: "human",
            actor_id: "analyst:e2e",
            reason: input.reason,
            changed_at: "2026-10-13T10:06:00Z",
          },
        ],
      };
      return fulfill(route, { association: sample, idempotent_replay: false });
    }
    if (
      path === `/api/investigations/${ids.investigation}/requests` &&
      method === "POST"
    ) {
      return fulfill(route, {
        investigation: toInvestigationView(),
        request_id: "77777777-7777-4777-8777-777777777777",
        job_id: "88888888-8888-4888-8888-888888888888",
        idempotent_replay: false,
      });
    }
    if (
      path === `/api/investigations/${ids.investigation}/detections` &&
      method === "GET"
    ) {
      return fulfill(route, {
        investigation_id: ids.investigation,
        versions: [],
      });
    }
    if (
      path === `/api/investigations/${ids.investigation}/opinion` &&
      method === "GET"
    ) {
      return fulfill(route, opinionResponse());
    }
    if (
      path === `/api/investigations/${ids.investigation}/opinion/compose` &&
      method === "POST"
    ) {
      const blocks: OpinionPayload["blocks"] = [
        {
          type: "key_findings",
          text: "Une variante complémentaire est étayée.",
          finding_refs: [ids.finding],
          evidence_refs: [ids.evidence],
          qualification: "fact",
          certainty: 0.9,
        },
      ];
      opinion = newOpinion(blocks, 1, "draft");
      return fulfill(route, { draft: opinion });
    }
    if (path === `/api/opinions/${ids.opinion}/revise` && method === "POST") {
      const input = body as {
        expected_version: number;
        blocks: Array<{
          block_type: "key_findings";
          text: string;
          finding_refs: string[];
          evidence_refs: string[];
          qualification: OpinionPayload["blocks"][number]["qualification"];
          certainty: number;
        }>;
        unresolved_points: string[];
      };
      opinion = newOpinion(
        input.blocks.map((block) => ({
          type: block.block_type,
          text: block.text,
          finding_refs: block.finding_refs,
          evidence_refs: block.evidence_refs,
          qualification: block.qualification,
          certainty: block.certainty,
        })),
        input.expected_version + 1,
        "draft",
      );
      opinion.unresolved_points = input.unresolved_points;
      return fulfill(route, { draft: opinion });
    }
    if (path === `/api/opinions/${ids.opinion}/approve` && method === "POST") {
      const input = body as { version: number; expected_sha256: string };
      opinion = newOpinion(
        opinion?.blocks ?? [],
        input.version + 1,
        "approved",
      );
      return fulfill(route, {
        approved: opinion,
        reviewed_version: input.version,
        approved_version: opinion.version,
      });
    }
    if (path === `/api/opinions/${ids.opinion}/publish` && method === "POST") {
      return fulfill(route, {
        publication: {
          artifact_id: "44444444-4444-4444-8444-444444444444",
          run_id: ids.run,
          subject_id: ids.subject,
          version: 5,
          sha256: hash("e"),
          previous_publication_artifact_id: ids.publication,
        },
        composition_repin_required: true,
        message:
          "Nouvelle PUBLICATION créée. Le réépinglage dans une composition reste une action de l’éditeur.",
      });
    }
    return fulfill(route, { detail: { code: "unmocked_route", path } }, 404);
  }

  return {
    ids,
    requests,
    install: (page: Page) =>
      page.route((url) => url.pathname.startsWith("/api/"), handle),
  };
}

function fulfill(route: Route, payload: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(payload),
  });
}
