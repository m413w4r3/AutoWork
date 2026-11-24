import type { InvestigationEnvelopeView } from "../../../api/investigationGovernance";

export type EnvelopeAccessKind =
  "never_approved" | "active" | "revoked" | "inactive";

export interface EnvelopeAccessState {
  kind: EnvelopeAccessKind;
  isActive: boolean;
  isRevoked: boolean;
  statusLabel: string;
  statusMessage: string;
  journalLabel: string;
  journalAcquisitionLabel: string;
  providersLabel: string;
  providerPermissionLabel: (
    provider: string,
    displayName: string,
    isRecorded: boolean,
  ) => string;
  acquisitionLabel: string;
  inactiveDisclosureLabel: string;
  technicalProvidersLabel: string;
  policyStatusLabel: string | null;
}

type EnvelopeAccessView = Pick<
  InvestigationEnvelopeView,
  | "approved"
  | "currently_authorized"
  | "allowed_providers"
  | "acquire_hits_allowed"
  | "max_acquire_volume"
  | "disclosure"
  | "authority_kind"
  | "policy_status"
>;

const POLICY_STATUS_LABELS: Record<string, string> = {
  active: "Active",
  revoked: "Révoquée",
  superseded: "Remplacée par une extension",
};

function policyStatusLabel(status: string | null | undefined): string | null {
  if (!status) return null;
  return POLICY_STATUS_LABELS[status] ?? status;
}

export function envelopeAccessState(
  view: EnvelopeAccessView,
  options: { revocationCommitted?: boolean } = {},
): EnvelopeAccessState {
  const currentlyActive = view.approved && view.currently_authorized === true;
  const explicitlyRevoked =
    options.revocationCommitted === true ||
    view.authority_kind === "human_g2" ||
    (view.authority_kind === "osint_policy" &&
      view.policy_status === "revoked");
  const kind: EnvelopeAccessKind = !view.approved
    ? "never_approved"
    : currentlyActive
      ? "active"
      : explicitlyRevoked
        ? "revoked"
        : "inactive";
  const isActive = kind === "active";
  const isRevoked = kind === "revoked";
  const policyLabel = policyStatusLabel(view.policy_status);
  const displayTlp = view.disclosure.tlp?.toUpperCase() ?? "non déclaré";
  const externalLlmValue = view.disclosure.external_llm_allowed ? "oui" : "non";

  let statusLabel: string;
  let statusMessage: string;
  if (kind === "active") {
    statusLabel = "Accès externes : actifs";
    statusMessage =
      "Les fournisseurs autorisés et les plafonds actifs sont listés ci-dessous.";
  } else if (kind === "revoked") {
    statusLabel = "Accès externes : révoqués";
    statusMessage =
      "Aucune nouvelle opération VT, Shodan ou Bridge externe n’est autorisée. Les analyses locales restent accessibles.";
  } else if (kind === "never_approved") {
    statusLabel = "Accès externes : non accordés";
    statusMessage =
      "Aucune autorisation externe n’est accordée. Les analyses locales restent accessibles.";
  } else {
    statusLabel = "Accès externes : non autorisés";
    statusMessage = policyLabel
      ? `État de la politique : ${policyLabel}. L’autorisation courante de cette enveloppe est absente. Les analyses locales restent accessibles.`
      : "L’autorisation courante est absente. Les analyses locales restent accessibles.";
  }

  const inactiveDisclosureLabel =
    kind === "never_approved"
      ? `Bornes de divulgation source affichées sans approbation G2 : TLP ${displayTlp} · do_not_submit : ${view.disclosure.do_not_submit ? "oui" : "non"} · LLM externe : contrainte source « ${externalLlmValue} », sans approbation G2`
      : `Limites de divulgation enregistrées (non actives) : TLP ${displayTlp} · do_not_submit : ${view.disclosure.do_not_submit ? "oui" : "non"} · LLM externe : valeur enregistrée « ${externalLlmValue} » (non active)`;
  const journalLabel =
    kind === "active"
      ? "Enveloppe approuvée"
      : kind === "never_approved"
        ? "Décision requise : enveloppe G2 non approuvée"
        : statusLabel;
  const acquisitionLabel = isActive
    ? view.acquire_hits_allowed
      ? "autorisée"
      : "non autorisée"
    : kind === "never_approved"
      ? view.acquire_hits_allowed
        ? "enregistrée sans approbation G2"
        : "non accordée (aucune approbation G2)"
      : view.acquire_hits_allowed
        ? "enregistrée, non active"
        : "non accordée (enveloppe inactive)";
  const journalAcquisitionLabel = isActive
    ? view.acquire_hits_allowed
      ? "acquisition de hits autorisée"
      : "acquisition de hits non autorisée"
    : kind === "never_approved"
      ? view.acquire_hits_allowed
        ? "acquisition de hits enregistrée sans approbation G2"
        : "acquisition de hits non accordée (aucune approbation G2)"
      : view.acquire_hits_allowed
        ? "acquisition de hits enregistrée, non active"
        : "acquisition de hits non accordée (enveloppe inactive)";

  function providerPermissionLabel(
    provider: string,
    displayName: string,
    isRecorded: boolean,
  ): string {
    const plural = provider === "vt" || provider === "bridge";
    if (isActive) {
      return isRecorded
        ? plural
          ? "autorisées"
          : "autorisé"
        : plural
          ? "interdites"
          : "interdit";
    }
    if (kind === "never_approved") {
      return isRecorded
        ? `${displayName} ${plural ? "enregistrées" : "enregistré"} sans approbation G2`
        : plural
          ? "non accordées (aucune approbation G2)"
          : "non accordé (aucune approbation G2)";
    }
    return isRecorded
      ? `${displayName} ${plural ? "enregistrées, non actives" : "enregistré, non actif"}`
      : `${displayName} ${plural ? "non enregistrées" : "non enregistré"} (accès inactifs)`;
  }

  return {
    kind,
    isActive,
    isRevoked,
    statusLabel,
    statusMessage,
    journalLabel,
    journalAcquisitionLabel,
    providerPermissionLabel,
    providersLabel:
      kind === "never_approved"
        ? "Fournisseurs enregistrés (aucune approbation G2)"
        : isActive
          ? "Fournisseurs autorisés"
          : "Fournisseurs enregistrés (non actifs)",
    acquisitionLabel,
    inactiveDisclosureLabel,
    technicalProvidersLabel: isActive
      ? "Identifiants de fournisseurs autorisés"
      : "Fournisseurs enregistrés (non actifs)",
    policyStatusLabel: policyLabel,
  };
}
