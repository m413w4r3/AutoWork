/**
 * Vue d’ensemble — enveloppe G2 initiale du bootstrap.
 *
 * L’affichage est borné aux plafonds proposés. L’approbation est une décision
 * humaine explicite : motif obligatoire, confirmation cochée, empreinte de la
 * proposition épinglée dans la requête et `Idempotency-Key` générée par le
 * client. L’empreinte n’est jamais un bouton et aucun clic par hash n’approuve.
 */

import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { FormEvent } from "react";

import { investigationGovernanceQueryKeys } from "../../../api/investigationGovernance";
import {
  useApproveBootstrapEnvelope,
  useBootstrapEnvelopeProposal,
  type BootstrapEnvelopeApprovalV1,
  type BootstrapEnvelopeProposalV1,
} from "../api/bootstrap";
import { createIdempotencyKey, toWorkshopApiError } from "../api/client";
import {
  WorkshopLoadingState,
  WorkshopStateBanner,
  WorkshopTypedErrorState,
} from "../shell/WorkshopStates";
import {
  BUDGET_DIMENSION_LABELS,
  PLAN_STATE_LABELS,
  ROUTE_STATUS_LABELS,
  bounded,
  formatBytes,
  formatCount,
} from "./overviewPresentation";

const MAX_LIST = 10;

export function EnvelopeSection({
  investigationId,
  workspaceReady,
  workspaceBlockedReason,
}: {
  investigationId: string;
  workspaceReady: boolean;
  workspaceBlockedReason: string | null;
}) {
  const queryClient = useQueryClient();
  const proposalQuery = useBootstrapEnvelopeProposal(investigationId);
  const approval = useApproveBootstrapEnvelope(investigationId);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [submittedApproval, setSubmittedApproval] =
    useState<BootstrapEnvelopeApprovalV1 | null>(null);

  const proposal = proposalQuery.data ?? null;
  const approvedNow =
    submittedApproval !== null || proposal?.already_approved === true;
  const proposalError = proposalQuery.isError
    ? toWorkshopApiError(proposalQuery.error)
    : null;
  const approvalError = approval.isError
    ? toWorkshopApiError(approval.error)
    : null;
  const stale =
    proposalError?.code === "bootstrap_proposal_stale" ||
    approvalError?.code === "bootstrap_proposal_stale";

  const blockedReason = approvalBlockedReason(
    proposal,
    workspaceReady,
    workspaceBlockedReason,
  );
  const canSubmit =
    proposal !== null &&
    blockedReason === null &&
    reason.trim() !== "" &&
    confirmed &&
    !approval.isPending;

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!proposal || !canSubmit) return;
    approval.mutate(
      {
        reason: reason.trim(),
        correlation_id: createIdempotencyKey(),
        proposal_sha256: proposal.proposal_sha256,
      },
      {
        onSuccess: (result) => {
          setSubmittedApproval(result);
          setReason("");
          setConfirmed(false);
          // L’approbation fait passer l’enveloppe de « absente » à
          // « approuvée » : le cycle de vie monte sans rechargement de page.
          void queryClient.invalidateQueries({
            queryKey:
              investigationGovernanceQueryKeys.envelope(investigationId),
          });
        },
      },
    );
  };

  return (
    <section
      className="job-card workshop-envelope"
      aria-labelledby="overview-envelope-heading"
    >
      <h2 id="overview-envelope-heading">Enveloppe G2 initiale</h2>

      {submittedApproval ? (
        <ApprovalResult approval={submittedApproval} />
      ) : proposal?.already_approved ? (
        <AlreadyApproved proposal={proposal} />
      ) : null}

      {approvalError && !stale ? (
        <WorkshopTypedErrorState
          error={approvalError}
          title="Enveloppe G2 refusée"
        />
      ) : null}

      {stale ? (
        <WorkshopStateBanner tone="error" title="Proposition expirée" live>
          <p>
            L’inventaire ou les plafonds ont changé depuis l’affichage. Aucune
            approbation n’a été envoyée avec l’ancienne empreinte.
          </p>
          {proposalError ? (
            <p>
              Code <code>{proposalError.code}</code>.
            </p>
          ) : approvalError ? (
            <p>
              Code <code>{approvalError.code}</code>.
            </p>
          ) : null}
          <button
            type="button"
            className="button button--secondary"
            onClick={() => {
              approval.reset();
              void proposalQuery.refetch();
            }}
          >
            Recharger la proposition
          </button>
        </WorkshopStateBanner>
      ) : null}

      {proposalQuery.isPending ? (
        <WorkshopLoadingState label="Chargement de la proposition d’enveloppe…" />
      ) : proposalError && !stale ? (
        <WorkshopTypedErrorState
          error={proposalError}
          title="Proposition d’enveloppe indisponible"
          onRetry={() => void proposalQuery.refetch()}
        />
      ) : proposal ? (
        <>
          <EnvelopeProposal proposal={proposal} />
          {approvedNow ? null : (
            <form className="workshop-approval-form" onSubmit={submit}>
              <label htmlFor="g2-approval-reason">
                Motif de la décision
                <textarea
                  id="g2-approval-reason"
                  value={reason}
                  maxLength={4000}
                  required
                  onChange={(event) => setReason(event.currentTarget.value)}
                />
              </label>
              <label className="workshop-approval-form__confirm">
                <input
                  type="checkbox"
                  checked={confirmed}
                  onChange={(event) =>
                    setConfirmed(event.currentTarget.checked)
                  }
                />
                <span>
                  Je confirme approuver l’enveloppe G2 avec les plafonds
                  affichés. Cette décision humaine est versionnée.
                </span>
              </label>
              {blockedReason ? (
                <p role="note">{blockedReason}</p>
              ) : (
                <p role="note">
                  L’empreinte de la proposition est épinglée dans la requête ;
                  elle n’est jamais un bouton d’approbation. Une clé
                  d’idempotence est générée par le client.
                </p>
              )}
              <button
                type="submit"
                className="button button--secondary"
                disabled={!canSubmit}
              >
                {approval.isPending
                  ? "Approbation en cours…"
                  : "Approuver l’enveloppe"}
              </button>
            </form>
          )}
        </>
      ) : null}
    </section>
  );
}

function approvalBlockedReason(
  proposal: BootstrapEnvelopeProposalV1 | null,
  workspaceReady: boolean,
  workspaceBlockedReason: string | null,
): string | null {
  if (!proposal) return null;
  if (proposal.already_approved) return null;
  if (!workspaceReady)
    return (
      workspaceBlockedReason ??
      "Le workspace local doit être disponible avant de décider l’enveloppe G2."
    );
  if (
    proposal.plan_state === "blocked" ||
    proposal.plan_state === "unavailable"
  )
    return "Le plan de bootstrap est bloqué ou indisponible : l’enveloppe ne peut pas être approuvée dans cet état.";
  if (proposal.covered_operations === 0)
    return "Aucune opération n’est couverte par cette proposition : vérifiez l’inventaire et les capacités locales.";
  return null;
}

function AlreadyApproved({
  proposal,
}: {
  proposal: BootstrapEnvelopeProposalV1;
}) {
  return (
    <WorkshopStateBanner
      tone="success"
      title={`Enveloppe G2 approuvée — version ${proposal.envelope_version ?? "inconnue"}`}
    >
      <p>
        La proposition courante indique que cette enveloppe est déjà approuvée.
        Aucune nouvelle décision n’est demandée.
      </p>
      <p>
        Fournisseurs autorisés :{" "}
        {proposal.allowed_providers.length === 0
          ? "aucun"
          : proposal.allowed_providers.join(", ")}{" "}
        · acquisition d’échantillons :{" "}
        {proposal.acquire_hits_allowed ? "autorisée" : "non autorisée"}.
      </p>
      <LifecycleReferral />
    </WorkshopStateBanner>
  );
}

/** Renvoi unique : l’extension et la révocation vivent hors de cette section. */
function LifecycleReferral() {
  return (
    <p>
      Les extensions et la révocation se décident dans{" "}
      <a href="#overview-envelope-lifecycle-heading">
        le cycle de vie de l’enveloppe G2
      </a>
      .
    </p>
  );
}

function ApprovalResult({
  approval,
}: {
  approval: BootstrapEnvelopeApprovalV1;
}) {
  return (
    <WorkshopStateBanner
      tone="success"
      title={`Enveloppe G2 approuvée — version ${approval.envelope_version}`}
    >
      <p>
        Décision <code>{approval.approval_decision_id}</code> · acteur{" "}
        {approval.approval_actor_id} ·{" "}
        <time dateTime={approval.approved_at}>{approval.approved_at}</time>
      </p>
      <p>
        Fournisseurs autorisés :{" "}
        {approval.allowed_providers.length === 0
          ? "aucun"
          : approval.allowed_providers.join(", ")}{" "}
        · acquisition :{" "}
        {approval.acquire_hits_allowed ? "autorisée" : "non autorisée"} · TLP{" "}
        {approval.disclosure_tlp ?? "non déclaré"} · modèle externe{" "}
        {approval.disclosure_external_llm_allowed ? "autorisé" : "non autorisé"}
        .
      </p>
      <dl className="workshop-meta">
        {Object.entries(approval.budget_limits).map(([dimension, limit]) => (
          <div key={dimension}>
            <dt>{budgetLabel(dimension)}</dt>
            <dd>{formatCount(limit)}</dd>
          </div>
        ))}
      </dl>
      <LifecycleReferral />
    </WorkshopStateBanner>
  );
}

function budgetLabel(dimension: string): string {
  return dimension in BUDGET_DIMENSION_LABELS
    ? BUDGET_DIMENSION_LABELS[dimension as keyof typeof BUDGET_DIMENSION_LABELS]
    : dimension;
}

function EnvelopeProposal({
  proposal,
}: {
  proposal: BootstrapEnvelopeProposalV1;
}) {
  const summary = bounded(proposal.summary, MAX_LIST);
  return (
    <section
      className="workshop-ceilings"
      aria-labelledby="overview-envelope-ceilings-heading"
    >
      <h3 id="overview-envelope-ceilings-heading">Plafonds proposés</h3>
      <dl className="workshop-meta">
        <dt>Schéma</dt>
        <dd>{proposal.schema_version}</dd>
        <dt>Généré le</dt>
        <dd>
          <time dateTime={proposal.generated_at}>{proposal.generated_at}</time>
        </dd>
        <dt>État du plan</dt>
        <dd>{PLAN_STATE_LABELS[proposal.plan_state]}</dd>
        <dt>Empreinte de la proposition</dt>
        <dd>
          <code>{proposal.proposal_sha256}</code> (non cliquable)
        </dd>
        <dt>Inventaire</dt>
        <dd>
          {formatCount(proposal.inventory.seeds)} graine(s) ·{" "}
          {formatCount(proposal.inventory.discovered)} découvert(s) ·{" "}
          {formatCount(proposal.inventory.samples)} échantillon(s) ·{" "}
          {formatCount(proposal.inventory.ipv4)} IPv4 ·{" "}
          {formatCount(proposal.inventory.domains)} domaine(s) ·{" "}
          {formatCount(proposal.inventory.urls)} URL
          {proposal.inventory.truncated ? " · tronqué" : ""}
        </dd>
        <dt>Opérations</dt>
        <dd>
          {formatCount(proposal.covered_operations)} couverte(s) sur{" "}
          {formatCount(proposal.planned_operations)} planifiée(s) ·{" "}
          {formatCount(proposal.covered_ioc_ids.length)} IOC couverts affichés
        </dd>
        <dt>Fournisseurs autorisés</dt>
        <dd>
          {proposal.allowed_providers.length === 0
            ? "aucun"
            : proposal.allowed_providers.join(", ")}
        </dd>
        <dt>Acquisition</dt>
        <dd>{proposal.acquire_hits_allowed ? "autorisée" : "non autorisée"}</dd>
      </dl>

      <section aria-labelledby="overview-envelope-vt-heading">
        <h3 id="overview-envelope-vt-heading">Lectures VirusTotal</h3>
        <dl className="workshop-meta">
          <dt>Autorisées</dt>
          <dd>{proposal.vt_reads.allowed ? "oui" : "non"}</dd>
          <dt>Opérations</dt>
          <dd>
            {formatCount(proposal.vt_reads.operations_covered)} couverte(s) sur{" "}
            {formatCount(proposal.vt_reads.operations_planned)}
          </dd>
          <dt>Unités</dt>
          <dd>
            {formatCount(proposal.vt_reads.units_proposed)} proposée(s) sur{" "}
            {formatCount(proposal.vt_reads.units_planned)} planifiée(s)
          </dd>
          <dt>IOC</dt>
          <dd>
            {formatCount(proposal.vt_reads.iocs_covered)} couvert(s) sur{" "}
            {formatCount(proposal.vt_reads.iocs_planned)}
          </dd>
        </dl>
        <ul>
          {proposal.vt_reads.per_tool.map((tool) => (
            <li key={tool.tool_name}>
              <code>{tool.tool_name}</code> : {formatCount(tool.covered)}{" "}
              couverte(s) sur {formatCount(tool.planned)}
            </li>
          ))}
        </ul>
        <p>{proposal.vt_reads.summary}</p>
      </section>

      <section aria-labelledby="overview-envelope-samples-heading">
        <h3 id="overview-envelope-samples-heading">
          Acquisition d’échantillons
        </h3>
        <dl className="workshop-meta">
          <dt>Autorisée</dt>
          <dd>{proposal.sample_acquisition.allowed ? "oui" : "non"}</dd>
          <dt>Acquisition directe</dt>
          <dd>
            {proposal.sample_acquisition.acquire_hits_allowed
              ? "autorisée"
              : "non autorisée"}
          </dd>
          <dt>Échantillons</dt>
          <dd>
            {formatCount(proposal.sample_acquisition.sample_count_covered)}{" "}
            couvert(s) sur{" "}
            {formatCount(proposal.sample_acquisition.sample_count_planned)}
          </dd>
          <dt>Volume</dt>
          <dd>
            {formatCount(proposal.sample_acquisition.downloads_limit)}{" "}
            téléchargement(s) ·{" "}
            {formatBytes(proposal.sample_acquisition.per_sample_max_bytes)} par
            échantillon ·{" "}
            {formatBytes(proposal.sample_acquisition.total_max_bytes)} au total
          </dd>
          <dt>Route</dt>
          <dd>
            {ROUTE_STATUS_LABELS[proposal.sample_acquisition.route_status]}
          </dd>
        </dl>
        <p>{proposal.sample_acquisition.summary}</p>
      </section>

      <section aria-labelledby="overview-envelope-costs-heading">
        <h3 id="overview-envelope-costs-heading">Coûts et quotas</h3>
        <ul>
          {proposal.costs.limits.map((limit) => (
            <li key={limit.dimension}>
              {BUDGET_DIMENSION_LABELS[limit.dimension]} :{" "}
              {formatCount(limit.limit)} · source {limit.source}
            </li>
          ))}
        </ul>
        <dl className="workshop-meta">
          <dt>Unités VT</dt>
          <dd>{formatCount(proposal.costs.caps.vt_units)}</dd>
          <dt>Téléchargements</dt>
          <dd>{formatCount(proposal.costs.caps.downloads)}</dd>
          <dt>Taille par échantillon</dt>
          <dd>{formatBytes(proposal.costs.caps.per_sample_bytes)}</dd>
          <dt>Secondes d’outil</dt>
          <dd>{formatCount(proposal.costs.caps.tool_seconds)}</dd>
          <dt>Contexte</dt>
          <dd>
            {formatBytes(proposal.costs.caps.context_bytes)} ·{" "}
            {formatCount(proposal.costs.caps.context_tokens)} tokens
          </dd>
        </dl>
        <p>{proposal.costs.summary}</p>
      </section>

      <section aria-labelledby="overview-envelope-model-heading">
        <h3 id="overview-envelope-model-heading">Usage de modèle externe</h3>
        <dl className="workshop-meta">
          <dt>Autorisé</dt>
          <dd>{proposal.external_model.allowed ? "oui" : "non"}</dd>
          <dt>Fournisseurs</dt>
          <dd>
            {proposal.external_model.providers.length === 0
              ? "aucun"
              : proposal.external_model.providers.join(", ")}
          </dd>
          <dt>Source de politique</dt>
          <dd>{proposal.external_model.policy_source}</dd>
        </dl>
        <p>{proposal.external_model.summary}</p>
      </section>

      <section aria-labelledby="overview-envelope-disclosure-heading">
        <h3 id="overview-envelope-disclosure-heading">
          Limites de divulgation
        </h3>
        <dl className="workshop-meta">
          <dt>TLP</dt>
          <dd>{proposal.disclosure.tlp ?? "non déclaré"}</dd>
          <dt>Ne pas soumettre</dt>
          <dd>{proposal.disclosure.do_not_submit ? "oui" : "non"}</dd>
          <dt>Modèle externe</dt>
          <dd>
            {proposal.disclosure.external_llm_allowed
              ? "autorisé"
              : "non autorisé"}
          </dd>
          <dt>Portée</dt>
          <dd>{proposal.disclosure.scope_statement}</dd>
        </dl>
        <p>{proposal.disclosure.summary}</p>
      </section>

      {summary.visible.length > 0 ? (
        <section aria-label="Résumé de la proposition">
          <h3>Résumé</h3>
          <ul>
            {summary.visible.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          {summary.hidden > 0 ? (
            <p role="note">
              {formatCount(summary.hidden)} autre(s) ligne(s) non affichée(s).
            </p>
          ) : null}
        </section>
      ) : null}

      {proposal.limitations.length > 0 ? (
        <section aria-label="Limites de la proposition">
          <h3>Limites de la proposition</h3>
          <ul>
            {bounded(proposal.limitations, MAX_LIST).visible.map(
              (limitation) => (
                <li key={limitation}>{limitation}</li>
              ),
            )}
          </ul>
        </section>
      ) : null}
    </section>
  );
}
