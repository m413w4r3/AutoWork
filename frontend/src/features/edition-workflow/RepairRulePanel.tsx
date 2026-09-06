import type {
  EditionRepairDetail,
  ProductionRepairAction,
} from "../../api/publication";
import { alternativeRepairActions, repairActionLabel } from "./RepairQueue";

const RULE_ACTION_LABELS: Record<string, string> = {
  include: "Inclure la règle dans le livrable",
  exclude: "Exclure la règle",
};

export function RepairRulePanel({
  detail,
  currentAction,
  onDecision,
  readOnly,
  disabled,
}: {
  detail: EditionRepairDetail;
  /** Effective action, or null while the rule is not arbitrated yet. */
  currentAction: ProductionRepairAction | null;
  onDecision: (action: ProductionRepairAction) => void;
  /** A historical review keeps the rule body readable and offers no action. */
  readOnly: boolean;
  disabled: boolean;
}) {
  // A rule whose full body could not be recovered cannot be included: the
  // projection would have no body to write into the sidecar.
  const bodyAvailable =
    detail.payload_available !== false &&
    detail.body !== null &&
    detail.body !== undefined;
  const alternatives = alternativeRepairActions(
    "rejected_rule",
    currentAction,
  ).filter((action) => bodyAvailable || action !== "include");
  return (
    <section
      className="repair-rule-panel"
      aria-labelledby="repair-rule-heading"
    >
      <h4 id="repair-rule-heading">
        {detail.artifact_type ?? "Règle de détection"}
      </h4>
      {bodyAvailable ? (
        <pre className="repair-rule-panel__body">
          <code>{detail.body}</code>
        </pre>
      ) : (
        <>
          <p>
            Corps intégral non récupérable depuis les preuves archivées : cette
            règle ne peut pas être incluse.
          </p>
          <p>
            Relancez Extraction pour régénérer les preuves si vous souhaitez
            réexaminer cette règle.
          </p>
        </>
      )}
      {currentAction ? (
        <p className="repair-decision-badge" role="status">
          Décision actuelle : {repairActionLabel(currentAction)}
        </p>
      ) : null}
      {!readOnly && alternatives.length > 0 ? (
        <div className="repair-inspector__actions">
          {alternatives.map((action) => (
            <button
              key={action}
              className={
                action === "exclude" ? "button button--danger" : "button"
              }
              type="button"
              disabled={disabled}
              onClick={() => onDecision(action)}
            >
              {RULE_ACTION_LABELS[action] ?? repairActionLabel(action)}
            </button>
          ))}
        </div>
      ) : null}
    </section>
  );
}
