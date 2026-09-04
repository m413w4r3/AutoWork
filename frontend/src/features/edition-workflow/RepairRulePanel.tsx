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
  disabled,
}: {
  detail: EditionRepairDetail;
  /** Effective action, or null while the rule is not arbitrated yet. */
  currentAction: ProductionRepairAction | null;
  onDecision: (action: ProductionRepairAction) => void;
  disabled: boolean;
}) {
  const alternatives = alternativeRepairActions("rejected_rule", currentAction);
  return (
    <section
      className="repair-rule-panel"
      aria-labelledby="repair-rule-heading"
    >
      <h4 id="repair-rule-heading">
        {detail.artifact_type ?? "Règle de détection"}
      </h4>
      {detail.body !== null && detail.body !== undefined ? (
        <pre className="repair-rule-panel__body">
          <code>{detail.body}</code>
        </pre>
      ) : (
        <p>
          Le corps intégral de cette règle n&apos;est pas disponible dans le
          pack d&apos;évidence chargé.
        </p>
      )}
      {currentAction ? (
        <p className="repair-decision-badge" role="status">
          Décision actuelle : {repairActionLabel(currentAction)}
        </p>
      ) : null}
      {alternatives.length > 0 ? (
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
