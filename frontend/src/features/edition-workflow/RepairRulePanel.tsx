import type {
  EditionRepairDetail,
  ProductionRepairAction,
} from "../../api/publication";

export function RepairRulePanel({
  detail,
  resolved,
  onDecision,
  disabled,
}: {
  detail: EditionRepairDetail;
  resolved: boolean;
  onDecision: (action: ProductionRepairAction) => void;
  disabled: boolean;
}) {
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
      {resolved ? null : (
        <div className="repair-inspector__actions">
          <button
            className="button"
            type="button"
            disabled={disabled}
            onClick={() => onDecision("include")}
          >
            Inclure la règle dans le livrable
          </button>
          <button
            className="button button--danger"
            type="button"
            disabled={disabled}
            onClick={() => onDecision("exclude")}
          >
            Exclure la règle
          </button>
        </div>
      )}
    </section>
  );
}
