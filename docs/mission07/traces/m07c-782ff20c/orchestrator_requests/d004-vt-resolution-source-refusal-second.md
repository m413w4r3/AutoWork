# Request: alternate VT resolution pivot hits the same provenance gate

Investigation `782ff20c-8bfb-4ac0-a1a8-356e8b7bbe03`; operator D; state v170.

A second, different IPv4 `resolutions` pivot (`vt.relationships.v1`, invocation `1e190bfe-17f9-40a7-b027-0c5c28b7749a`) was also refused before provider access:

- `error_code`: `virustotal_external_effect_refused_source_references_required`
- exact error reason: `The tool could not complete the request. Review this error code and current investigation context, correct the inputs, then retry.`
- canonical observation `97370ae7-c274-5903-8ed4-8c843d27835c` records status `failed`, reason `external_effect_refused`.

This is a distinct object from the first refusal in d003; the published schema still exposes no source-reference argument and the pivot catalog did not flag the candidate as blocked. No provider result or indicator value is included. The operator is closing this `resolutions` branch after two distinct refusals and continues with other available passive/static pivots.
