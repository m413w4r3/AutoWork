# AutoWork Implementer

Before editing:
1. Read `.ai/current-task.md` when present.
2. Use `codebase_search` for initial discovery.
3. Inspect relevant implementation and existing tests.
4. Verify task assumptions against the current repository.

Implement the smallest coherent change that satisfies the task.
Prefer existing abstractions and patterns.
Avoid unrelated refactors.

Use targeted tests while iterating.

Before completion, normally run:
- `make lint`
- `make typecheck`
- `make test`

If persistence, repositories or migrations changed, also run:
- `make test-integration`

Finish by inspecting:
- `git status --short`
- `git diff --check`
- `git diff --stat`

Never discard unrelated user changes.
Do not commit or push unless explicitly requested.

If the task plan conflicts materially with the repository and resolving it
requires an architectural decision, stop and report the conflict instead
of inventing a new design.
