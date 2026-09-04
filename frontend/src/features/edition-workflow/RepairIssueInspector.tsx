import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError } from "../../api/editions";
import {
  decideEditionRepair,
  getEditionRepairDetail,
  type EditionRepairItem,
  type ProductionRepairAction,
} from "../../api/publication";
import { RepairRulePanel } from "./RepairRulePanel";
import { RepairSourcePanel } from "./RepairSourcePanel";
import {
  repairKindLabel,
  repairReasonLabel,
  repairStatusLabel,
} from "./RepairQueue";

const STALE_REPAIR_MESSAGE =
  "Cet élément a changé depuis son ouverture. La file de réparation a été rechargée.";

function decisionLabel(action: ProductionRepairAction): string {
  if (action === "continue_without_source") return "Continué sans source";
  return action === "include" ? "Inclus" : "Exclu";
}

export function RepairIssueInspector({
  editionId,
  item,
  readOnly,
  onChanged,
  onArchived,
}: {
  editionId: string;
  item: EditionRepairItem | null;
  readOnly: boolean;
  onChanged: () => void;
  onArchived: (item: EditionRepairItem) => void;
}) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const detail = useQuery({
    queryKey: ["edition-repair-detail", editionId, item?.repair_key],
    queryFn: () => getEditionRepairDetail(editionId, item?.repair_key ?? ""),
    enabled: Boolean(item),
  });
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
        reason: reason || null,
      });
    },
    retry: false,
    onSuccess: () => {
      setError(null);
      setReason("");
      void queryClient.invalidateQueries({
        queryKey: ["edition-repair-detail", editionId, item?.repair_key],
      });
      onChanged();
    },
    onError: (mutationError: unknown) => {
      if (
        mutationError instanceof ApiError &&
        (mutationError.code === "production_repair_stale" ||
          mutationError.code === "production_repair_resolved")
      ) {
        setError(STALE_REPAIR_MESSAGE);
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

  const currentDetail = detail.data;
  const action =
    currentDetail?.effective_decision?.action ?? item.effective_action;
  const resolved = item.resolved || Boolean(currentDetail?.effective_decision);
  const reasonCode = currentDetail?.reason_code ?? item.reason_code;
  const sourceTitle = currentDetail?.source_title ?? item.source_title;
  const sourceUrl = currentDetail?.source_url ?? item.source_url;
  const isRule = item.kind === "rejected_rule";
  // A rule body is rendered in full by RepairRulePanel, inside a bounded,
  // scrollable block. Repeating it unbounded in the facts list pushes every
  // action off-screen for a large YARA rule, so the summary stays short here.
  const value = isRule ? item.preview : (currentDetail?.value ?? item.preview);

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
          <div>
            <dt>run_id</dt>
            <dd>
              <code>{item.run_id}</code>
            </dd>
          </div>
          <div>
            <dt>pipeline_generation</dt>
            <dd>{item.pipeline_generation}</dd>
          </div>
        </dl>
      </details>

      {detail.isPending ? <p role="status">Chargement du détail…</p> : null}
      {detail.isError ? (
        <p className="error-message" role="alert">
          Le détail de cet élément est inaccessible : {detail.error.message}
        </p>
      ) : null}

      {item.kind === "supplemental_source_unarchived" && currentDetail ? (
        <RepairSourcePanel
          subjectId={item.subject_id}
          detail={currentDetail}
          readOnly={readOnly}
          resolved={resolved}
          onArchived={() => {
            onArchived(item);
            onChanged();
          }}
        />
      ) : null}

      {item.kind === "rejected_rule" && currentDetail ? (
        <RepairRulePanel
          detail={currentDetail}
          resolved={resolved}
          disabled={readOnly || decide.isPending}
          onDecision={(nextAction) => decide.mutate(nextAction)}
        />
      ) : null}

      {item.kind !== "supplemental_source_unarchived" &&
      item.kind !== "rejected_rule" &&
      !resolved ? (
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
            <button
              className="button"
              type="button"
              disabled={readOnly || decide.isPending}
              onClick={() => decide.mutate("include")}
            >
              Inclure dans la fiche
            </button>
            <button
              className="button button--danger"
              type="button"
              disabled={readOnly || decide.isPending}
              onClick={() => decide.mutate("exclude")}
            >
              Exclure
            </button>
          </div>
        </div>
      ) : null}

      {item.kind === "supplemental_source_unarchived" && !resolved ? (
        <div className="repair-inspector__decision-panel">
          <button
            className="button button--secondary"
            type="button"
            disabled={readOnly || decide.isPending || !item.artifact_id}
            onClick={() => decide.mutate("continue_without_source")}
          >
            {decide.isPending
              ? "Enregistrement…"
              : "Continuer sans cette source"}
          </button>
        </div>
      ) : null}

      {resolved && action ? (
        <p className="repair-decision-badge" role="status">
          {action === "include"
            ? "Inclus par décision analyste"
            : decisionLabel(action)}
        </p>
      ) : null}

      {currentDetail?.effective_decision ? (
        <details className="repair-inspector__audit">
          <summary>Audit de la dernière décision</summary>
          <dl>
            <div>
              <dt>Identité</dt>
              <dd>{currentDetail.effective_decision.actor_id}</dd>
            </div>
            <div>
              <dt>Raison</dt>
              <dd>{currentDetail.effective_decision.reason ?? "—"}</dd>
            </div>
            <div>
              <dt>Date</dt>
              <dd>{currentDetail.effective_decision.created_at}</dd>
            </div>
          </dl>
        </details>
      ) : null}

      {error ? (
        <p className="error-message" role="alert">
          {error}
        </p>
      ) : null}
      <details className="repair-inspector__pipeline-link">
        <summary>Voir le diagnostic pipeline</summary>
        <p>
          Le tableau Pipeline conserve les diagnostics techniques détaillés de
          cet article. Les décisions de publication se prennent ici.
        </p>
      </details>
    </section>
  );
}

export { STALE_REPAIR_MESSAGE };
