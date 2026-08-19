# AutoWork Implementer

Read `.ai/current-task.md` first if it exists, then locate the relevant
implementation and its tests before editing. If the plan contradicts the
repository, stop and report the conflict — do not redesign.

Make the smallest coherent change. Reuse existing abstractions.
No drive-by refactors.

Validation, in order:
1. Targeted tests while iterating.
2. `make lint && make typecheck && make test` before reporting done.
3. `make test-integration` as well if persistence, repositories or
   migrations were touched.
4. `git status --short` and `git diff --stat` to confirm the diff
   contains only intended files.

Do not commit or push unless asked.
