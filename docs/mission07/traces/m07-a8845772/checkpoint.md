# Mission 07 — checkpoint CTI-ANALYST

Mise à jour : 2026-10-28 08:18:25 UTC.

## État canonique

- Investigation `a8845772-5025-4702-8850-2b6be776855b`, sujet `d22822d4-27ce-47d2-9cc5-f84cf312986a`, run `27478dcc-69d6-4593-89fb-7bd45e9c3537`, `operator_led`, `running`. Working-state v69, SHA `e96388f985db93e098fcd5700b04606faac9c5871ea94f7f06f441b3f302af88`.
- Synthèse `4d37c738-d78f-4087-b251-3c38a56e8e53` v1, SHA `8eb9cbe0fade4cffe00fb0f5f645a5f2dd074ce431e07c16a0e487a205bdd618`. Politique OSINT_OPEN `dc034634-0e36-57cd-ba4e-a61bb2bb736a` v1 active; G2 approuvé; TLP CLEAR; `external_llm_allowed=true`, `do_not_submit=false`.

## Dernier essai Bridge et blocage

- `e009-bridge-plan` invocation `bc4acebb-2d66-4543-aeba-fc12fd64aa30` : préflight preuves incomplet (65/68), 0 tour, aucune transmission.
- Unique dossier corrigé `e015-bridge-plan-corrected`, invocation `11db4592-7107-47e5-955d-e78c27ecd0e1`; observation `e384ca82-4535-514a-a61d-3c78faa1b240`, SHA `c5b659ab36fc262bcbebcb6c55207e69edda6bed9aece6588f282fb677d52e55`. `preflight_status=READY` (65/65 présentes, 3 omises), puis échec prétransport `external_effect_authority_required`; `confirmed_non_execution=true`, aucun `bridge_invocation_id`, 0 tour, 0 octet de réponse, usage fournisseur unavailable. Aucun conseil reçu.
- Le préflight a créé le blob interne `b23aa970-ef0f-4a92-b698-b69f07704ff1`, 353028 octets, SHA `b4b1777f56db5ebe33b2136e35d2bb62a543402933724a3c6156e16561bb0a85`; manifeste : samples en Base64. Blob non lu, non transmis, non supprimé. Ne pas récupérer ni supprimer pendant l'audit.
- Instruction actuelle : arrêter les consultations. L'utilisateur fera un audit externe si le blocage persiste. Attendre son retour.

## Findings et avis

- H1 `f6f0a34e-8207-5e1c-9040-b17cb49b95d8`, H2 `2b02047e-949e-500f-97ce-dc52a2f41ac2`, H3 `dd157c98-921e-5194-b863-f00fc899ebc8`, H4 `746907e4-ccde-53a5-b073-0e189976758c`: tous `qualified`; aucun `confirmed`.
- Avis interne `128f991d-09c9-5cb5-905c-d0c044bfff00` v1, SHA `6a7529373a213b5b263a6aa06a8304e4519bb9862cb64f23c3a4ef9ba410dcbd`. Contenu non vérifié phrase par phrase; 3 révisions ont échoué `citations_invalid`. Non Bridge-révisé, non approuvé, non publié; aucun PDF.

## Budget observé à v69

- Bridge : 0 consommé, 13 réservés, 187 restants; VT : 22 consommées, 13 réservées, 9965 restantes; downloads : 0/1000; tool_seconds : 31 consommés, 47040 réservés, 39329 restants; Shodan : 0.

## Fichiers et Git

- Dossier détaillé local : `analyst_log.md`; relais append-only : `journal.jsonl`. Ces fichiers et ce checkpoint sont ignorés par Git.
- Handoff suivi : `docs/mission07/M07_HANDOFF.md`, section de relève datée 2026-10-28 08:18 UTC.
- Handoff commité sur `main` et poussé par fast-forward : `4a18e688` (`docs(mission07): handoff after blocked Bridge retry`). `HEAD` et `origin/main` sont synchronisés à `4a18e688`; working tree propre. Push a publié les 33 commits précédemment en avance et le commit de relève (34 au total).
- Aucun appel AutoWork ni test supplémentaire effectué pendant la préparation du handoff; arrêt demandé après le push.

## Addendum CTI-ANALYST-6 — 2026-10-28 16:25 UTC

This addendum supersedes the earlier instruction in this checkpoint to stop Bridge consultations: the current explicit analyst assignment requires a post-analysis consultation and an opinion review. The earlier stop instruction was session-specific and is not the present authorization.

### Canonical state and relay

- Last canonical status read: working-state v109, SHA `820da05eda6e8dc55ed604857479d5dd76cd3789557f147a005f0d5e154966ff`; investigation still `running`, no active jobs. The latest snapshot was taken before the outstanding Bridge request.
- One `advisor.consult` pivot-strategy POST (`c6-084-bridge-post-analysis`) has been placed in the inbox with observations only (no raw-byte observations). No outbox response exists yet. A subsequent status GET (`c6-085-status-after-bridge-submit`) is queued behind it. Do not replay the consultation; check the original outbox and then the queued status.
- Most recent guidance showed 182 G2 Bridge turns remaining before the pending call; current spend is unknown until its result arrives. No VT calls, downloads, or Shodan calls were made in this analyst-6 continuation.

### Static analysis added

- RuntimeBroker SHA `53f35e49eb9b271fd8cbcd3daacb525328dbf159a03dbd1c7adebe0363daa402`: methods inventory complete (207 methods; observations `0b354777-c78d-59a2-a586-ab04668d7d96` SHA `9a0102e21b43e04e9c73ed89dd2730a42fb7541549aae31404d71e7ef918ac28`, and `c88fe8d9-b96c-5c96-bd86-ce3a7bdcea1a` SHA `86c24e5aa34eea573aa103720c2dc93bed99632add9de0f237b0e91779f8870c`).
- Static CIL: `InitializeNewDomain` sets CurrentDirectory to the AppDomain BaseDirectory and invokes a default-domain helper (`ff209343-f085-5dae-a721-79b51b079663`, SHA `e9ef1800ecce17ffd546e815062805edaa35d9841572fc75286f8a2c6699c3fd`). The helper sets HTTP service-point options, installs exception handlers, starts 63s/120s timers, and enters Application.Run (`60e6a38a-ac6c-5896-a080-c7de01da2dda`, SHA `c4946f7ef708e8eb8b886cd3085a14d5e7d124fdf2a9bc064c5a8c7fe94d2e14`). The inspected timer callbacks perform local path/visibility and file/registry checks (`0d439fa8-94f0-579e-ba30-808bc30a5034`, SHA `d0cd4c99e2d8e7826c6c20c17c16a8a6ee2dccc292b38a7e7e06d5477349dc56`; `f47a08b3-43e3-5397-bae3-d7fb0cff5886`, SHA `efe949b850a96533a1364a0c4b6c7a34e53e82341ad91e68e0e63b0a528fa368`).
- RID 20 uses generic WebClient request/download/regex/base64/conditional-upload logic (`efe8966c-957c-5922-a009-c23aa00f9082`, SHA `538d75a3eac06364c38c5585c6564c8068395d24736f733204253e2242de3c4f`). This does not expose an endpoint, GitHub API, call sites, active C2, or campaign linkage.
- RID 207 initializes an embedded byte array and applies an index-based XOR transform (`d0a069d2-22b7-5169-afe6-a45501a917bd`, SHA `2b1df2cd1d7458032e198024d1c37c65bfcfdcd23185b6a9a6923c51147cd44b`). Decoded strings were not produced or logged. Existing FLOSS completed but yielded no readable GitHub/ETW/config indicators; absence is limited by the obfuscation.
- The prior Bridge plan’s suggested archive-member hash comparison is adapted as a priority if a supported safe tool route is found. No raw sample bytes were cited, no malware was executed, and no network/C2 contact occurred.

### Findings, opinion, and blocker

- H1 `f6f0a34e-8207-5e1c-9040-b17cb49b95d8`, H2 `2b02047e-949e-500f-97ce-dc52a2f41ac2`, H3 `dd157c98-921e-5194-b863-f00fc899ebc8`, H4 `746907e4-ccde-53a5-b073-0e189976758c` remain `qualified`; no new finding or opinion revision has been committed yet.
- Current opinion remains `128f991d-09c9-5cb5-905c-d0c044bfff00` v1, SHA `6a7529373a213b5b263a6aa06a8304e4519bb9862cb64f23c3a4ef9ba410dcbd`.
- The canonical opinion GET route returned HTTP 403 `operator_client_scope_denied` (`c6-080-opinion-api`). Context exposes only opinion metadata, not blocks. Stop that GET path; the `opinion.revise` tool schema supports a full block replacement using the current opinion version and SHA. Existing `citations_invalid` revisions had factual blocks without supported findings; any new draft must use supported finding citations for `fact`, otherwise `inference`/`hypothesis` qualifications.

### BLOCKER update — 2026-10-28 16:28 UTC

- The one post-analysis Bridge consultation (`c6-084-bridge-post-analysis`) remains in the inbox with no outbox after approximately eight minutes. The queued status GET (`c6-085-status-after-bridge-submit`) also has no result. This outcome is unresolved, not a failure or completed turn. No retry or further relay submission was made.
- Stop the Bridge path until this original request returns or relay service is restored. Last confirmed canonical state remains v109 / SHA `820da05eda6e8dc55ed604857479d5dd76cd3789557f147a005f0d5e154966ff`.
- Because the relay is serialized and blocked, the requested post-analysis verdict, finding updates, opinion revision, and pre-final review_text consultation remain outstanding. Opinion v1 is still not reviewed or approved.

### Bridge pending request resolved — 2026-10-28 16:31 UTC

- `c6-084-bridge-post-analysis` completed as `partial`, Bridge turn `6e75af9a-e583-4c7d-b4b3-9ea39c0bd1a0`, output SHA `bd02a44428b5d81818c63c60f64ee4b12de1a58c674e019089a59d1212905309`; it consumed one turn. The queued status request returned v110 / SHA `ef6cd02c5153b87bc7398ef633d3f3b0c23c35903c026ba485e48fadcd4bbec0`, no jobs, 181 G2 Bridge turns remain. The previous pending/serialized-relay blocker is resolved.
- Bridge's structured recommendation is an exact SHA-256 comparison of the two ZIP members against the standalone DLLs. Accept the test criterion (exact equality proves inclusion in this archive; no equality excludes those exact candidates only). Defer implementation because the catalog has no sanctioned archive extraction/member-hash tool. Existing central-directory evidence confirms filenames only, and previous notes say no member bytes or hashes were extracted. Do not retry the refused VT `bundled_files` call. ETW effectiveness, GitHub fallback, and campaign linkage remain open.
- No finding or opinion mutation has yet followed this consultation. Required final `review_text` consultation remains outstanding.

### Current analyst-6 milestone — 2026-10-28 16:36 UTC

- Canonical last confirmed state: v113, SHA `a2dee3c571ef5c49cd229507c7e551ace3ac3f998dcf5e1f3bb0991347bbdb3f`; no active jobs; Bridge turns remaining 181 before the pending review call.
- Post-analysis Bridge plan consultation completed as `partial` (one turn). The exact archive-member SHA-256 comparison remains the best discriminating test, but is deferred because no sanctioned archive extraction/member-hash operation exists. Do not replay ZIP `bundled_files` `needs_review` invocation.
- New sample-only capability finding `91d9ff4d-193a-5773-96b0-56bc3b1efd97` v1 is `qualified`, citing direct RID20 CIL observation `efe8966c-957c-5922-a009-c23aa00f9082`. The tool did not retain `supported`; do not retry to force it.
- H2 current immutable revision is `a507ceb9-1f82-51c3-bef6-f01f63fc94ad` v2, `qualified`, with bounded CIL/import evidence and explicit limits. H1, H3, H4 remain qualified.
- Opinion `128f991d-09c9-5cb5-905c-d0c044bfff00` is revised to draft v2, SHA `a7baeab86dcd36fab671aead80fc154989d42770f23eae06d62bb1fdc856a300`. Four inference/hypothesis blocks; no factual blocks. It does not equate code presence with execution and lists remaining archive, ETW, GitHub, and attribution questions.
- Required `review_text` POST `c6-094-bridge-review-opinion-v2` has been submitted once; no outbox result yet. No further POST until this one returns. Afterward, record the advice decision, make any justified final revision, update status/checkpoint, and report final opinion SHA. Draft remains unapproved/unpublished.

### 2026-10-28 16:47 UTC — Analyst #6 final relay status

- First review request `c6-094` was blocked before transport by invalid body-unit anchors; no Bridge turn was consumed. The source validator requires `anchor: null` for `form: body` units.
- One corrected request `c6-104-bridge-review-opinion-v2-corrected` was queued after canonical status v114 / SHA `cfd9dfc4274e591158b40d0bfd3d82cc617006d9c97eb0c20019db5c49548279` (181 Bridge turns then remained; no active jobs). Its units use null anchors and its evidence refs are static-analysis observations only.
- At 16:47 UTC, `c6-104` remains in `inbox/` with no matching `processed/` or `outbox/` file. Read-only status request `c6-105-status-review-pending` is likewise unprocessed. No evidence shows the corrected request reached Bridge. The last confirmed canonical status is v114; status has not been refreshed since the queued POST.
- BLOCKER: relay queue is stalled. Do not retry or duplicate while `c6-104` remains queued; reconcile it after relay recovery because a late pickup is possible. Opinion `128f991d-09c9-5cb5-905c-d0c044bfff00` v2 SHA `a7baeab86dcd36fab671aead80fc154989d42770f23eae06d62bb1fdc856a300` remains draft and unreviewed by Bridge.

- 16:48 UTC recheck after user signal found no change: `c6-104` and `c6-105` remain only in `inbox/`, with no `processed/` or `outbox/` results. No Bridge receipt is evidenced locally; do not duplicate the queued consultation.

### Addendum CTI-ANALYST-7 — 2026-10-28 19:36 UTC

- The corrected review request `c6-104` resolved: invocation `fcceac6c-6740-494a-b163-dd25bba5e651`, one Bridge batch `9f6ec75c-d26f-45d8-b938-9e74ba6f903d`, status `partial`, output SHA `d904208b891e1d1bee80c6314baf5b755f36ef2aac4fdc1df67ec01960e47068`; `usable=false`, no normalized reviews. Opinion v2 is still not validly Bridge-reviewed. Its queued status GET returned v115 / SHA `7f97cb16a7e054b5397b6dfa56fe5f4d47fe7c254475638f49934b52dbba7f32`.
- Most recent status snapshot available from the orchestrator is v121 / SHA `77ee68344e50e90f961ed2f70d7debfd343bdaa3445d52d8b1b70f39ec11e19d`, no active jobs, current opinion v2 draft SHA `a7baeab86dcd36fab671aead80fc154989d42770f23eae06d62bb1fdc856a300`. A later raw-byte pivot smoke completed at v122, status `partial`, one Bridge turn, output SHA `a99641f70fa37b3d1059756c70f01cd9987f865c9430d5d6498c9f509707537c`; Bridge endorsed exact member-hash comparison as the discriminator and explicitly had no hash confirming identity. Treat this as advice, not evidence. No fresh `/operator-status` has been read since v122.
- The existing ZIP observations are available: `zip_members` `7847dd28-c0b2-59a3-a442-a209f8f227c3` SHA `2ae9c058d1a5f142d14441fccee7800863a244724521df56072787856e7897ea`; `archive_list` `fdb307ec-cc01-5760-b7ea-422145f64ade` SHA `e13b61b901ad8bf8f925f746ecb8d0aafa08e3fbc0a674766367bbee78923caf`; `archive_extract_hash` `7074781e-4029-5ef8-b0a8-dac6a7648636` SHA `bd77b5e2ffc261677b9d1066a938600dc966979d972f26b1da95c81a949338ab`. Both hashing calls succeeded across 53 members and 401,996 bytes. Current outbox previews are capped at 4,000 output characters, before the target DLL entries; exact member hashes remain unread, so H1 remains qualified/inconclusive. Do not rerun these analyses.
- At 19:35 UTC, `pgrep -af relay.py` returned no process. No c7 request was queued and no new operator POST was made. After relay recovery: fetch fresh status and the full archive observations, compare the DLL member hashes, then use a supported advisor reparse or one well-formed review request after a fresh status. Keep all findings qualified and opinion v2 draft until direct evidence and a usable review support changes.


## Addendum CTI-ANALYST-8 — final checkpoint — 2026-10-28 21:43 UTC

- Canonical GET `c8-154` completed HTTP 200: working-state v158 / SHA `f6655bdb36a98722ff621cc76a53034c436c3b4d96ccb17ae35348d93e4389f6`; investigation `running`; no active jobs, ambiguous invocations, or reconciliation-required invocations. Prior concern about the queued status GET is resolved.
- Current opinion `128f991d-09c9-5cb5-905c-d0c044bfff00` v6 SHA `612122ac651e545229b34ef4d6cff272ca0f1065e1e8e51807e2f0d17a92eb8c`, status `draft`. Valid final Bridge review: invocation `b8fc6f80-7da5-4d73-a1f0-6f8d0335894a`, Bridge turn `f4190795-afb0-4f80-a634-688e5fe7ecde`, response observation `e0e85a12-dd49-51b4-b018-5785afd643c5` SHA `f6fa86fa3bdc4890f4ce86059c773db4c82e6061008ae668c51ad7182d028075`, output SHA `082d45c8bd1e1d80f9dd39760433389e517de9d11ba5e736d551263cc86858a`; all U1–U5 usable, no warnings/unprocessed units. No post-review edit needed. Draft remains unapproved/unpublished.
- Exact SHA comparisons prove `Resources/PsProxy.dll` and `Resources/RuntimeBroker.dll` in ZIP `6e7d9b33f1e72ea1ede71373a604ecdb060dab7d42055179c1eede9ecd1fd239` match the standalone sample hashes. Archive observation `ac2fc5ca-65e9-5a9e-81a1-1a9c7b9fa64d` SHA `694f25df103b424a036f8c75331f7d6564e8549a5ae93a14db0ea1401018aef8`; `FlightManager.cs` entry observation `066ca77a-7c75-5ab9-962a-8f73f8a2539a` SHA `ecf212ffc1708d1ef2a85d0b7e862e419f530f9ca4e6e0dce4cad13fa7c91785`. Membership/component identity is not execution or actor attribution; project hash remains unmatched.
- Static CIL supports PowerShell runspace, generic WebClient transfer and AES-CBC/file-decryption capabilities (observations `4b293b47-7255-5ea6-a2f3-701b00ea6bfc`, `efe8966c-957c-5922-a009-c23aa00f9082`, `7d89d116-9a79-5b64-bb8f-c309baf8b955`, `d0d12c2e-b1fc-5841-bf5c-a1fd860ad0f3`); no call chain proves GitHub Issues/401 fallback. Source S2P2.1 says RuntimeBroker config has `etwEnable enabled="false"`; config bytes/runtime effect remain unverified. Iran alignment and Blackwood naming are source-reported only. No runtime behavior observed; absence of telemetry does not establish absence.
- Findings all remain `qualified`: H1 `4ef7b836-731f-5f51-96d4-f364640b8e3b`; H2 `fef1e600-fa76-5274-9a9e-acd6cc892702`; H3 `266bf1f9-14ec-58ef-94bf-0f8ceabfb484`; H4 `746907e4-ccde-53a5-b073-0e189976758c`; static capability `c9ab2d7c-c1fa-5bc5-ab28-25913c88bfb4`.
- Canonical budget: Bridge 10 consumed / 18 reserved / 172 remaining; VT 22 / 13 / 9965; downloads 0 / 0 / 1000; Shodan 1 / 1 / 998; tool_seconds 3215 / 65040 / 277345. No test, malware execution, C2 contact, sample download, publication, or release by CTI-ANALYST-8. See the analyst-log addendum for review decisions and uncertainties.

## Addendum CTI-ANALYST-9 — 2026-10-28 final-opinion continuation (pending Bridge)

- Current canonical status last read before Bridge: v165 / SHA `ca75323f553412767443cf773cc60e290f9bb2e1acb2a03a51bf4e960f513c6a`; investigation running, no active jobs, G2 approved/not revoked, TLP CLEAR, external LLM allowed.
- Opinion `128f991d-09c9-5cb5-905c-d0c044bfff00` v7 draft SHA `0743996682c0b62bccfd795bfe2dd6fc6609d06b75f584a78acb5e1e4c7d3057`. It has eight blocks, including artifact inventory, static capabilities, additional sample caveat, network IOC status, separate ETW/GitHub/attribution qualifications, candidate local hunt, and evidence-level notes.
- Current supported findings: archive membership `41865f57-dffb-5f72-8bbf-7d130d2e91d4`; passive VT PsProxy name/hash association `54697eed-bbd7-58ed-bc3f-efb8967fdd84`; passive VT RuntimeBroker name/hash association `174dcc0d-2dc7-5fdb-afa0-35d40c7190a9`. Broader H1/H2/H3/H4 and static capability remain qualified.
- One valid-format Bridge review request `c9-040-bridge-review-v7` is queued; first 30-second poll found no outbox file. Treat as pending. Do not duplicate; poll up to 25 minutes, then classify based on the result. No approval or publication endpoint was called.

## Addendum CTI-ANALYST-9 — 2026-10-28 review validation correction

- Supersedes the previous pending-review statement above. `c9-040-bridge-review-v7` completed HTTP 200 at working-state v166, but advisor invocation `d36f80eb-2c07-4ccd-8ac2-52eb2f14be26` was rejected before transport. Response observation `12b3f135-492a-58d3-8faa-f43b2ab57e61` confirms `bridge_invocation_id=null`, zero Bridge turns consumed, and `confirmed_non_execution=true`; observation `5039469a-9a7a-5242-8b3e-b17768dbdb6a` has no usable review. This is not a valid Bridge review.
- Current canonical checkpoint remains working-state v166 / SHA `59c39dd181c07b0509737bcaf0e9a3d2f4078522279ae6855191a2bc87dd1073`, no active jobs, opinion v7 draft SHA `0743996682c0b62bccfd795bfe2dd6fc6609d06b75f584a78acb5e1e4c7d3057`, G2 approved, not revoked, TLP CLEAR, external LLM allowed.
- Cause: Bridge U2 requested an `implant_materials` body section absent from the persisted v7 opinion. The operator tool's `opinion.revise` interface accepts blocks but not typed material items; the backend drops a plain-text `implant_materials` block when no typed items are supplied. This is recorded as a tooling **BLOCKER** in `analyst_log.md`.
- Next permissible action: revise the complete seven-block draft to place the exact cited archive inventory table in `additional_samples`, then issue one changed `review_text` request for the seven existing body blocks after a fresh status GET. Apply justified Bridge remarks in one final `opinion.revise`; retain DRAFT status. No approval/publication.
- That changed full-draft revision has completed: `c9-046-opinion-revise-v8` succeeded at working-state v167, invocation `a48f2600-1316-42ea-ba90-170f6f3b7bec`. It preserves the complete cited seven-row artifact table in `additional_samples` alongside the FlightManager.csproj caveat. The opinion remains draft; obtain fresh status and submit one corrected review request covering the seven canonical body sections. The typed `implant_materials` projection remains unavailable through the operator tool schema.
- Reconciliation GET `c9-047-status-before-review-v8`: working-state v167 / SHA `743630c0a0ea22efd62960ed2469468eca23fbf8c436f6c5a433f9337ad55b05`; opinion v8 SHA `a8dbed14c5dd6c8f6391e1b6ad47c0a86d8015c8a4d34d4ecc10f4f9e94aae83`, DRAFT; no active/reconciliation jobs. G2/TLP/external-LLM gate is green. Next: fresh status then the one changed Bridge review covering the seven persisted sections, with the inventory assertion incorporated into `additional_samples`.
- Corrected review `c9-049-bridge-review-v8-corrected` returned after about 19 minutes as `needs_review` at working-state v168. It has no usable reviews; failure is `advisor_timeout_or_ambiguous` / `model_submission_reconciliation_required`, and `confirmed_non_execution=false`. Treat the external submission as ambiguous and do not replay it. Linked advisor response observation is `194057c6-cb1a-5bf3-8052-4307a515e9fc`.
- Status reconciliation GET `c9-050-status-after-bridge-review-v8` is still in inbox after two 60-second waits. No follow-up POST or duplicate Bridge request is authorized until it completes and the linked observation is read. If the existing response can be recovered, only local `advisor.reparse` may be considered. Opinion v8 remains DRAFT, SHA `a8dbed14c5dd6c8f6391e1b6ad47c0a86d8015c8a4d34d4ecc10f4f9e94aae83`.
- Superseding reconciliation: `c9-050` completed at v168 / SHA `af32b71b12d30379cc3bd94ed91af95ee24745774c335140837e29a0bae7f3e9`, no active/reconciliation-required jobs. GET `c9-051` confirms response observation `194057c6-cb1a-5bf3-8052-4307a515e9fc` stores an `advisor-response.v3` marked `needs_review`, with one Bridge turn in its budget-consumption record, no invocation ID, and no normalized review yet. Next is one local `advisor.reparse` after fresh status; never resubmit the Bridge consultation.
- The first local `advisor.reparse` request `c9-053` used generic schema `1` and was unavailable at v169. Source confirms the tool's registered schema is `advisor-reparse.v1`; after fresh status, retry locally once with the exact schema. This does not contact Bridge.
- Exact-schema local reparse `c9-055` completed at v170 but returned `failed` / `tool_failed`, with no recovered result. The stored advisor response was fetched page-by-page and SHA-checked; it records `invocation_stage=timeout_ambiguous`, `submission_state=external_state_unknown`, `response_bytes=0`, `output_sha256=null`, provider usage unavailable, and one Bridge turn. No review text is present to normalize. Do not call Bridge or reparse again; an ambiguous external submission must not be duplicated.
- Opinion v8 remains the last safe draft, SHA `a8dbed14c5dd6c8f6391e1b6ad47c0a86d8015c8a4d34d4ecc10f4f9e94aae83`; no valid review means no review remarks to classify and no justified post-review revision. Final status GET remains outstanding. The typed `implant_materials` projection is still blocked by the operator interface; the complete cited table is stored in `additional_samples`.
- Final status GET `c9-057-final-status-draft-opinion`: v170 / SHA `c4632851d2c279bb4fa3157af4dbd7ab253109eb7125d652085d040fe970e539`, no active jobs or listed reconciliation-required invocation. Last operation is failed reparse with `provider response or reconciliation evidence` missing. Opinion v8 remains DRAFT with the same SHA. Bridge budget ledger is 10 consumed / 19 reserved / 171 remaining, while the stored response records one Bridge turn and zero response bytes. Stop here: no safe basis exists to classify Bridge remarks or make a post-review revision.

## CTI-ANALYST-10 checkpoint — 2026-10-28 23:17 UTC

- Last canonical status: v170 / SHA `c4632851d2c279bb4fa3157af4dbd7ab253109eb7125d652085d040fe970e539`; opinion v8 SHA `a8dbed14c5dd6c8f6391e1b6ad47c0a86d8015c8a4d34d4ecc10f4f9e94aae83`, DRAFT. G2 green (approved, not revoked, TLP CLEAR, external LLM allowed); no active or reconciliation-required jobs.
- No directly observed literal crypto constant/token was identified in the reviewed PsProxy/RuntimeBroker static observations; no typed `implant_materials` rows added. Relevant methods show generic AES-CBC with supplied key/IV or a derived key, while string coverage is opaque/truncated.
- v8 content checks: `key_findings` already leads with the principal archive result and immediate limits; opinion prose has no repeated source URLs; refs remain in evidence fields. Pipe inventory markup is supported by the renderer.
- Fresh valid Bridge review still pending. Seven canonical body units and their block-carried observation refs are ready; prior c9-049 remains external-state-unknown and will not be reconciled/replayed. Read fresh operator status before the new POST. Keep the opinion DRAFT.

## CTI-ANALYST-10 checkpoint — 2026-10-28 23:29 UTC

- Fresh Bridge review of v8 completed: invocation `8a15ea8a-7f7d-468f-84aa-d658ad3b122b`, Bridge turn `ff3c9a74-7b2e-4226-a6e1-b0548da76332`, response observation `e3ed369b-26f5-5ad9-958c-9066b5a88be9` SHA `c773da4778e90953cc4df8b26e65eb79de84995be6cb7dd59d1f4abca24ddc67`, output SHA `35e526a12985bcb5a06fd6c73f039eefb82cee3dadf3b0568aea180f42d810dc`. Overall `partial`, but `usable=true` and all seven canonical body units have usable reviews; preflight sent all 58 items with none omitted. Batch metadata also lists every unit as unprocessed; this inconsistency and heuristic alias warnings are documented in `analyst_log.md` and were not retried.
- Verdicts are recorded per unit in `analyst_log.md`. Useful AES-helper precision, endpoint qualification, source-attributed Unit 42 confidence, in-process PowerShell hunt scope and archive-vs-identity distinction will be applied once. The archive path/missing-evidence objections and HTTP 403 expansion were rejected; duplicate FlightManager.cs prose will be removed.
- No literal crypto material was directly identified; no typed `implant_materials` rows and no finding changes. Candidate opinion revision preserves evidence and unresolved points, has no URLs in prose and retains the strict archive table. Current canonical opinion remains v8 DRAFT pending one final revision. Read status immediately before POST.

## CTI-ANALYST-10 final checkpoint — 2026-10-29

- Opinion `128f991d-09c9-5cb5-905c-d0c044bfff00` is canonically v9, SHA-256 `cedb2157e6bfd06f0bfd8b61765e9e1b183e9dc76d43e5be6ae6e6b6e80047c8`, status DRAFT. Final status GET `c10-status-final-v9`: working-state v172 / SHA `d6f3437059b05f47ae52f0b41db77377ff4a5cf12deb5bd5580296a6399ffcb5`; no active, ambiguous, or reconciliation-required jobs.
- One revision succeeded: `c10-opinion-revise-v9-final`, invocation `f160d8d7-140b-4506-984c-d7325001b045`. It applies the usable v8 Bridge remarks and content QA changes; no finding statuses changed, and no `implant_materials` rows were added because no directly observed literal crypto material was identified.
- Valid fresh v8 Bridge review: invocation `8a15ea8a-7f7d-468f-84aa-d658ad3b122b`; overall `partial`, but all seven canonical body-unit reviews are individually usable. Record includes output SHA `35e526a12985bcb5a06fd6c73f039eefb82cee3dadf3b0568aea180f42d810dc`; warnings/inconsistent unprocessed-unit metadata are documented in `analyst_log.md`. No duplicate/reparse; earlier c9-049 was not reconciled or replayed.
- Verdicts U1–U7 and the precise edits/rejections are in the final analyst-log addendum. G2/TLP/external-LLM gates passed before revision. No approval or publication was performed.
- This final checkpoint supersedes the preceding 2026-10-28 CTI-ANALYST-10 checkpoint's “review pending” and “candidate revision” state; v9 is now canonical and reconciled.

## ORCHESTRATOR #6 checkpoint — 2026-10-29
- Verified: git main aab70010 clean; fingerprint 9c99e611cbca43ec identical in backend/worker/analysis-worker/job-recovery == git; Alembic 0006 head; relay alive (GET /operator-status 200, wsv 172); budgets Bridge 11/19res/200, VT 22/13res/10000, downloads 0, tool_seconds 3269/68640res/345600.
- Launching: TYPST-PDF QA v9 (point 1) and OPERATOR-AUDIT (point 2) in parallel, fresh Luna xhigh sessions.

## ORCHESTRATOR #6 checkpoint 2 — 2026-10-29 ~07:30 UTC
- main local = 9bf2bd5e (NOT pushed, NOT deployed): merged audit+playbook (b818ed4c), relay v2 (bbbe4b1c), PDF QA fixes (reorder reverted; no confidence %), archive.extract_member (+fixes). Gates at 9bf2bd5e: ruff/format/mypy OK; full pytest incl PG+Typst 5814 passed.
- archive.extract_member: CODE-REVIEW 2 = REJECT (P1 blob delete race, P2 transfer orphan, P2 parser same-name member). TECH-FIX f7a8d1cf (branch m07/archive-extract-member-fixes2 in /tmp/m07-extract3) decided: drop physical deletion of transfer blob; fix parser. Then re-run PG+gates, CODE-REVIEW 3, then safe_deploy.
- Relay v2 now running (pid via relay-status.json) with INV a8845772 (to restart with new INV at new analysis).
- USER AUTHORIZED new investigation, autonomous (human token use authorised by user message 2026-10-29 for starting it; never approve/publish opinions).
- pdf_qa_002.md open items: hash wraps in prose, p.9 opening order (QA contract), FINAL-REVIEW + visual check after deploy+re-render.
- 2026-10-29 ~12:30 UTC: deployed 5198c33c (fingerprint 5198c33cc2a85c77); PDF QA pdf_qa_003 PASS_WITH_NOTES; extract_member live OK; launching restart-with-new-sources for subject d22822d4-27ce-47d2-9cc5-f84cf312986a (user-authorised new investigation)
