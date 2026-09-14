# Frontend — React / TypeScript strict / Vite / TanStack Query

## Rules

- No `any`.
- Server state belongs in TanStack Query, not `useState`.
- Preserve strict TypeScript types.
- Keep API access in the existing API layer instead of embedding fetch logic
  in components.

When running under MetaHarness bounded execution, the rules above remain
authoritative, but MetaHarness owns repository discovery, mutable scope and
deterministic validation. Do not broaden the contract or run unrelated suites.

## Validation

Run the narrowest relevant test first.

For frontend-only changes:

    cd frontend && pnpm test --run
    cd frontend && pnpm lint
    cd frontend && pnpm typecheck

Do not run backend checks for a frontend-only change unless the API contract
also changed.
