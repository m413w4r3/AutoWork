# Request: article claim passages remain absent from Bridge snapshot

Investigation `782ff20c-8bfb-4ac0-a1a8-356e8b7bbe03`; operator D; state v164.

The distinct Bridge YARA consultation `d029-yara-bridge-candidate` created advisor invocation `eb392d6b-1b5f-4cb6-a492-8053f6169487` and failed before provider submission:

- `error_code`: `advisor_preflight_insufficient_evidence`
- `failure_stage`: `evidence_preflight`
- `failure_reason`: `required_canonical_evidence_not_in_payload`
- `confirmed_non_execution`: `true`
- `bridge_turns_consumed`: `0`
- `usable`: `false`

The preflight reports the same missing article artifact ref, `article:8eb9cbe0fade4cffe00fb0f5f645a5f2dd074ce431e07c16a0e487a205bdd618`, for both passages:

- `Claim passage s3p2.1 is referenced but absent from the loaded snapshot.`
- `Claim passage s4p1.1 is referenced but absent from the loaded snapshot.`

The request included both claim IDs and their direct passage text in prose. The current article resolver still did not load either claim. No source passage, sample bytes, indicator value, or credential is included here. Please expose the supported claim-loading path or repair the snapshot resolver. The operator is continuing with a separate static-evidence-only dossier and has not repeated this request.
