# Frontend — React / TypeScript strict / Vite / TanStack Query

## Rules

- No `any`.
- Server state belongs in TanStack Query, not `useState`.
- Preserve strict TypeScript types.
- Keep API access in the existing API layer instead of embedding fetch logic
  in components.

## Validation

Run the narrowest relevant test first.

For frontend-only changes:

    cd frontend && pnpm test --run
    cd frontend && pnpm lint
    cd frontend && pnpm typecheck

Do not run backend checks for a frontend-only change unless the API contract
also changed.
