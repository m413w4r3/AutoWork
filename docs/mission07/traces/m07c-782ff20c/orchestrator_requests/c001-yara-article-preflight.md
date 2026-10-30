# Request to orchestrator — article-claim preflight remains unresolved

Investigation `782ff20c-8bfb-4ac0-a1a8-356e8b7bbe03`; operator C; canonical state v153.

Two distinct `advisor.consult` `yara_draft` dossiers (`c019` and `c031`) were stopped at `evidence_preflight` with `required_canonical_evidence_not_in_payload`, `confirmed_non_execution=true`, `sent=0`, and `bridge_turns_consumed=0`. Both named claims S3P2.1 and S4P1.1 were reported absent from the loaded article snapshot. The second dossier also included a fresh successful `article.get_claims` observation for those exact claim IDs, but the same source references remained unresolved.

Please expose the accepted canonical article-source reference or repair the resolver/snapshot path for these claims. No raw claim passages, sample hashes, or operational credentials are included. The operator has not retried either Bridge dossier.
