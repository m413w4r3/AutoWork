import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  bodyOf,
  createTestQueryClient,
  errorResponse,
  headersOf,
  installFetch,
  jsonResponse,
  wrapperFor,
} from "../api/testSupport";
import { PlanPanel } from "./PlanPanel";
import { INVESTIGATION_ID } from "../tasks/testFixtures";

afterEach(() => {
  vi.unstubAllGlobals();
});

const PLAN_V1_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const PLAN_V2_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const INVOCATION_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const DECISION_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
const PLAN_SHA = "f".repeat(64);

const LEADS = [
  {
    title: "Analyser le dropper",
    objective: "Identifier le packer du binaire.",
    cti_justification: "Le claim 1 décrit un chargeur non documenté.",
    claim_refs: ["claim-1"],
    start_evidence_refs: ["obs-1"],
    sample_refs: ["a".repeat(64)],
    tools: ["floss", "ghidra"],
    preconditions: ["Échantillon en quarantaine"],
    interest: "Découvrir le protocole C2.",
    conclusion_criteria: "Chaîne d’infection établie.",
    limits: "Pas d’exécution sur l’hôte.",
    depends_on: [],
    lead_id: "lead-a",
  },
  {
    title: "Pivoter sur le domaine",
    objective: "",
    cti_justification: "",
    claim_refs: [],
    start_evidence_refs: [],
    sample_refs: [],
    tools: [],
    preconditions: [],
    interest: "",
    conclusion_criteria: "",
    limits: "",
    depends_on: ["lead-a"],
  },
];

function planSummary(version: number, versionId: string, current: boolean) {
  return {
    plan_version_id: versionId,
    investigation_id: INVESTIGATION_ID,
    version,
    schema_version: "investigation-plan.v1",
    content_sha256: PLAN_SHA,
    parse_status: "partially_parsed",
    source_invocation_id: INVOCATION_ID,
    source_observation_id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
    supersedes_id: null,
    created_at: "2026-11-22T10:00:00Z",
    lead_count: LEADS.length,
    current,
  };
}

function planDetail(version: number, versionId: string, current: boolean) {
  return {
    ...planSummary(version, versionId, current),
    content: {
      schema_version: "investigation-plan.v1",
      investigation_id: INVESTIGATION_ID,
      version,
      leads: LEADS,
    },
  };
}

function emptyPage() {
  return { items: [], total: 0 };
}

function renderPanel() {
  return render(<PlanPanel investigationId={INVESTIGATION_ID} />, {
    wrapper: wrapperFor(createTestQueryClient()),
  });
}

describe("PlanPanel request", () => {
  it("demande un plan avec la version d’état, l’idempotence et le libellé avis", async () => {
    const user = userEvent.setup();
    const recorder = installFetch(({ url, init }) => {
      const method = init?.method ?? "GET";
      if (url.endsWith(`/api/investigations/${INVESTIGATION_ID}`)) {
        return jsonResponse({ version: 7 });
      }
      if (url.endsWith("/plans/request") && method === "POST") {
        return jsonResponse({
          investigation_id: INVESTIGATION_ID,
          invocation_id: INVOCATION_ID,
          invocation_status: "succeeded",
          idempotent_replay: false,
          advisor_parse_status: "unparsed",
          plan_parse_status: "unparsed",
          plan_version_id: null,
          plan_version: null,
          missing_fields: ["leads"],
          advice_only: true,
        });
      }
      if (url.endsWith("/plans")) {
        return jsonResponse({ current_version: null, plans: [] });
      }
      if (url.endsWith("/consultations")) {
        return jsonResponse({
          investigation_id: INVESTIGATION_ID,
          consultations: [],
        });
      }
      if (url.endsWith("/tasks")) return jsonResponse(emptyPage());
      return errorResponse(500, "unexpected_request");
    });

    renderPanel();

    await screen.findByText(/Aucun plan pour cette enquête/);
    await user.type(
      screen.getByLabelText(/Question facultative pour Bridge/),
      "Priorise le PCAP.",
    );
    await user.click(
      screen.getByRole("button", { name: "Demander un plan à Bridge" }),
    );

    expect(
      await screen.findByText(/Aucune version de plan n’a été créée/),
    ).toBeInTheDocument();
    const request = recorder.calls.find((call) =>
      call.url.endsWith("/plans/request"),
    );
    expect(request).toBeDefined();
    if (request === undefined) throw new Error("plan request was not sent");
    expect(bodyOf(request.init)).toEqual({
      expected_version: 7,
      question: "Priorise le PCAP.",
    });
    expect(headersOf(request.init)["Idempotency-Key"]).toBeTruthy();
  });
});

describe("PlanPanel acceptance", () => {
  it("affiche les champs de piste et accepte la version courante sous CAS", async () => {
    const user = userEvent.setup();
    const recorder = installFetch(({ url, init }) => {
      const method = init?.method ?? "GET";
      if (url.endsWith(`/api/investigations/${INVESTIGATION_ID}`)) {
        return jsonResponse({ version: 7 });
      }
      if (url.endsWith(`/plans/${PLAN_V2_ID}/accept`) && method === "POST") {
        return jsonResponse({
          investigation_id: INVESTIGATION_ID,
          plan_version_id: PLAN_V2_ID,
          plan_version: 2,
          decision_id: DECISION_ID,
          tasks: [
            {
              task_id: "11111111-1111-4111-8111-111111111111",
              task_key: "lead-a",
              title: "Analyser le dropper",
              status: "ready",
              created: true,
            },
            {
              task_id: "22222222-2222-4222-8222-222222222222",
              task_key: "lead-b",
              title: "Pivoter sur le domaine",
              status: "proposed",
              created: true,
            },
          ],
          idempotent_replay: false,
        });
      }
      if (url.endsWith(`/plans/${PLAN_V1_ID}`)) {
        return jsonResponse(planDetail(1, PLAN_V1_ID, false));
      }
      if (url.endsWith(`/plans/${PLAN_V2_ID}`)) {
        return jsonResponse(planDetail(2, PLAN_V2_ID, true));
      }
      if (url.endsWith("/plans")) {
        return jsonResponse({
          current_version: 2,
          plans: [
            planSummary(1, PLAN_V1_ID, false),
            planSummary(2, PLAN_V2_ID, true),
          ],
        });
      }
      if (url.endsWith("/consultations")) {
        return jsonResponse({
          investigation_id: INVESTIGATION_ID,
          consultations: [],
        });
      }
      if (url.endsWith("/tasks")) return jsonResponse(emptyPage());
      return errorResponse(500, "unexpected_request");
    });

    renderPanel();

    expect(
      await screen.findByText("Piste 1 — Analyser le dropper"),
    ).toBeInTheDocument();
    for (const label of [
      "Objectif",
      "Justification CTI",
      "Références de claims",
      "Preuves de départ",
      "Échantillons",
      "Outils",
      "Préconditions",
      "Intérêt",
      "Critères de conclusion",
      "Limites",
      "Dépendances",
    ]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(screen.getAllByText("Non renseigné").length).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: /Version 1/ }));
    expect(
      await screen.findByText(/Version antérieure : lecture seule/),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Version 2/ }));
    await user.click(
      await screen.findByLabelText(
        /Je confirme l’acceptation de cette version/,
      ),
    );
    await user.click(
      screen.getByRole("button", {
        name: "Accepter le plan et créer les tâches",
      }),
    );

    expect(
      await screen.findByText(
        /Plan accepté : 2 tâche\(s\) créée\(s\), 0 déjà présente\(s\)/,
      ),
    ).toBeInTheDocument();
    const request = recorder.calls.find((call) =>
      call.url.endsWith(`/plans/${PLAN_V2_ID}/accept`),
    );
    expect(request).toBeDefined();
    if (request === undefined) throw new Error("accept was not sent");
    expect(bodyOf(request.init)).toEqual({ expected_sha256: PLAN_SHA });
    expect(headersOf(request.init)["Idempotency-Key"]).toBeTruthy();
  });
});
