# Request: available VT resolution pivots refused for missing source references

Investigation `782ff20c-8bfb-4ac0-a1a8-356e8b7bbe03`; operator D; state v169.

Distinct `vt.relationships` invocation `f7a945a6-393b-4c27-ad2f-0cd1c5516b68` (a current IPv4 `resolutions` pivot marked `available`, `blocked_reason=null`) failed before provider access:

- `error_code`: `virustotal_external_effect_refused_source_references_required`
- exact error reason: `The tool could not complete the request. Review this error code and current investigation context, correct the inputs, then retry.`
- canonical observation `8296ee56-f552-5245-a1fd-1d0531724d4c` records status `failed`, reason `external_effect_refused`.

The published `vt.relationships.v1` schema accepts only `object_type`, `object_id`, `relation`, and optional `cursor`; it exposes no source-reference field. The current `available_pivots` entry had no blocked reason. No provider result was obtained and no indicator value is included here. Please reconcile the provenance gate or expose the typed source-reference path. The operator will test one different relation pivot; if the same guard returns, that family will be closed without interpreting it as a negative result.
