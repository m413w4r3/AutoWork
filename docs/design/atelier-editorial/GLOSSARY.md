# Editorial Workshop

Terms for editing a production document through stable block occurrences, discussion threads, proposals, and versioned adoption.

## Workspace identity

**EditorialBlock** [Bloc éditorial]: One addressable structural occurrence in a production run, including an occurrence whose content duplicates another block.
_Not_: a content hash, a legacy revision target, or an unqualified `OpinionBlock`.
_Owner_: `EditorialWorkspace` (`application/editorial_workspace/`) resolves and projects blocks.
_Invariant_: `block_id` is a run-scoped UUID; content SHA-256 identifies bytes; the legacy target ID remains the SHA-256 of its target JSON; `before_hash` is only the precondition fence. _Avoid_: block ID, occurrence, target ID, and content hash as synonyms.

**EditorialWorkspaceSnapshot / pin** [Instantané de l’atelier / épingle]: A snapshot is the server-resolved view of exact subject and run artifact pins, and its workspace pin hash identifies that pinned set.
_Not_: a new canonical artifact, an editable draft, or a publication status.
_Owner_: `EditorialWorkspace` (`application/editorial_workspace/`).
_Invariant_: snapshots are append-only and unique per production run and pin; publication kind is explicit. _Avoid_: artifact version, workspace version, and pin interchangeably.

**Block registry snapshot** [Instantané du registre des blocs]: The registry is identity and lineage metadata for block occurrences inside a workspace snapshot.
_Not_: the canonical text or an evidence store.
_Owner_: `EditorialWorkspace`.
_Invariant_: retain retired IDs; store no block text or evidence; link legacy versions only by recorded lineage or one exact unique match. _Avoid_: block content, document body, or heuristic match.

**Fence / CAS** [Garde de version / comparaison-échange]: A fence is an expected content hash and pinned version set, while compare-and-swap (CAS) writes only if those expectations still match.
_Not_: a block identity, legacy target ID, or general-purpose lock.
_Owner_: `editorial_proposal_adoption`.
_Invariant_: re-read and re-hash exact canonical bytes before adoption; a mismatch prevents overwrite and makes the proposal stale. _Avoid_: hash, pin, and fence as interchangeable names.

## Threads and proposals

**EditorialThread** [Fil éditorial]: A thread belongs to a subject and block occurrence, with an immutable origin artifact ID, version, and canonical hash plus a current binding to that occurrence in the current artifact.
_Not_: a `ModelConversation` or a separate thread for every artifact version.
_Owner_: the `EditorialThreads` aggregate and application flow; its `ModelConversation` is private.
_Invariant_: a missing or ambiguous current occurrence is reported as stale or ambiguous; no proposal is applied without rechecking current fences. _Avoid_: chat, conversation, and version-specific thread.

**Discussion message** [Message de discussion]: A message is a user instruction or Bridge reply in a thread whose objective is discussion.
_Not_: a proposal request, validated patch, or evidence.
_Owner_: `EditorialThreads`.
_Invariant_: a `MESSAGE` objective never creates an `EditorialProposal`. _Avoid_: proposal, finding, and evidence.

**Proposal request** [Demande de proposition]: A proposal request asks Bridge to produce a typed patch or refusal for a thread’s current target.
_Not_: the resulting proposal or an instruction already validated for adoption.
_Owner_: `EditorialThreads` prepares the request; `EditorialProposals` validates its result.
_Invariant_: a `PROPOSAL` objective emits only `EditorialPatch` or `EditorialRefusal` and makes no canonical write. _Avoid_: proposal, draft, and message.

**EditorialProposal** [Proposition éditoriale]: A persistent aggregate for one validated, typed editorial change against a stable target and base version, with evidence references and transition history.
_Not_: free-form chat, a P10 invariant proposal, an enrichment wire proposal, or an analyst opinion draft.
_Owner_: `domain/editorial_proposal.py` and `application/editorial_proposals.py`.
_Invariant_: preparing may become proposed, refused, or needs-review; proposed may become accepted, rejected, or stale; terminal states are immutable. _Avoid_: `CandidateInvariantProposal`, `EditorialEnrichmentProposalV1`, and unvalidated proposal text.

**Proposal variant** [Variante de proposition]: A variant is an alternative `EditorialProposal` for the same base and target.
_Not_: a new canonical version or an ordinal reference such as “the second one.”
_Owner_: `EditorialProposals`, with explicit references in `EditorialThreads`.
_Invariant_: variants are identified explicitly; accepting one makes other proposed variants on that base stale. _Avoid_: alternative draft, second option, and revision.

**Typed patch** [Patch typé]: A patch is the immutable validated operation payload that describes the exact proposed editorial change.
_Not_: raw model text or arbitrary HTML, SVG, Typst, JavaScript, or diagram markup.
_Owner_: `editorial_patch_validation` and `editorial_proposal_adoption`.
_Invariant_: adoption verifies the exact patch SHA-256 and accepts only supported semantic operations. _Avoid_: diff, response text, and proposal request.

**Decision** [Décision]: A decision is a human ACCEPT or REJECT command against a specific proposal state version and patch hash.
_Not_: proposal preparation or the canonical write itself.
_Owner_: `application/editorial_proposals.py`.
_Invariant_: decisions carry an idempotency key; a different decision against a terminal proposal conflicts. _Avoid_: adoption, model response, and approval status alone.

**Adoption** [Adoption]: Adoption is the single transactional promotion of an accepted patch into a new canonical version.
_Not_: accepting a card in the UI, generating a proposal, or publishing externally.
_Owner_: `editorial_proposal_adoption`, the only adoption writer.
_Invariant_: revalidate policy, evidence, revocations, and fences; write the version, lineage, registry advance, and impact outbox atomically without another model call. _Avoid_: save draft, accept, and publication.

**Stale proposal** [Proposition périmée]: A proposal is stale when its base fence no longer matches or another variant for that base was accepted.
_Not_: a rejected or deleted proposal.
_Owner_: the `EditorialProposal` state machine.
_Invariant_: stale is terminal and cannot be adopted; prepare a new proposal against the current snapshot. _Avoid_: retry, overwrite, and auto-rebase.

**Impact** [Impact différé]: Impact is the post-commit record of dependent editorial work made stale or needing reassembly by an adoption.
_Not_: the adoption transaction, a rollback, or an automatic repin.
_Owner_: `editorial_revision_impact` and its outbox worker.
_Invariant_: processing is idempotent and replayable; pending impact remains visible and never reverses the canonical adoption. _Avoid_: publication, rollback, and repin.

## Placement and document state

**EditorialPlacement** [Emplacement éditorial]: An editorial placement inserts an enrichment `after_paragraph` by referring to the paragraph’s `anchor_block_id` in the pinned registry.
_Not_: an array index, temporary UI position, or end-of-section fallback.
_Owner_: `EditorialWorkspace` resolves the anchor; enrichment and publication assemblers preserve it.
_Invariant_: a missing anchor yields `editorial_anchor_stale` and stale enrichment/preview; same-anchor items sort by canonical position then element key. _Avoid_: index, nearest paragraph, and fallback placement.

**EditorialDraft** [Brouillon éditorial]: An editorial draft is uncommitted manual text, either held locally per block or stored server-side as a noncanonical draft.
_Not_: an `EditorialProposal`, an `AnalystOpinion` draft, a canonical version, or a publication.
_Owner_: the editorial workspace frontend for local state and `editorial_drafts` for server drafts.
_Invariant_: saving a manual edit validates its base and content, then creates a canonical version without a Bridge call. _Avoid_: draft as a synonym for proposal, version, or publication.

**Local draft** [Brouillon local]: A local draft is unsaved editor input retained per block in the current workspace interaction.
_Not_: a canonical browser copy or a server draft.
_Owner_: `frontend/src/features/editorial-workspace/`.
_Invariant_: do not persist sensitive CTI text in browser storage without an explicit policy. _Avoid_: autosaved version and server draft.

**Server draft** [Brouillon serveur]: A server draft is access-controlled, retention-limited manual text stored without canonical status.
_Not_: an adopted proposal or canonical artifact version.
_Owner_: the editorial draft application and `editorial_drafts` store.
_Invariant_: subject ACLs apply; saving as canonical still requires base-version and content validation. _Avoid_: persisted version and publication.

**Canonical version** [Version canonique]: A canonical version is an append-only, authoritative artifact version produced by validated manual saving or proposal adoption.
_Not_: a local or server draft, preview, or assembled publication.
_Owner_: the production artifact store and the authorized editorial write path.
_Invariant_: preserve exact bytes, SHA-256, version history, and lineage. _Avoid_: current text, draft, and publication version as synonyms.

**Publication** [Publication]: A publication is the canonical assembled document artifact rendered from pinned production inputs.
_Not_: a workspace snapshot, publication kind, preview, or external release action.
_Owner_: `PublicationAssembler` and the publication rendering path.
_Invariant_: retain its input pins and become stale when a required input changes. _Avoid_: canonical source version, preview, and release.

**Publication kind** [Type de publication]: Publication kind is the explicit server-resolved ARTICLE or BRIEF classification carried by a workspace snapshot.
_Not_: publication lifecycle status, artifact version, or publication artifact.
_Owner_: server publication routing and legacy resolution, projected by `EditorialWorkspace`.
_Invariant_: never silently default to ARTICLE or drop the kind between production stages. _Avoid_: publication type, status, and format without qualification.

## Bridge states

**Bridge states** [États du Bridge]: These states project a thread’s asynchronous Bridge work: `ready` [Prêt], `waiting_for_capacity` [En attente de capacité], `generating` [Génération en cours], `reply_available` [Réponse disponible], `refused` [Appel refusé], `session_lost` [Session indisponible], and `reconciliation_required` [Réconciliation nécessaire].
_Not_: proposal lifecycle states or generic page-loading states.
_Owner_: `EditorialThreads` and the Bridge worker/gateway flow.
_Invariant_: reconcile the exact possibly-submitted ModelRun rather than resubmitting; a lost session requires a new thread. _Avoid_: retry, proposal state, and conversation state.

## Terms to avoid

| Avoid | Use |
|---|---|
| “Proposal” for unvalidated chat text | “Discussion message” or “proposal request”; reserve `EditorialProposal` for a persisted validated aggregate. |
| P10 proposal for an editorial change | `CandidateInvariantProposal` / P10 invariant proposal. |
| Analyst opinion draft for an editorial draft | `AnalystOpinion` draft; it is a separate opinion lifecycle. |
| Enrichment wire proposal for an editorial proposal | `EditorialEnrichmentProposalV1`; it is the enrichment generator’s structured output. |
| `block_id`, hash, and legacy target interchangeably | Name `block_id`, content SHA-256, legacy target ID, or `before_hash` precisely. |
| Publication, publication kind, preview, and release interchangeably | Name the publication artifact, ARTICLE/BRIEF kind, preview, or external release action. |
