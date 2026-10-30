# Request: Bridge unreachable during a static-only YARA preflight

Investigation `782ff20c-8bfb-4ac0-a1a8-356e8b7bbe03`; operator D; state v165.

Distinct advisor invocation `39a2c0b8-1dba-4ccf-9dc6-a0ae6fc2b6a3` (`d042-yara-static-only`) returned:

- `error_code`: `advisor_unavailable`
- `advisor_status`: `unavailable`
- `failure_stage`: `preflight`
- `failure_reason`: `bridge_unreachable`
- `confirmed_non_execution`: `true`
- `bridge_turns_consumed`: `0`
- no output SHA or Bridge response

The dossier contained only current static evidence refs and omitted article claims. The request has not been replayed. Please restore/confirm Bridge availability; the operator will wait several minutes before the single permitted retry and will continue passive VT pivots meanwhile. No raw evidence, indicator, sample bytes, or credentials are included.
