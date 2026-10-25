# Mission 05 — architecture implementation and deletion test

Status: **PENDING M05-A**. This file will record only what the integrated
commit and executed tests prove.

## Modules before / after

| Aspect | Before (`55fba67d`) | After (M05-A) |
|---|---|---|
| Renderer implementation | `InvestigationAdvisorTool._prompt` inside `investigation_advisor_tools.py` | — |
| Preparation dependency | `advisor_consultation.prepare` imports `InvestigationAdvisorTool` at call time | — |
| Legacy path | `_fit_advisor_prompt` → private static method | — |
| Renderer engines | 2 access paths, 1 body (private) | — |
| Public preparation interface | `prepare(projection) -> PreparedConsultation \| PreparationRefusal` | — |

## Direct imports and dependency direction

- Before: `advisor_consultation.py` → `investigation_advisor_tools.py`
  (illegitimate inversion). Evidence: `advisor_consultation.py:196-202`
  at baseline.
- After: to be recorded with the AST/source check showing
  `advisor_consultation.py` and the pure renderer never import
  `investigation_advisor_tools.py`.

## Caller obligations and interface size

| Question | Before | After |
|---|---|---|
| Public entries a caller must know | — | — |
| Objects/invariants the caller must compose | — | — |
| Prompt renderings duplicated | — | — |
| Tests through the public interface | — | — |
| Interface fields/params + learned invariants | — | — |

## Byte parity and golden corpus

- Golden cases: — (types, units, aliases, Unicode, CRLF, JSON-like CTI,
  Base64/hex, refs/versions/SHA, refusal/completeness).
- Byte-for-byte / SHA-256 / length / token-cost equality: —.
- Baseline anti-slop fingerprint: `54c9f454f0e80c95e518279c6a3205a776714db84964039a1fdaa0c7ebecafab`.
- Refusals and zero fake sends: —.

## Deletion test

- — (describe how the legacy private method is removed or reduced to a
  delegation, and how `prepare` plus the public caller keep working when the
  legacy method is made to raise).

## Residual risks

- —
