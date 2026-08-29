# Analyst investigation

`AnalystInvestigationStatus`: `queued`, `running`, `awaiting_review`, `completed`, `exhausted`, `failed`, `cancelled`.
`AnalystInvestigationStage`: `seeds`, `features`, `tooling`, `invariants`, `pivots`, `corpus`, `detection`, `note`.

The state machine starts `queued -> running`; a running investigation can await review, complete, exhaust, fail for a technical error, or be cancelled. A reviewable investigation can complete, fail technically, or be cancelled. A cycle without a validated new member, or reaching `max_cycles`, transitions to `exhausted`.

`LoopBudget` has `max_cycles=3` and caller-supplied maxima for pivot runs, acquired hits, new samples, and VT read units. `LoopBudgetCategory` types every consumed category; each increment is persisted and rejected before it exceeds its maximum.

Publication production progression is a single pipeline: `SOURCES -> REFERENCES -> EXTRACTION -> SYNTHESIS -> ASSEMBLY`. Analyst investigations remain an independent, explicitly launched subsystem and are not production stages.

`AnalystDecision` is append-only and investigation-scoped. It records a typed decision and target, target id, actor, reason, correlation id, and occurrence timestamp; it does not extend editorial `HumanDecision`.
