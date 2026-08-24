import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { DiscoveryMergeReview } from "./DiscoveryMergeReview";

const editionId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const runId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const reconcileJobId = "ffffffff-ffff-4fff-8fff-ffffffffffff";

const pendingRun = {
  id: runId,
  edition_id: editionId,
  parent_snapshot_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
  intake_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  planner_kind: "chatgpt",
  validation_status: "needs_review",
  review_reasons: ["low_confidence_group"],
  warnings: [],
  plan: null,
  projected_diff: [
    {
      group_index: 0,
      existing_subject_handles: ["X1"],
      incoming_candidate_handles: ["C1"],
      disposition: "review",
      flags: [],
      confidence: "low",
      rationale: "Les deux décrivent peut-être la même campagne.",
      evidence: { conflict_signals: ["victimologie divergente"] },
    },
  ],
  handle_labels: {
    X1: {
      handle: "X1",
      title: "Campagne MuddyWater",
      summary: "Sujet déjà consolidé",
      source_urls: ["https://a.example/rapport"],
    },
    C1: {
      handle: "C1",
      title: "Nouvelle activité MuddyWater",
      summary: "Candidat entrant",
      source_urls: ["https://b.example/rapport"],
    },
  },
  supersedes_merge_run_id: null,
  created_at: "2026-08-20T10:00:00+00:00",
};

function runningReconcileJob(overrides: Record<string, unknown> = {}) {
  return {
    id: reconcileJobId,
    kind: "reconcile_discovery",
    aggregate_type: "edition",
    aggregate_id: editionId,
    status: "running",
    progress_current: 0,
    progress_total: 0,
    user_message: null,
    attempt: 1,
    max_attempts: 3,
    next_retry_at: null,
    started_at: "2026-08-21T09:00:00+00:00",
    finished_at: null,
    heartbeat_at: "2026-08-21T09:00:05+00:00",
    error_code: null,
    error_message: null,
    error_details: null,
    correlation_id: "corr-1",
    output_reference: null,
    cancellation_requested: false,
    created_at: "2026-08-21T09:00:00+00:00",
    updated_at: "2026-08-21T09:00:05+00:00",
    ...overrides,
  };
}

/** The stubs only ever match on the path, and RequestInfo is not a string. */
function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

type FetchHandler = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Response | Promise<Response>;

/** None of these tests exercise the merge-planning job itself: without this,
 * every test's catch-all branch would also answer `/api/jobs?...` with a
 * merge-run payload, which `DiscoveryMergeReview` would misread as an active
 * reconciliation job and show a spurious "en cours" banner. */
function withNoReconcileJob(fallback: FetchHandler): FetchHandler {
  return (input, init) => {
    if (urlOf(input).includes("/api/jobs?")) return Response.json([]);
    return fallback(input, init);
  };
}

function stubFetch(handler: FetchHandler) {
  vi.stubGlobal("fetch", vi.fn(withNoReconcileJob(handler)));
}

function renderReview(onReconciling?: (active: boolean) => void) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <DiscoveryMergeReview
        editionId={editionId}
        onReconciling={onReconciling}
      />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

it("propose d’appliquer la fusion en attente et envoie une décision par groupe", async () => {
  const fetchMock = vi.fn(
    withNoReconcileJob((input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input);
      if (url.endsWith("/resolve")) {
        expect(init?.method).toBe("POST");
        expect(JSON.parse(init?.body as string)).toEqual({
          group_decisions: [
            {
              group_index: 0,
              action: "create_new",
              target_subject_handle: null,
            },
          ],
        });
        return Response.json({ snapshot_id: runId, snapshot_version: 2 });
      }
      if (url.endsWith(runId)) return Response.json(pendingRun);
      return Response.json([pendingRun]);
    }),
  );
  vi.stubGlobal("fetch", fetchMock);

  renderReview();

  // The reviewer must see the titles behind X1/C1, not the handles alone.
  expect(await screen.findByText("Campagne MuddyWater")).toBeInTheDocument();
  expect(screen.getByText("Nouvelle activité MuddyWater")).toBeInTheDocument();
  expect(
    screen.getByText(/Les deux décrivent peut-être la même campagne/),
  ).toBeInTheDocument();

  await userEvent.selectOptions(
    screen.getByLabelText("Décision"),
    "create_new",
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Appliquer la fusion" }),
  );

  expect(
    fetchMock.mock.calls.some(([input]) => urlOf(input).endsWith("/resolve")),
  ).toBe(true);
});

it("donne au relecteur les sources et l’effet de la décision choisie", async () => {
  stubFetch((input: RequestInfo | URL) => {
    const url = urlOf(input);
    if (url.endsWith(runId)) return Response.json(pendingRun);
    return Response.json([pendingRun]);
  });

  renderReview();

  // Judging a merge means opening the sources, so they must be reachable.
  const source = await screen.findByRole("link", {
    name: "a.example/rapport",
  });
  expect(source).toHaveAttribute("href", "https://a.example/rapport");

  expect(
    screen.getByText(/Applique le regroupement tel que proposé/),
  ).toBeInTheDocument();
  await userEvent.selectOptions(screen.getByLabelText("Décision"), "defer");
  expect(
    screen.getByText(/Rien n’est appliqué pour ce groupe/),
  ).toBeInTheDocument();
});

it("bloque l’application tant qu’un rattachement n’a pas de sujet cible", async () => {
  stubFetch((input: RequestInfo | URL) => {
    const url = urlOf(input);
    if (url.endsWith(runId)) return Response.json(pendingRun);
    return Response.json([pendingRun]);
  });

  renderReview();

  await userEvent.selectOptions(
    await screen.findByLabelText("Décision"),
    "attach_to",
  );
  expect(
    screen.getByRole("button", { name: "Appliquer la fusion" }),
  ).toBeDisabled();

  await userEvent.selectOptions(screen.getByLabelText("Sujet cible"), "X1");
  expect(
    screen.getByRole("button", { name: "Appliquer la fusion" }),
  ).toBeEnabled();
});

it("n’offre pas d’appliquer une fusion qu’aucun groupe ne compose", async () => {
  // Reproduces the observed dead end: the merge model stalled, so the run was
  // persisted with zero groups. Resolving it is impossible — the endpoint
  // rejects an empty decision list — so no button may be shown.
  const emptyRun = {
    ...pendingRun,
    id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    projected_diff: [],
    handle_labels: {},
    review_reasons: ["plan_invalid_after_repair"],
  };
  stubFetch(() => Response.json([emptyRun]));

  renderReview();

  expect(
    await screen.findByText(/Fusion impossible à planifier/),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Appliquer la fusion" }),
  ).not.toBeInTheDocument();
});

it("présente la plus ancienne fusion actionnable malgré une fusion vide plus récente", async () => {
  // The empty run is newer; showing it would hide a proposal the analyst can
  // actually act on — which is how the pending proposals appeared to vanish.
  const emptyRun = {
    ...pendingRun,
    id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    projected_diff: [],
    handle_labels: {},
    review_reasons: ["plan_invalid_after_repair"],
    created_at: "2026-08-20T18:00:00+00:00",
  };
  stubFetch((input: RequestInfo | URL) => {
    const url = urlOf(input);
    if (url.endsWith(runId)) return Response.json(pendingRun);
    return Response.json([emptyRun, pendingRun]);
  });

  renderReview();

  expect(await screen.findByText("Campagne MuddyWater")).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "Appliquer la fusion" }),
  ).toBeEnabled();
  expect(screen.getByText(/1 autre fusion en attente/)).toBeInTheDocument();
});

it("retire le panneau une fois la fusion appliquée", async () => {
  // The reported failure: the merge landed, but the panel kept showing the same
  // proposal, so the analyst clicked again and got a server error. Once the run
  // is settled the list no longer reports it as awaiting review, and the panel
  // must follow rather than sit on the answer it already got.
  let resolved = false;
  stubFetch((input: RequestInfo | URL) => {
    const url = urlOf(input);
    if (url.endsWith("/resolve")) {
      resolved = true;
      return Response.json({ snapshot_id: runId, snapshot_version: 2 });
    }
    if (url.endsWith(runId)) return Response.json(pendingRun);
    return Response.json(
      resolved
        ? [{ ...pendingRun, validation_status: "resolved" }]
        : [pendingRun],
    );
  });

  renderReview();

  await userEvent.click(
    await screen.findByRole("button", { name: "Appliquer la fusion" }),
  );

  expect(
    await screen.findByText(/Aucune fusion en attente/),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Appliquer la fusion" }),
  ).not.toBeInTheDocument();
});

it("explique un parent périmé au lieu de rester figé", async () => {
  // A contribution consolidated in the meantime invalidates this proposal. The
  // reviewer needs to read that, not an unqualified "la découverte a échoué".
  stubFetch((input: RequestInfo | URL) => {
    const url = urlOf(input);
    if (url.endsWith("/resolve"))
      return Response.json(
        {
          detail: {
            code: "discovery_snapshot_stale",
            message: "Une contribution plus récente a modifié l'édition.",
          },
        },
        { status: 409 },
      );
    if (url.endsWith(runId)) return Response.json(pendingRun);
    return Response.json([pendingRun]);
  });

  renderReview();

  await userEvent.click(
    await screen.findByRole("button", { name: "Appliquer la fusion" }),
  );

  expect(await screen.findByRole("alert")).toHaveTextContent(
    /consolidée entre-temps/,
  );
});

it("ne montre aucun bouton quand la consolidation est automatique", async () => {
  stubFetch(() => Response.json([]));

  renderReview();

  expect(
    await screen.findByText(/Aucune fusion en attente/),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Appliquer la fusion" }),
  ).not.toBeInTheDocument();
});

it("signale que le bridge est occupé pendant la planification de la fusion", async () => {
  // Nothing is pending review yet — the planner is still calling ChatGPT in
  // the background. Without this, the panel used to say nothing at all.
  const onReconciling = vi.fn();
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = urlOf(input);
      if (url.includes("/api/jobs?"))
        return Response.json([runningReconcileJob()]);
      if (url.endsWith(`/api/jobs/${reconcileJobId}`))
        return Response.json(runningReconcileJob());
      return Response.json([]);
    }),
  );

  renderReview(onReconciling);

  expect(
    await screen.findByText(/Fusion en cours de génération/),
  ).toBeInTheDocument();
  expect(
    screen.queryByText(/Aucune fusion en attente/),
  ).not.toBeInTheDocument();
  expect(onReconciling).toHaveBeenCalledWith(true);
});

it("ne signale plus le bridge occupé une fois le job en attente de décision humaine", async () => {
  // Regression: "waiting_human" is not terminal either, but it means the
  // bridge already answered and the job is parked on the review below — the
  // banner used to stay pinned up forever and block new searches even once
  // nothing was running anymore.
  const onReconciling = vi.fn();
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = urlOf(input);
      if (url.includes("/api/jobs?"))
        return Response.json([
          runningReconcileJob({ status: "waiting_human" }),
        ]);
      if (url.endsWith(runId)) return Response.json(pendingRun);
      return Response.json([pendingRun]);
    }),
  );

  renderReview(onReconciling);

  expect(await screen.findByText("Campagne MuddyWater")).toBeInTheDocument();
  expect(
    screen.queryByText(/Fusion en cours de génération/),
  ).not.toBeInTheDocument();
  expect(onReconciling).toHaveBeenCalledWith(false);
});

it("distingue un nouveau sujet d’une vraie fusion et traduit les avertissements", async () => {
  const newSubjectRun = {
    ...pendingRun,
    warnings: ["group 0: conflicts force human review"],
    projected_diff: [
      {
        ...pendingRun.projected_diff[0],
        existing_subject_handles: [],
      },
    ],
  };
  stubFetch((input: RequestInfo | URL) => {
    const url = urlOf(input);
    if (url.endsWith(runId)) return Response.json(newSubjectRun);
    return Response.json([newSubjectRun]);
  });

  renderReview();

  expect(await screen.findByText("nouveau sujet")).toBeInTheDocument();
  expect(
    screen.getByText(/il n’y a rien à\s*fusionner ici/),
  ).toBeInTheDocument();
  expect(
    screen.getByText(
      /la contradiction n’a pas été tranchée automatiquement et force cette revue humaine/,
    ),
  ).toBeInTheDocument();
  // The raw validator text must not leak into the reviewer-facing copy.
  expect(
    screen.queryByText(/conflicts force human review/),
  ).not.toBeInTheDocument();
});
