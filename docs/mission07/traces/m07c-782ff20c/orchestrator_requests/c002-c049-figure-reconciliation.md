# Request to orchestrator — reconcile the current figure_plan advisor invocation

Investigation `782ff20c-8bfb-4ac0-a1a8-356e8b7bbe03`; operator C; state v157.

The distinct `advisor.consult` figure_plan invocation `6a157f4a-bbea-47ee-b1bb-5e553df797fd` remains canonically `running` after about ten minutes, with no response refs. `/operator-status` lists it under both `invocations_requiring_reconciliation` and `ambiguous_invocations`, with `evidence_missing=provider response or reconciliation evidence` and `decision_expected=reconcile before resume`. Bridge budget now shows one additional reserved turn; provider usage is unknown.

Please reconcile this exact invocation and reservation when possible. The operator has not replayed it or submitted another Bridge consultation. No CTI indicators or raw evidence are included.
