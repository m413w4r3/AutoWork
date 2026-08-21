import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";

import {
  listMergeRuns,
  readMergeRun,
  resolveMergeRun,
  type MergeDecisionAction,
  type MergeGroupDiff,
  type MergeHandleLabel,
  type MergeRun,
} from "../api/discovery";
import { listJobs } from "../api/jobs";
import { JobStatusCard } from "./JobStatusCard";

const RECONCILE_JOB_KIND = "reconcile_discovery";

const actionLabels: Record<MergeDecisionAction, string> = {
  accept: "Accepter la proposition",
  create_new: "Créer un sujet distinct",
  attach_to: "Rattacher à un sujet existant",
  merge_existing: "Fusionner des sujets existants",
  defer: "Décider plus tard",
};

/** What each action does, in the reviewer's terms rather than the API's. */
const actionHelp: Record<MergeDecisionAction, string> = {
  accept: "Applique le regroupement tel que proposé ci-dessus.",
  create_new:
    "Les candidats entrants deviennent un sujet à part, sans être rattachés.",
  attach_to: "Les candidats entrants rejoignent le sujet existant choisi.",
  merge_existing:
    "Les sujets existants du groupe sont fusionnés dans celui choisi.",
  defer:
    "Rien n’est appliqué pour ce groupe ; il restera à trancher plus tard.",
};

const confidenceLabels: Record<string, string> = {
  high: "confiance élevée",
  medium: "confiance moyenne",
  low: "confiance faible",
};

const reviewReasonLabels: Record<string, string> = {
  human_decision_deferred: "Des groupes sont restés en attente de décision.",
  low_confidence_group: "Le planificateur hésite sur au moins un regroupement.",
  conflict_signals: "Des signaux contradictoires ont été relevés.",
  plan_invalid: "Le plan produit ne respectait pas le schéma attendu.",
};

const GROUP_WARNING_PATTERN = /^group (\d+): (.+)$/;

/** Translate the validator's terse guard-rail text into something a reviewer
 * without backend context can act on, instead of raw English internals. */
function translateWarning(message: string): string {
  const removedUrls = message.match(/^removed unknown evidence URLs: (.+)$/);
  if (removedUrls) {
    return (
      "Le modèle citait, à l’appui de ce regroupement, des URLs qui ne " +
      "correspondent à aucune source connue de l’édition ; elles ont été " +
      "retirées des preuves affichées plutôt que gardées telles quelles : " +
      removedUrls[1]
    );
  }
  if (message === "conflicts force human review") {
    return (
      "Le modèle a relevé des signaux contradictoires tout en se déclarant " +
      "confiant ; la contradiction n’a pas été tranchée automatiquement et " +
      "force cette revue humaine."
    );
  }
  if (message === "empty rationale") {
    return "Le modèle n’a fourni aucune justification pour ce regroupement.";
  }
  return message;
}

/** Warnings come back as one flat list ("group N: ..."); splitting them back
 * out by group lets each land next to the group it concerns instead of one
 * unreadable line concatenating every group's warnings at the top. */
function groupWarnings(warnings: string[]): {
  byGroup: Map<number, string[]>;
  other: string[];
} {
  const byGroup = new Map<number, string[]>();
  const other: string[] = [];
  for (const warning of warnings) {
    const match = warning.match(GROUP_WARNING_PATTERN);
    if (!match) {
      other.push(warning);
      continue;
    }
    const index = Number(match[1]);
    const list = byGroup.get(index) ?? [];
    list.push(translateWarning(match[2] ?? warning));
    byGroup.set(index, list);
  }
  return { byGroup, other };
}

/** An action that names a target subject cannot be submitted without one. */
function needsTarget(action: MergeDecisionAction): boolean {
  return action === "attach_to" || action === "merge_existing";
}

/** Say what the reviewer should do next, not just that something failed. */
function resolveErrorMessage(error: unknown): string {
  if (!(error instanceof Error)) return "La fusion a échoué.";
  const code = (error as { code?: string }).code;
  if (code === "discovery_snapshot_stale")
    return (
      "Une autre contribution a été consolidée entre-temps : cette proposition " +
      "portait sur un état dépassé. Elle a été replanifiée, la nouvelle version " +
      "s’affiche ci-dessus."
    );
  if (code === "merge_still_needs_review")
    return (
      "Des groupes sont restés sans décision : une proposition de reprise les " +
      "reprend et s’affiche ci-dessus."
    );
  return error.message;
}

function labelFor(run: MergeRun, handle: string): MergeHandleLabel | undefined {
  return run.handle_labels[handle];
}

function HandleList({ run, handles }: { run: MergeRun; handles: string[] }) {
  if (handles.length === 0) return <em>aucun</em>;
  return (
    <ul className="merge-handle-list">
      {handles.map((handle) => {
        const label = labelFor(run, handle);
        return (
          <li key={handle}>
            <span className="merge-handle">{handle}</span>{" "}
            <strong>{label ? label.title : "(libellé indisponible)"}</strong>
            {label?.summary ? (
              <p className="merge-handle-summary">{label.summary}</p>
            ) : null}
            {/* Deciding whether two subjects are the same usually comes down to
                opening the sources, so they are one click away rather than a
                count the reviewer has to go hunting for. */}
            {label && label.source_urls.length > 0 ? (
              <ul className="merge-handle-sources">
                {label.source_urls.map((url) => (
                  <li key={url}>
                    <a href={url} target="_blank" rel="noreferrer noopener">
                      {url.replace(/^https?:\/\//, "").slice(0, 70)}
                    </a>
                  </li>
                ))}
              </ul>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

function MergeGroupCard({
  run,
  group,
  warnings,
  decision,
  target,
  onDecision,
  onTarget,
}: {
  run: MergeRun;
  group: MergeGroupDiff;
  warnings: string[];
  decision: MergeDecisionAction;
  target: string;
  onDecision: (action: MergeDecisionAction) => void;
  onTarget: (handle: string) => void;
}) {
  const existingHandles = Object.keys(run.handle_labels)
    .filter((handle) => handle.startsWith("X"))
    .sort((a, b) => Number(a.slice(1)) - Number(b.slice(1)));
  const conflicts = group.evidence.conflict_signals ?? [];
  const shared = [
    ...(group.evidence.shared_campaigns ?? []),
    ...(group.evidence.shared_malware ?? []),
    ...(group.evidence.shared_explicit_identifiers ?? []),
  ];
  // No existing subject in the group means there is nothing to merge: the
  // planner is only flagging this incoming candidate for a look before it
  // becomes a new subject. Calling that a "fusion" like the others is what
  // makes reviewers hunt for a merge that was never proposed.
  const isNewSubject = group.existing_subject_handles.length === 0;

  return (
    <article className="merge-group-card">
      <div className="merge-group-card__heading">
        <h4>Groupe {group.group_index + 1}</h4>
        <span className={`merge-group-kind is-${isNewSubject ? "new" : "merge"}`}>
          {isNewSubject ? "nouveau sujet" : "fusion proposée"}
        </span>
        {group.confidence ? (
          <span className={`merge-confidence is-${group.confidence}`}>
            {confidenceLabels[group.confidence] ?? group.confidence}
          </span>
        ) : null}
      </div>

      {isNewSubject ? (
        <p className="merge-group-card__hint">
          Aucun sujet existant ne recoupe ce candidat : il n’y a rien à
          fusionner ici, seulement une vérification avant qu’il ne devienne un
          nouveau sujet.
        </p>
      ) : null}

      <div className="merge-group-card__sides">
        <div className="merge-side merge-side--existing">
          <h5>Déjà dans l’édition</h5>
          <HandleList run={run} handles={group.existing_subject_handles} />
        </div>
        <div className="merge-side merge-side--incoming">
          <h5>Apporté par cette contribution</h5>
          <HandleList run={run} handles={group.incoming_candidate_handles} />
        </div>
      </div>

      {group.rationale ? (
        <p className="merge-group-card__rationale">
          <strong>Justification :</strong> {group.rationale}
        </p>
      ) : null}

      {shared.length > 0 ? (
        <p>
          <strong>Éléments partagés :</strong> {shared.join(", ")}
        </p>
      ) : null}

      {conflicts.length > 0 ? (
        <p className="merge-group-card__conflicts">
          <strong>Signaux contradictoires :</strong> {conflicts.join(", ")}
        </p>
      ) : null}

      {group.flags.length > 0 ? (
        <p>
          <strong>Signalements :</strong> {group.flags.join(", ")}
        </p>
      ) : null}

      {warnings.length > 0 ? (
        <ul className="merge-group-card__warnings">
          {warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      ) : null}

      <div className="merge-group-card__decision">
        <label>
          Décision
          <select
            value={decision}
            onChange={(event) =>
              onDecision(event.target.value as MergeDecisionAction)
            }
          >
            {(Object.keys(actionLabels) as MergeDecisionAction[]).map(
              (action) => (
                <option key={action} value={action}>
                  {actionLabels[action]}
                </option>
              ),
            )}
          </select>
        </label>

        {needsTarget(decision) ? (
          <label>
            Sujet cible
            <select
              value={target}
              onChange={(event) => onTarget(event.target.value)}
            >
              <option value="">— choisir —</option>
              {existingHandles.map((handle) => (
                <option key={handle} value={handle}>
                  {handle} · {labelFor(run, handle)?.title ?? handle}
                </option>
              ))}
            </select>
          </label>
        ) : null}
      </div>
      <p className="merge-group-card__help">{actionHelp[decision]}</p>
    </article>
  );
}

export function DiscoveryMergeReview({
  editionId,
  onReconciling,
}: {
  editionId: string;
  /** Reports whether a merge-planning job is actively using the ChatGPT
   * bridge, so a caller can hold off on launching another bridge action. */
  onReconciling?: (active: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [decisions, setDecisions] = useState<
    Record<number, { action: MergeDecisionAction; target: string }>
  >({});

  const runs = useQuery({
    queryKey: ["merge-runs", editionId],
    queryFn: () => listMergeRuns(editionId),
  });

  const awaiting = (Array.isArray(runs.data) ? runs.data : [])
    .filter((run) => run.validation_status === "needs_review")
    .sort((a, b) => a.created_at.localeCompare(b.created_at));

  // Oldest first: each contribution parked its own run against the same parent
  // snapshot, and resolving one makes the later ones stale. A run with no group
  // could not be planned at all — the resolve endpoint rejects an empty
  // decision list, so it must never be offered as actionable.
  const pending = awaiting.find((run) => run.projected_diff.length > 0);
  const unplannable = awaiting.filter((run) => run.projected_diff.length === 0);
  const queued = awaiting.filter((run) => run !== pending).length;

  const detail = useQuery({
    queryKey: ["merge-run", editionId, pending?.id],
    queryFn: () => readMergeRun(editionId, pending!.id),
    enabled: pending !== undefined,
  });

  // Every group starts on the planner's own proposal; the analyst only has to
  // touch the ones they disagree with. Keyed on the run id, not on the query
  // object: a background refetch returns a fresh object and would otherwise wipe
  // choices the reviewer had already made.
  const detailRunId = detail.data?.id;
  const groupIndexes = detail.data?.projected_diff
    .map((group) => group.group_index)
    .join(",");
  useEffect(() => {
    if (!groupIndexes) return;
    setDecisions(
      Object.fromEntries(
        groupIndexes
          .split(",")
          .map((index) => [Number(index), { action: "accept", target: "" }]),
      ),
    );
  }, [detailRunId, groupIndexes]);

  // The planner runs as a background job against the single-slot ChatGPT
  // bridge *before* any merge run exists to poll — without tracking it
  // separately, the panel has nothing to show between a contribution landing
  // and a proposal appearing, and nothing signals that the bridge is busy.
  const reconcileJobs = useQuery({
    queryKey: ["reconcile-job", editionId],
    queryFn: () => listJobs("edition", editionId, RECONCILE_JOB_KIND),
    refetchInterval: ({ state }) => {
      const latest = state.data?.[0];
      return latest && (latest.status === "queued" || latest.status === "running")
        ? 3_000
        : 8_000;
    },
  });
  const latestReconcileJob = reconcileJobs.data?.[0];
  // "waiting_human" is not terminal either, but it means the bridge already
  // answered and the job is parked on the review below — treating it as
  // "still busy" is what pinned this banner up forever and blocked new
  // searches even once there was nothing left running.
  const reconciling =
    latestReconcileJob?.status === "queued" || latestReconcileJob?.status === "running";
  useEffect(() => {
    onReconciling?.(reconciling);
  }, [reconciling, onReconciling]);
  // Leaving the panel must not leave the caller believing the bridge is
  // still busy on its account.
  useEffect(() => () => onReconciling?.(false), [onReconciling]);

  const refreshMergeState = () => {
    void queryClient.invalidateQueries({ queryKey: ["merge-runs", editionId] });
    void queryClient.invalidateQueries({ queryKey: ["merge-run", editionId] });
    void queryClient.invalidateQueries({ queryKey: ["discovery", editionId] });
  };

  const resolve = useMutation({
    mutationFn: (run: MergeRun) =>
      resolveMergeRun(
        editionId,
        run.id,
        run.projected_diff.map((group) => {
          const choice = decisions[group.group_index] ?? {
            action: "accept" as MergeDecisionAction,
            target: "",
          };
          return {
            group_index: group.group_index,
            action: choice.action,
            target_subject_handle: needsTarget(choice.action)
              ? choice.target || null
              : null,
          };
        }),
      ),
    onSuccess: refreshMergeState,
    // A refused resolution still moved the server state — a deferral parks a
    // successor run, a stale parent means another contribution landed first.
    // Refetching is what stops the panel from showing a decision that no longer
    // exists, which is how a single failure used to look like a frozen screen.
    onError: refreshMergeState,
  });

  const reconcileBanner = reconciling ? (
    <section className="merge-review merge-review__job" aria-live="polite">
      <h4>Fusion en cours de génération</h4>
      <p>
        ChatGPT évalue si la dernière contribution recoupe des sujets déjà
        dans l’édition. Aucune autre recherche ne peut utiliser le bridge tant
        que cette évaluation n’est pas terminée.
      </p>
      <JobStatusCard jobId={latestReconcileJob!.id} onTerminal={refreshMergeState} />
    </section>
  ) : null;

  let body: ReactNode;
  if (runs.isPending) {
    body = null;
  } else if (runs.isError) {
    body = (
      <p role="alert" className="error-message">
        L’état de la fusion est inaccessible.
      </p>
    );
  } else if (!pending) {
    if (unplannable.length > 0) {
      body = (
        <section className="merge-review is-blocked">
          <h4>Fusion impossible à planifier</h4>
          <p>
            {unplannable.length} contribution
            {unplannable.length > 1 ? "s ont" : " a"} atteint la fusion sans
            qu’aucun regroupement puisse être proposé. Il n’y a rien à valider
            ici : la cause est en amont, côté modèle de fusion.
          </p>
          <ul className="merge-review__reasons">
            {[...new Set(unplannable.flatMap((run) => run.review_reasons))].map(
              (reason) => (
                <li key={reason}>{reviewReasonLabels[reason] ?? reason}</li>
              ),
            )}
          </ul>
        </section>
      );
    } else if (reconciling) {
      // The banner above already says a proposal is on its way — saying
      // "nothing pending" at the same time would just contradict it.
      body = null;
    } else {
      body = (
        <p className="merge-review-idle">
          Aucune fusion en attente : les contributions sont consolidées
          automatiquement.
        </p>
      );
    }
  } else if (detail.isPending) {
    body = <p role="status">Chargement de la proposition de fusion…</p>;
  } else if (detail.isError || !detail.data) {
    body = (
      <p role="alert" className="error-message">
        La proposition de fusion est inaccessible.
      </p>
    );
  } else {
    const run = detail.data;
    const { byGroup: groupedWarnings, other: otherWarnings } = groupWarnings(
      run.warnings,
    );
    const incomplete = run.projected_diff.some((group) => {
      const choice = decisions[group.group_index];
      return choice && needsTarget(choice.action) && !choice.target;
    });

    body = (
      <section className="merge-review">
        <h4>Fusion à valider</h4>
        <p>
          La consolidation automatique s’est arrêtée et attend une décision
          humaine sur {run.projected_diff.length} groupe
          {run.projected_diff.length > 1 ? "s" : ""}.
        </p>

        {queued > 0 ? (
          <p className="merge-review__queued">
            {queued} autre{queued > 1 ? "s" : ""} fusion{queued > 1 ? "s" : ""}{" "}
            en attente derrière celle-ci ; elles seront replanifiées une fois
            celle-ci appliquée.
          </p>
        ) : null}

        {run.review_reasons.length > 0 ? (
          <ul className="merge-review__reasons">
            {run.review_reasons.map((reason) => (
              <li key={reason}>{reviewReasonLabels[reason] ?? reason}</li>
            ))}
          </ul>
        ) : null}

        {otherWarnings.length > 0 ? (
          <p className="merge-review__warnings">
            <strong>Avertissements :</strong> {otherWarnings.join(" · ")}
          </p>
        ) : null}

        {run.projected_diff.map((group) => (
          <MergeGroupCard
            key={group.group_index}
            run={run}
            group={group}
            warnings={groupedWarnings.get(group.group_index) ?? []}
            decision={decisions[group.group_index]?.action ?? "accept"}
            target={decisions[group.group_index]?.target ?? ""}
            onDecision={(action) =>
              setDecisions((current) => ({
                ...current,
                [group.group_index]: {
                  action,
                  target: current[group.group_index]?.target ?? "",
                },
              }))
            }
            onTarget={(handle) =>
              setDecisions((current) => ({
                ...current,
                [group.group_index]: {
                  action: current[group.group_index]?.action ?? "accept",
                  target: handle,
                },
              }))
            }
          />
        ))}

        <button
          type="button"
          disabled={incomplete || resolve.isPending}
          onClick={() => resolve.mutate(run)}
        >
          {resolve.isPending ? "Fusion en cours…" : "Appliquer la fusion"}
        </button>

        {incomplete ? (
          <p className="merge-review__blocked">
            Choisis un sujet cible pour chaque rattachement avant d’appliquer.
          </p>
        ) : null}

        {resolve.isError ? (
          <p role="alert" className="error-message">
            {resolveErrorMessage(resolve.error)}
          </p>
        ) : null}
      </section>
    );
  }

  return (
    <>
      {reconcileBanner}
      {body}
    </>
  );
}
