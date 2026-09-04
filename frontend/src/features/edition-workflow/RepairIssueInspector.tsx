import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { ApiError } from "../../api/editions";
import {
  decideEditionRepair,
  getEditionRepairDetail,
  type EditionRepairItem,
  type ProductionRepairAction,
  type RepairDecisionApplicationState,
  type ReviewItem,
} from "../../api/publication";
import { Link } from "../../routing";
import { RepairRulePanel } from "./RepairRulePanel";
import { RepairSourcePanel } from "./RepairSourcePanel";
import {
  alternativeRepairActions,
  repairActionLabel,
  repairKindLabel,
  repairReasonLabel,
  repairStatusLabel,
} from "./RepairQueue";

const STALE_REPAIR_MESSAGE =
  "Cet élément a changé depuis son ouverture. La file de réparation a été rechargée.";
const CHANGED_REPAIR_MESSAGE =
  "La décision a changé depuis son affichage. La décision courante a été rechargée.";

const DECISION_ACTION_LABELS: Record<string, string> = {
  include: "Inclure dans la fiche",
  exclude: "Exclure",
  continue_without_source: "Continuer sans cette source",
};

/**
 * "L’analyste a décidé INCLURE" et "le livrable contient la valeur" sont deux
 * faits distincts. L’application est donnée par le backend, qui compare la
 * décision effective au marqueur de la projection réellement en place.
 */
const APPLICATION_LABELS: Record<RepairDecisionApplicationState, string> = {
  already_effective: "matérialisée",
  projection_required: "en attente de rebuild",
  unbuildable: "non projetable",
  unresolved: "aucune décision à appliquer",
};

const SUPERSEDED_LABEL = "remplacée par une décision ultérieure";

const DECISION_DATE_FORMAT = new Intl.DateTimeFormat("fr-FR", {
  day: "2-digit",
  month: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

function decisionTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : DECISION_DATE_FORMAT.format(date);
}

export function RepairIssueInspector({
  editionId,
  item,
  article,
  readOnly,
  onChanged,
  onArchived,
}: {
  editionId: string;
  item: EditionRepairItem | null;
  /** The reviewed article, for the provenance of the final deliverable. */
  article: ReviewItem | null;
  readOnly: boolean;
  onChanged: () => void;
  onArchived: (item: EditionRepairItem) => void;
}) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  // Purely presentational: whether the alternative actions are unfolded. The
  // decision itself is never held locally -- the query is the authority.
  const [revising, setRevising] = useState(false);
  const detail = useQuery({
    queryKey: ["edition-repair-detail", editionId, item?.repair_key],
    queryFn: () => getEditionRepairDetail(editionId, item?.repair_key ?? ""),
    enabled: Boolean(item),
  });
  const repairKey = item?.repair_key;
  useEffect(() => {
    setRevising(false);
    setError(null);
    setReason("");
  }, [repairKey]);

  const currentDetail = detail.data;
  const effectiveDecision = currentDetail?.effective_decision ?? null;
  const effectiveAction: ProductionRepairAction | null =
    effectiveDecision?.action ?? item?.effective_action ?? null;
  const expectedEffectiveDecisionId =
    effectiveDecision?.id ?? item?.effective_decision_id ?? null;
  const decisionHistory = currentDetail?.decision_history ?? [];
  // Never inferred from the action: an unknown state stays unknown.
  const applicationState: RepairDecisionApplicationState | null =
    currentDetail?.application_state ?? item?.application_state ?? null;
  const applicationLabel = applicationState
    ? APPLICATION_LABELS[applicationState]
    : "non projetable (donnée héritée)";

  const decide = useMutation({
    mutationFn: (action: ProductionRepairAction) => {
      if (!item?.artifact_id) {
        return Promise.reject(
          new Error("L’identité de l’artefact n’est plus disponible."),
        );
      }
      return decideEditionRepair(editionId, item.repair_key, {
        action,
        observedSubjectId: item.subject_id,
        observedRunId: item.run_id,
        observedArtifactId: item.artifact_id,
        observedPipelineGeneration: item.pipeline_generation,
        expectedEffectiveDecisionId,
        reason: reason || null,
      });
    },
    retry: false,
    onSuccess: () => {
      setError(null);
      setReason("");
      setRevising(false);
      void queryClient.invalidateQueries({
        queryKey: ["edition-repair-detail", editionId, item?.repair_key],
      });
      onChanged();
    },
    onError: (mutationError: unknown) => {
      const code =
        mutationError instanceof ApiError ? mutationError.code : null;
      if (
        code === "production_repair_stale" ||
        code === "production_repair_decision_changed"
      ) {
        setError(
          code === "production_repair_decision_changed"
            ? CHANGED_REPAIR_MESSAGE
            : STALE_REPAIR_MESSAGE,
        );
        setRevising(false);
        void queryClient.invalidateQueries({
          queryKey: ["edition-repair-detail", editionId, item?.repair_key],
        });
        onChanged();
        return;
      }
      setError(
        mutationError instanceof Error
          ? mutationError.message
          : "La décision de réparation n’a pas pu être enregistrée.",
      );
    },
  });

  if (!item) {
    return (
      <section
        className="repair-inspector repair-inspector--empty"
        aria-labelledby="repair-inspector-heading"
      >
        <h3 id="repair-inspector-heading">Inspecteur</h3>
        <p>
          Sélectionnez un élément dans la file pour afficher sa preuve et
          décider.
        </p>
      </section>
    );
  }

  const alternatives = alternativeRepairActions(
    item.kind,
    effectiveAction,
    item.resolved,
  );
  // An arbitrated issue is no longer inert: it shows what was decided and
  // offers the answers it does not currently hold.
  const showActions = !effectiveAction || revising;
  const reasonCode = currentDetail?.reason_code ?? item.reason_code;
  const sourceTitle = currentDetail?.source_title ?? item.source_title;
  const sourceUrl = currentDetail?.source_url ?? item.source_url;
  const isRule = item.kind === "rejected_rule";
  // A rule body is rendered in full by RepairRulePanel, inside a bounded,
  // scrollable block. Repeating it unbounded in the facts list pushes every
  // action off-screen for a large YARA rule, so the summary stays short here.
  const value = isRule ? item.preview : (currentDetail?.value ?? item.preview);
  const projectionArtifactId = currentDetail?.artifact_id ?? item.artifact_id;
  const projectionArtifactVersion =
    currentDetail?.artifact_version ?? item.artifact_version;

  return (
    <section
      className="repair-inspector"
      aria-labelledby="repair-inspector-heading"
      aria-live="polite"
    >
      <div className="repair-inspector__heading">
        <div>
          <p className="eyebrow">Élément sélectionné</p>
          <h3 id="repair-inspector-heading">{item.article_title}</h3>
        </div>
        <span className="repair-issue-row__status">
          {repairStatusLabel(item)}
        </span>
      </div>

      <dl className="repair-inspector__facts">
        <div>
          <dt>Type</dt>
          <dd>{item.artifact_type ?? repairKindLabel(item)}</dd>
        </div>
        <div>
          <dt>{isRule ? "Extrait" : "Valeur"}</dt>
          <dd>
            <code className="repair-inspector__value">
              {value || "Valeur non conservée"}
            </code>
            {isRule ? (
              <span className="repair-inspector__value-hint">
                Corps intégral ci-dessous.
              </span>
            ) : null}
          </dd>
        </div>
        <div>
          <dt>Source</dt>
          <dd>
            {item.source_id ?? "—"}
            {sourceTitle ? ` — ${sourceTitle}` : ""}
            {sourceUrl ? (
              <a href={sourceUrl} target="_blank" rel="noreferrer">
                {sourceUrl}
              </a>
            ) : null}
          </dd>
        </div>
        <div>
          <dt>Motif</dt>
          <dd>{repairReasonLabel(reasonCode)}</dd>
        </div>
      </dl>

      <details className="repair-inspector__technical">
        <summary>Code technique et identité</summary>
        <dl>
          <div>
            <dt>reason_code</dt>
            <dd>
              <code>{reasonCode}</code>
            </dd>
          </div>
          <div>
            <dt>repair_key</dt>
            <dd>
              <code>{item.repair_key}</code>
            </dd>
          </div>
          <div>
            <dt>sha256</dt>
            <dd>
              <code>{currentDetail?.value_sha256 ?? item.value_sha256}</code>
            </dd>
          </div>
        </dl>
      </details>

      <section
        className="repair-inspector__provenance"
        aria-labelledby="repair-provenance-heading"
      >
        <h4 id="repair-provenance-heading">Provenance</h4>
        <dl>
          <div>
            <dt>Exécution</dt>
            <dd>
              <code>{item.run_id}</code> — génération {item.pipeline_generation}
            </dd>
          </div>
          <div>
            <dt>Projection d&apos;extraction</dt>
            <dd>
              {projectionArtifactId ? (
                <>
                  <code>{projectionArtifactId}</code>
                  {projectionArtifactVersion !== null &&
                  projectionArtifactVersion !== undefined
                    ? ` v.${projectionArtifactVersion}`
                    : ""}
                </>
              ) : (
                "—"
              )}
            </dd>
          </div>
          <div>
            <dt>Livrable final</dt>
            <dd>
              {article?.document_artifact_id ? (
                <>
                  <code>{article.document_artifact_id}</code>
                  {article.document_artifact_version !== null
                    ? ` v.${article.document_artifact_version}`
                    : ""}
                </>
              ) : (
                "aucun document publiable"
              )}
            </dd>
          </div>
          <div>
            <dt>Empreinte du livrable</dt>
            <dd>
              <code>{article?.document_input_hash ?? "—"}</code>
            </dd>
          </div>
        </dl>
      </section>

      {detail.isPending ? <p role="status">Chargement du détail…</p> : null}
      {detail.isError ? (
        <p className="error-message" role="alert">
          Le détail de cet élément est inaccessible : {detail.error.message}
        </p>
      ) : null}

      {item.kind === "supplemental_source_unarchived" && currentDetail ? (
        <RepairSourcePanel
          editionId={editionId}
          subjectId={item.subject_id}
          detail={currentDetail}
          readOnly={readOnly}
          onArchived={() => {
            onArchived(item);
            onChanged();
          }}
        />
      ) : null}

      {item.kind === "rejected_rule" && currentDetail ? (
        <RepairRulePanel
          detail={currentDetail}
          currentAction={showActions ? null : effectiveAction}
          readOnly={readOnly}
          disabled={decide.isPending}
          onDecision={(nextAction) => decide.mutate(nextAction)}
        />
      ) : null}

      {effectiveAction ? (
        <div className="repair-inspector__current-decision">
          <p className="repair-decision-badge" role="status">
            Décision actuelle : {repairActionLabel(effectiveAction)}
          </p>
          {!readOnly && !revising && alternatives.length > 0 ? (
            <button
              className="button button--secondary"
              type="button"
              disabled={readOnly || decide.isPending}
              onClick={() => setRevising(true)}
            >
              Modifier la décision
            </button>
          ) : null}
        </div>
      ) : null}

      {!readOnly &&
      item.kind !== "rejected_rule" &&
      showActions &&
      alternatives.length > 0 ? (
        <div className="repair-inspector__decision-panel">
          <label htmlFor="repair-decision-reason">
            Raison de la décision (facultatif)
          </label>
          <textarea
            id="repair-decision-reason"
            rows={2}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            disabled={readOnly || decide.isPending}
          />
          <div className="repair-inspector__actions">
            {alternatives.map((nextAction) => (
              <button
                key={nextAction}
                className={
                  nextAction === "exclude"
                    ? "button button--danger"
                    : nextAction === "continue_without_source"
                      ? "button button--secondary"
                      : "button"
                }
                type="button"
                disabled={
                  readOnly ||
                  decide.isPending ||
                  (nextAction === "continue_without_source" &&
                    !item.artifact_id)
                }
                onClick={() => decide.mutate(nextAction)}
              >
                {decide.isPending
                  ? "Enregistrement…"
                  : DECISION_ACTION_LABELS[nextAction]}
              </button>
            ))}
            {revising ? (
              <button
                className="button button--secondary"
                type="button"
                onClick={() => setRevising(false)}
              >
                Annuler
              </button>
            ) : null}
          </div>
        </div>
      ) : null}

      <section
        className="repair-inspector__effective-decision"
        aria-labelledby="repair-effective-decision-heading"
      >
        <h4 id="repair-effective-decision-heading">Décision effective</h4>
        {effectiveAction ? (
          <dl>
            <div>
              <dt>Décision</dt>
              <dd>{repairActionLabel(effectiveAction)}</dd>
            </div>
            <div>
              <dt>Identité</dt>
              <dd>{effectiveDecision?.actor_id ?? "—"}</dd>
            </div>
            <div>
              <dt>Date</dt>
              <dd>
                {effectiveDecision ? (
                  <time dateTime={effectiveDecision.created_at}>
                    {decisionTimestamp(effectiveDecision.created_at)}
                  </time>
                ) : (
                  "—"
                )}
              </dd>
            </div>
            <div>
              <dt>Application</dt>
              <dd>{applicationLabel}</dd>
            </div>
          </dl>
        ) : (
          <p>Aucune décision n’a encore été prise sur cet élément.</p>
        )}
      </section>

      <section
        className="repair-inspector__audit"
        aria-labelledby="repair-audit-heading"
      >
        <h4 id="repair-audit-heading">
          Historique des décisions ({decisionHistory.length})
        </h4>
        {decisionHistory.length > 0 ? (
          <ol
            className="repair-inspector__audit-list"
            aria-label="Historique des décisions"
          >
            {decisionHistory.map((entry) => {
              const isEffective = entry.id === expectedEffectiveDecisionId;
              return (
                <li
                  key={entry.id}
                  className={
                    isEffective
                      ? "repair-audit-entry is-effective"
                      : "repair-audit-entry"
                  }
                >
                  <p className="repair-audit-entry__line">
                    <time dateTime={entry.created_at}>
                      {decisionTimestamp(entry.created_at)}
                    </time>{" "}
                    <span>{entry.actor_id}</span>{" "}
                    <strong>{repairActionLabel(entry.action)}</strong>{" "}
                    <span className="repair-audit-entry__application">
                      {isEffective
                        ? `décision effective — application : ${applicationLabel}`
                        : SUPERSEDED_LABEL}
                    </span>
                  </p>
                  <dl>
                    <div>
                      <dt>Artefact observé</dt>
                      <dd>
                        <code>{entry.observed_artifact_id}</code>
                      </dd>
                    </div>
                    <div>
                      <dt>Génération observée</dt>
                      <dd>{entry.observed_pipeline_generation}</dd>
                    </div>
                    <div>
                      <dt>Raison</dt>
                      <dd>{entry.reason ?? "—"}</dd>
                    </div>
                  </dl>
                </li>
              );
            })}
          </ol>
        ) : (
          <p>Aucune décision enregistrée pour cet élément.</p>
        )}
      </section>

      {error ? (
        <p className="error-message" role="alert">
          {error}
        </p>
      ) : null}
      <p className="repair-inspector__pipeline-link">
        <Link to={`/subjects/${item.subject_id}#production-rejections-heading`}>
          Voir le diagnostic pipeline de cet article
        </Link>{" "}
        <span>
          Le tableau Pipeline conserve les diagnostics techniques détaillés de
          cet article. Les décisions de publication se prennent ici.
        </span>
      </p>
    </section>
  );
}

export { STALE_REPAIR_MESSAGE };
