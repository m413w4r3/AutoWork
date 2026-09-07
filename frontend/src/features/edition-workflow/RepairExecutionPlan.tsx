import type { RepairExecutionPlan } from "../../api/publication";
import {
  repairPlanActionLabel,
  repairPlanCostLabel,
  repairPlanImpactLabel,
  repairPlanSubtitle,
} from "./repairExecutionPlan";

export function RepairExecutionPlanView({
  plan,
  compact = false,
}: {
  plan: RepairExecutionPlan;
  compact?: boolean;
}) {
  const noDeliverableChange = plan.impact_kind === "no_deliverable_change";
  return (
    <section
      className={`repair-execution-plan${compact ? " repair-execution-plan--compact" : ""}`}
      aria-labelledby={compact ? undefined : "repair-execution-plan-heading"}
    >
      {!compact ? (
        <h4 id="repair-execution-plan-heading">Impact et exécution</h4>
      ) : null}
      <strong>{repairPlanActionLabel(plan)}</strong>
      <p>{repairPlanSubtitle(plan)}</p>
      <p>
        {plan.ready_to_apply
          ? "Cette réparation a nécessité"
          : "Cette correction entraînera"}{" "}
        : {repairPlanImpactLabel(plan)} — {repairPlanCostLabel(plan)}
      </p>
      <p className="repair-execution-plan__cost" role="status">
        {repairPlanCostLabel(plan)}
      </p>
      {noDeliverableChange && plan.ready_to_apply ? (
        <p role="status">Décision appliquée — aucun contenu à reconstruire.</p>
      ) : null}
      {!plan.ready_to_apply && !noDeliverableChange ? (
        <p>Plan en attente de la décision ou de la source.</p>
      ) : null}
      {!compact && plan.deterministic_steps.length > 0 ? (
        <ol
          className="repair-execution-plan__steps"
          aria-label="Étapes prévues"
        >
          {plan.deterministic_steps.map((step, index) => (
            <li key={`${step}-${index}`}>
              <span aria-hidden="true">{index === 0 ? "✓" : "→"}</span> {step}
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}
