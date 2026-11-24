/**
 * Vue d’ensemble — cycle de vie de l’enveloppe G2 approuvée.
 *
 * L’approbation initiale reste l’unique décision de `EnvelopeSection`. Cette
 * section ne monte que pour une enveloppe déjà approuvée et n’expose que deux
 * décisions humaines distinctes : l’extension et la révocation. Chacune exige
 * un motif, une confirmation explicite et la version attendue (CAS) ; le
 * client génère une clé d’idempotence et un garde synchrone empêche une
 * double soumission. Rien n’est affiché qui ne soit réellement renvoyé par
 * `GET /api/investigations/{id}/envelope`.
 */

import { useEffect, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent, ReactNode } from "react";

import type { BudgetDimension } from "../../../api/investigation";
import {
  useExtendInvestigationEnvelope,
  useInvestigationEnvelope,
  useRevokeInvestigationEnvelope,
  type GovernanceTlpBound,
  type GovernanceTlpInput,
  type InvestigationEnvelopeDecisionInput,
  type InvestigationEnvelopeDimensionView,
  type InvestigationEnvelopeView,
} from "../../../api/investigationGovernance";
import {
  createIdempotencyKey,
  toWorkshopApiError,
  type WorkshopApiError,
} from "../api/client";
import {
  WorkshopConflictState,
  WorkshopStateBanner,
  WorkshopTypedErrorState,
} from "../shell/WorkshopStates";
import { actorKindLabel, formatTimestamp } from "../tasks/labels";
import {
  BUDGET_DIMENSION_LABELS,
  formatBytes,
  formatCount,
} from "./overviewPresentation";

/**
 * Champs de cycle de vie déjà renvoyés par le contrat `EnvelopeView` du
 * backend, mais absents du type client partagé — hors du périmètre de ce lot.
 * La réponse est vérifiée à l’exécution : un champ manquant n’autorise rien.
 */
interface EnvelopeLifecycleView extends InvestigationEnvelopeView {
  policy_activated_by: string | null;
  authority_kind: string | null;
  inherited_policy_id: string | null;
  inherited_policy_version: number | null;
  inherited_policy_event_id: string | null;
  inherited_subject_id: string | null;
  policy_status: string | null;
  currently_authorized: boolean;
}

const BUDGET_DIMENSIONS: readonly BudgetDimension[] = [
  "bridge_turns",
  "vt_units",
  "shodan_credits",
  "downloads",
  "tool_seconds",
  "context_tokens",
  "context_bytes",
];

const PROVIDER_LABELS: Record<string, string> = {
  vt: "VirusTotal (lectures)",
  bridge: "Bridge (consultations)",
  shodan: "Shodan",
};

const TLP_OPTIONS: ReadonlyArray<{ value: GovernanceTlpInput; label: string }> =
  [
    { value: "white", label: "TLP:WHITE" },
    { value: "clear", label: "TLP:CLEAR" },
    { value: "green", label: "TLP:GREEN" },
    { value: "amber", label: "TLP:AMBER" },
    { value: "amber+strict", label: "TLP:AMBER+STRICT" },
    { value: "red", label: "TLP:RED" },
  ];

const TLP_RANK: Record<GovernanceTlpBound, number> = {
  white: 1,
  clear: 1,
  green: 2,
  amber: 3,
  red: 4,
};

const POLICY_STATUS_LABELS: Record<string, string> = {
  active: "Active",
  revoked: "Révoquée",
  superseded: "Remplacée par une extension",
};

const AUTHORITY_KIND_LABELS: Record<string, string> = {
  human_g2: "Décision humaine G2",
  osint_policy: "Politique OSINT héritée",
};

const LIFECYCLE_HEADING_ID = "overview-envelope-lifecycle-heading";

function tlpRank(value: string | null): number {
  if (value === null) return 0;
  if (value === "amber+strict") return TLP_RANK.amber;
  return value in TLP_RANK ? TLP_RANK[value as GovernanceTlpBound] : 0;
}

function parseLimit(raw: string): number | null {
  const value = raw.trim();
  if (value === "") return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : null;
}

function budgetValues(
  envelope: InvestigationEnvelopeView,
): Record<BudgetDimension, string> {
  return Object.fromEntries(
    BUDGET_DIMENSIONS.map((dimension) => [
      dimension,
      envelope.budget[dimension].limit?.toString() ?? "",
    ]),
  ) as Record<BudgetDimension, string>;
}

function isConflict(error: WorkshopApiError | null): boolean {
  return (
    error !== null &&
    (error.code === "envelope_conflict" || error.code === "envelope_revoked")
  );
}

export function EnvelopeLifecycleSection({
  investigationId,
}: {
  investigationId: string;
}) {
  const envelopeQuery = useInvestigationEnvelope(investigationId);
  const envelope = envelopeQuery.data as EnvelopeLifecycleView | undefined;
  const reload = () => void envelopeQuery.refetch();

  if (envelopeQuery.isError) {
    return (
      <section
        className="job-card workshop-envelope"
        aria-labelledby={LIFECYCLE_HEADING_ID}
      >
        <h2 id={LIFECYCLE_HEADING_ID}>Cycle de vie de l’enveloppe G2</h2>
        <WorkshopTypedErrorState
          error={toWorkshopApiError(envelopeQuery.error)}
          title="Enveloppe G2 indisponible"
          onRetry={reload}
          retryLabel="Recharger l’enveloppe"
        />
      </section>
    );
  }
  if (!envelope?.approved) return null;
  return (
    <ApprovedEnvelopeSection
      investigationId={investigationId}
      envelope={envelope}
      onReload={reload}
    />
  );
}

/** Current G2 state kept visible while the overview's G2 details are collapsed. */
export function EnvelopeAccessSummary({
  investigationId,
}: {
  investigationId: string;
}) {
  const envelopeQuery = useInvestigationEnvelope(investigationId);
  const envelope = envelopeQuery.data as EnvelopeLifecycleView | undefined;

  let label = "Accès externes : état indisponible";
  if (envelopeQuery.isPending) {
    label = "Accès externes : chargement";
  } else if (!envelopeQuery.isError && !envelope?.approved) {
    label = "Accès externes : non accordés";
  } else if (
    !envelopeQuery.isError &&
    envelope?.approved &&
    typeof envelope.currently_authorized === "boolean"
  ) {
    label = envelope.currently_authorized
      ? "Accès externes : actifs"
      : isExplicitlyRevoked(envelope)
        ? "Accès externes : révoqués"
        : "Accès externes : non autorisés";
  }

  return (
    <p className="workshop-overview-g2-summary" role="status">
      <strong>{label}</strong>
    </p>
  );
}

function ApprovedEnvelopeSection({
  investigationId,
  envelope,
  onReload,
}: {
  investigationId: string;
  envelope: EnvelopeLifecycleView;
  onReload: () => void;
}) {
  const extend = useExtendInvestigationEnvelope();
  const revoke = useRevokeInvestigationEnvelope();
  const [mode, setMode] = useState<"idle" | "extend">("idle");
  const [revokeOpen, setRevokeOpen] = useState(false);
  const [extendedVersion, setExtendedVersion] = useState<number | null>(null);

  const revokedNow = revoke.isSuccess;
  const explicitlyRevoked =
    revokedNow ||
    (!envelope.currently_authorized && isExplicitlyRevoked(envelope));
  const authorized = envelope.currently_authorized && !revokedNow;
  const inactive = !authorized;
  const revokedVersion = revokedNow
    ? (revoke.data?.version ?? envelope.version)
    : envelope.version;

  return (
    <section
      className="job-card workshop-envelope"
      aria-labelledby={LIFECYCLE_HEADING_ID}
    >
      <h2 id={LIFECYCLE_HEADING_ID}>Cycle de vie de l’enveloppe G2</h2>

      {explicitlyRevoked ? (
        <WorkshopStateBanner tone="warning" title="Accès externes : révoqués">
          <p>
            Aucune nouvelle opération VT, Shodan ou Bridge externe n’est
            autorisée. Les analyses locales restent accessibles.
          </p>
        </WorkshopStateBanner>
      ) : authorized ? (
        <WorkshopStateBanner tone="success" title="Accès externes : actifs">
          <p>
            Les fournisseurs autorisés et les plafonds actifs sont listés
            ci-dessous.
          </p>
        </WorkshopStateBanner>
      ) : (
        <WorkshopStateBanner
          tone="warning"
          title="Accès externes : non autorisés"
        >
          <p>
            {envelope.authority_kind === "osint_policy" &&
            envelope.policy_status
              ? `La politique n’autorise plus les accès externes (${POLICY_STATUS_LABELS[envelope.policy_status] ?? envelope.policy_status}).`
              : "L’autorisation courante est absente. Les analyses locales restent accessibles."}
          </p>
        </WorkshopStateBanner>
      )}

      {extendedVersion !== null && authorized ? (
        <WorkshopStateBanner tone="success" title="Enveloppe étendue">
          <p>
            La décision d’extension a été enregistrée ; les droits affichés sont
            rechargés depuis le serveur.
          </p>
        </WorkshopStateBanner>
      ) : null}

      <EnvelopeRights envelope={envelope} active={authorized} />
      <EnvelopeBudget
        envelope={envelope}
        inactive={inactive}
        explicitlyRevoked={explicitlyRevoked}
      />
      <EnvelopeDecision envelope={envelope} />
      <InheritedPolicy envelope={envelope} />
      <EnvelopeTechnicalDetails
        envelope={envelope}
        revokedVersion={revokedVersion}
        inactive={inactive}
      />

      {inactive ? null : (
        <>
          <div role="group" aria-label="Actions de cycle de vie G2">
            <button
              type="button"
              className="button button--secondary"
              onClick={() => {
                extend.reset();
                setExtendedVersion(null);
                setMode("extend");
              }}
            >
              Étendre l’enveloppe
            </button>{" "}
            <button
              type="button"
              className="button button--danger"
              onClick={() => {
                revoke.reset();
                setRevokeOpen(true);
              }}
            >
              Révoquer l’enveloppe
            </button>
          </div>

          {mode === "extend" ? (
            <ExtensionForm
              key={envelope.version}
              investigationId={investigationId}
              envelope={envelope}
              extend={extend}
              onClose={() => setMode("idle")}
              onReload={onReload}
              onSuccess={(version) => {
                setExtendedVersion(version);
                setMode("idle");
              }}
            />
          ) : null}
        </>
      )}

      {revokeOpen ? (
        <RevocationDialog
          investigationId={investigationId}
          envelope={envelope}
          revoke={revoke}
          onClose={() => setRevokeOpen(false)}
          onReload={onReload}
          onSuccess={() => setRevokeOpen(false)}
        />
      ) : null}
    </section>
  );
}

function isExplicitlyRevoked(envelope: EnvelopeLifecycleView): boolean {
  return (
    envelope.authority_kind === "human_g2" ||
    (envelope.authority_kind === "osint_policy" &&
      envelope.policy_status === "revoked")
  );
}

function EnvelopeRights({
  envelope,
  active,
}: {
  envelope: InvestigationEnvelopeView;
  active: boolean;
}) {
  const providers = envelope.allowed_providers;
  const otherProviders = providers.filter(
    (provider) => !(provider in PROVIDER_LABELS),
  );
  return (
    <section aria-labelledby="overview-envelope-rights-heading">
      <h3 id="overview-envelope-rights-heading">
        {active
          ? "Droits autorisés"
          : "Fournisseurs de l’enveloppe enregistrée (non actifs)"}
      </h3>
      <dl className="workshop-meta">
        <dt>VirusTotal (lectures)</dt>
        <dd>
          {providers.includes("vt")
            ? active
              ? "autorisées"
              : "VirusTotal (lectures) enregistrées, non actives"
            : "interdites"}
        </dd>
        <dt>Bridge (consultations)</dt>
        <dd>
          {providers.includes("bridge")
            ? active
              ? "autorisées"
              : "Bridge (consultations) enregistrées, non actives"
            : "interdites"}
        </dd>
        <dt>Shodan</dt>
        <dd>
          {providers.includes("shodan")
            ? active
              ? "autorisé"
              : "Shodan enregistré, non actif"
            : "interdit"}
        </dd>
        <dt>Acquisition d’échantillons</dt>
        <dd>
          {envelope.acquire_hits_allowed
            ? active
              ? `autorisée · ${formatCount(envelope.max_acquire_volume)} fichier(s) maximum`
              : `enregistrée, non active · ${formatCount(envelope.max_acquire_volume)} fichier(s) maximum`
            : "interdite"}
        </dd>
        <dt>Divulgation</dt>
        <dd>
          TLP{" "}
          {envelope.disclosure.tlp
            ? envelope.disclosure.tlp.toUpperCase()
            : "non déclaré"}{" "}
          · soumission du contenu :{" "}
          {envelope.disclosure.do_not_submit
            ? "interdite (do_not_submit)"
            : "autorisée"}{" "}
          · LLM externe :{" "}
          {envelope.disclosure.external_llm_allowed ? "autorisé" : "interdit"}
        </dd>
        {otherProviders.length > 0 ? (
          <>
            <dt>Autres fournisseurs</dt>
            <dd>
              {formatCount(otherProviders.length)} fournisseur(s) enregistré(s)
            </dd>
          </>
        ) : null}
      </dl>
    </section>
  );
}

function EnvelopeBudget({
  envelope,
  inactive,
  explicitlyRevoked,
}: {
  envelope: EnvelopeLifecycleView;
  inactive: boolean;
  explicitlyRevoked: boolean;
}) {
  return (
    <section aria-labelledby="overview-envelope-budget-heading">
      <h3 id="overview-envelope-budget-heading">Budget et consommation</h3>
      <p>
        Les invocations d’outils des opérateurs locaux consomment les budgets de
        temps d’outil et de contexte affichés ci-dessous.
      </p>
      <dl className="workshop-meta">
        {BUDGET_DIMENSIONS.map((dimension) => (
          <div key={dimension}>
            <dt>{BUDGET_DIMENSION_LABELS[dimension]}</dt>
            <dd>
              {budgetLine(
                dimension,
                envelope.budget[dimension],
                isExternalBudgetActive(dimension, envelope, inactive),
                explicitlyRevoked,
              )}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function budgetLine(
  dimension: BudgetDimension,
  value: InvestigationEnvelopeDimensionView,
  active: boolean,
  explicitlyRevoked: boolean,
): string {
  if (!active) {
    return explicitlyRevoked
      ? `Historique : ${budgetQuantity(dimension, value.consumed)} (autorisation révoquée, aucun plafond actif)`
      : `Historique : ${budgetQuantity(dimension, value.consumed)} (aucune autorisation active, aucun plafond actif)`;
  }
  const format = dimension === "context_bytes" ? formatBytes : formatCount;
  const limit = value.limit === null ? "illimité" : format(value.limit);
  const remaining =
    value.remaining === null ? "illimité" : format(value.remaining);
  return `Consommation : ${budgetQuantity(dimension, value.consumed)} · réservé : ${budgetQuantity(dimension, value.reserved, true)} · plafond actif : ${limit} · restant : ${remaining}`;
}

function budgetQuantity(
  dimension: BudgetDimension,
  value: number,
  reserved = false,
): string {
  const count =
    dimension === "context_bytes" ? formatBytes(value) : formatCount(value);
  const plural = value === 1 ? "" : "s";
  switch (dimension) {
    case "bridge_turns":
      return `${count} tour${plural} Bridge ${reserved ? "réservé" : "consommé"}${plural}`;
    case "vt_units":
      return `${count} unité${plural} VirusTotal ${reserved ? "réservée" : "consommée"}${plural}`;
    case "shodan_credits":
      return `${count} crédit${plural} Shodan ${reserved ? "réservé" : "consommé"}${plural}`;
    case "downloads":
      return `${count} fichier${plural} ${reserved ? "réservé" : "téléchargé"}${plural}`;
    case "tool_seconds":
      return `${count} seconde${plural} d’outil ${reserved ? "réservée" : "consommée"}${plural}`;
    case "context_tokens":
      return `${count} token${plural} de contexte ${reserved ? "réservé" : "consommé"}${plural}`;
    case "context_bytes":
      return `${count} ${reserved ? "réservés" : "consommés"}`;
  }
}

function isExternalBudgetActive(
  dimension: BudgetDimension,
  envelope: EnvelopeLifecycleView,
  revoked: boolean,
): boolean {
  switch (dimension) {
    case "bridge_turns":
      return (
        envelope.currently_authorized &&
        !revoked &&
        envelope.allowed_providers.includes("bridge")
      );
    case "vt_units":
      return (
        envelope.currently_authorized &&
        !revoked &&
        envelope.allowed_providers.includes("vt")
      );
    case "shodan_credits":
      return (
        envelope.currently_authorized &&
        !revoked &&
        envelope.allowed_providers.includes("shodan")
      );
    case "downloads":
      return (
        envelope.allowed_providers.includes("vt") &&
        envelope.currently_authorized &&
        !revoked &&
        envelope.acquire_hits_allowed
      );
    default:
      return true;
  }
}

function EnvelopeDecision({ envelope }: { envelope: EnvelopeLifecycleView }) {
  const policyAuthority = envelope.authority_kind === "osint_policy";
  return (
    <section aria-labelledby="overview-envelope-decision-heading">
      <h3 id="overview-envelope-decision-heading">Dernière décision G2</h3>
      <dl className="workshop-meta">
        <dt>{policyAuthority ? "Activée par" : "Acteur authentifié"}</dt>
        <dd>
          {policyAuthority
            ? actorKindLabel(envelope.policy_activated_by)
            : actorKindLabel(envelope.approval_actor_id)}
        </dd>
        <dt>{policyAuthority ? "Activée le" : "Décidée le"}</dt>
        <dd>
          {envelope.approved_at ? (
            <time dateTime={envelope.approved_at}>
              {formatTimestamp(envelope.approved_at)}
            </time>
          ) : (
            "Non renseignée"
          )}
        </dd>
        <dt>Motif</dt>
        <dd>Non transmis par l’API d’enveloppe.</dd>
      </dl>
    </section>
  );
}

function EnvelopeTechnicalDetails({
  envelope,
  revokedVersion,
  inactive,
}: {
  envelope: EnvelopeLifecycleView;
  revokedVersion: number;
  inactive: boolean;
}) {
  return (
    <details>
      <summary>Détails techniques</summary>
      <dl className="workshop-meta">
        <dt>Version de l’enveloppe</dt>
        <dd>{inactive ? revokedVersion : envelope.version}</dd>
        {envelope.approval_actor_id ? (
          <>
            <dt>Identifiant de l’acteur authentifié</dt>
            <dd>
              <code>{envelope.approval_actor_id}</code>
            </dd>
          </>
        ) : null}
        {envelope.policy_activated_by ? (
          <>
            <dt>Identifiant de l’acteur ayant activé la politique</dt>
            <dd>
              <code>{envelope.policy_activated_by}</code>
            </dd>
          </>
        ) : null}
        {envelope.allowed_providers.length > 0 ? (
          <>
            <dt>
              {inactive
                ? "Fournisseurs enregistrés (non actifs)"
                : "Identifiants de fournisseurs autorisés"}
            </dt>
            <dd>
              <code>{envelope.allowed_providers.join(", ")}</code>
            </dd>
          </>
        ) : null}
        <dt>Identifiant de décision</dt>
        <dd>
          <code>{envelope.approval_decision_id ?? "Non fourni"}</code>
        </dd>
        {envelope.inherited_policy_id ? (
          <>
            <dt>Identifiant de la politique héritée</dt>
            <dd>
              <code>{envelope.inherited_policy_id}</code>
            </dd>
          </>
        ) : null}
        {envelope.inherited_policy_version !== null ? (
          <>
            <dt>Version de la politique héritée</dt>
            <dd>{envelope.inherited_policy_version}</dd>
          </>
        ) : null}
        {envelope.inherited_policy_event_id ? (
          <>
            <dt>Identifiant de l’événement hérité</dt>
            <dd>
              <code>{envelope.inherited_policy_event_id}</code>
            </dd>
          </>
        ) : null}
        {envelope.inherited_subject_id ? (
          <>
            <dt>Identifiant du sujet de politique</dt>
            <dd>
              <code>{envelope.inherited_subject_id}</code>
            </dd>
          </>
        ) : null}
      </dl>
    </details>
  );
}

function InheritedPolicy({ envelope }: { envelope: EnvelopeLifecycleView }) {
  const rows: Array<[string, ReactNode]> = [];
  if (envelope.authority_kind) {
    rows.push([
      "Autorité",
      AUTHORITY_KIND_LABELS[envelope.authority_kind] ?? "Autorité enregistrée",
    ]);
  }
  if (envelope.policy_status) {
    rows.push([
      "Statut de la politique",
      POLICY_STATUS_LABELS[envelope.policy_status] ??
        "Statut de politique enregistré",
    ]);
  }
  if (rows.length === 0) return null;
  return (
    <section aria-labelledby="overview-envelope-policy-heading">
      <h3 id="overview-envelope-policy-heading">Politique héritée</h3>
      <dl className="workshop-meta">
        {rows.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function ExtensionForm({
  investigationId,
  envelope,
  extend,
  onClose,
  onReload,
  onSuccess,
}: {
  investigationId: string;
  envelope: EnvelopeLifecycleView;
  extend: ReturnType<typeof useExtendInvestigationEnvelope>;
  onClose: () => void;
  onReload: () => void;
  onSuccess: (version: number) => void;
}) {
  const [providers, setProviders] = useState<string[]>([
    ...envelope.allowed_providers,
  ]);
  const [limits, setLimits] = useState<Record<BudgetDimension, string>>(() =>
    budgetValues(envelope),
  );
  const [acquireHits, setAcquireHits] = useState(envelope.acquire_hits_allowed);
  const [tlp, setTlp] = useState<GovernanceTlpInput | "">("");
  const [doNotSubmit, setDoNotSubmit] = useState(
    envelope.disclosure.do_not_submit,
  );
  const [externalLlm, setExternalLlm] = useState(
    envelope.disclosure.external_llm_allowed,
  );
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const submittingRef = useRef(false);
  const correlationRef = useRef<string | null>(null);

  const currentProviders = new Set(envelope.allowed_providers);
  const providerOptions = Array.from(
    new Set([...envelope.allowed_providers, ...envelope.available_providers]),
  ).sort();
  const unavailableCurrent = envelope.allowed_providers.filter(
    (provider) => !envelope.available_providers.includes(provider),
  );
  const limitsValid = BUDGET_DIMENSIONS.every(
    (dimension) =>
      limits[dimension].trim() === "" || parseLimit(limits[dimension]) !== null,
  );
  const downloadsUnlimited = envelope.budget.downloads.limit === null;
  const vtBudget =
    parseLimit(limits.vt_units) ?? envelope.budget.vt_units.limit ?? 0;
  const maxVolume = parseLimit(limits.downloads);
  const acquisitionValid =
    !acquireHits ||
    (providers.includes("vt") &&
      vtBudget > 0 &&
      !downloadsUnlimited &&
      (maxVolume ?? 0) > 0);
  const canSubmit =
    reason.trim() !== "" &&
    confirmed &&
    limitsValid &&
    acquisitionValid &&
    unavailableCurrent.length === 0 &&
    !extend.isPending;

  const toggleProvider = (provider: string) => {
    if (currentProviders.has(provider)) return;
    setProviders((current) =>
      current.includes(provider)
        ? current.filter((item) => item !== provider)
        : [...current, provider],
    );
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submittingRef.current || !canSubmit) return;
    submittingRef.current = true;
    const budgetLimits: Partial<Record<BudgetDimension, number>> = {};
    for (const dimension of BUDGET_DIMENSIONS) {
      const parsed = parseLimit(limits[dimension]);
      if (parsed !== null) budgetLimits[dimension] = parsed;
    }
    const correlationId = correlationRef.current ?? createIdempotencyKey();
    correlationRef.current = correlationId;
    const input: InvestigationEnvelopeDecisionInput = {
      investigation_id: investigationId,
      expected_version: envelope.version,
      allowed_providers: providers,
      budget_limits: budgetLimits,
      acquire_hits_allowed: acquireHits,
      max_acquire_volume: downloadsUnlimited ? null : maxVolume,
      disclosure: {
        tlp: tlp === "" ? null : tlp,
        do_not_submit: doNotSubmit,
        external_llm_allowed: externalLlm,
      },
      reason: reason.trim(),
      correlation_id: correlationId,
    };
    try {
      const result = await extend.mutateAsync(input);
      onSuccess(result.version);
    } catch {
      // L’erreur typée est rendue depuis `extend.error`.
    } finally {
      submittingRef.current = false;
    }
  };

  const error = extend.error ? toWorkshopApiError(extend.error) : null;

  return (
    <form
      className="workshop-approval-form"
      onSubmit={(event) => void submit(event)}
    >
      <h3>Étendre l’enveloppe approuvée</h3>
      <p role="note">
        Version attendue (CAS) : {envelope.version} · décision{" "}
        <code>{envelope.approval_decision_id ?? "non fournie"}</code>. Une clé
        d’idempotence est générée par le client à la soumission ; le double-clic
        ne produit qu’une seule requête.
      </p>

      <fieldset>
        <legend>Fournisseurs autorisés</legend>
        {providerOptions.map((provider) => (
          <label key={provider}>
            <input
              type="checkbox"
              checked={providers.includes(provider)}
              disabled={currentProviders.has(provider)}
              onChange={() => toggleProvider(provider)}
            />
            {PROVIDER_LABELS[provider] ?? provider}
            {currentProviders.has(provider)
              ? " — déjà autorisé, ne peut pas être retiré par une extension"
              : ""}
          </label>
        ))}
      </fieldset>
      {unavailableCurrent.length > 0 ? (
        <p role="note">
          Fournisseur(s) autorisé(s) devenu(s) indisponible(s) :{" "}
          {unavailableCurrent.join(", ")}. Une extension ne peut ni les retirer
          ni les reconduire : révoquez l’enveloppe ou rétablissez la capacité.
        </p>
      ) : null}

      <fieldset>
        <legend>Budget maximal par dimension</legend>
        {BUDGET_DIMENSIONS.map((dimension) => {
          const currentLimit = envelope.budget[dimension].limit;
          return (
            <label key={dimension}>
              {BUDGET_DIMENSION_LABELS[dimension]}
              <input
                type="number"
                min={currentLimit ?? 0}
                step={1}
                value={limits[dimension]}
                disabled={currentLimit === null}
                onChange={(event) =>
                  setLimits((current) => ({
                    ...current,
                    [dimension]: event.currentTarget.value,
                  }))
                }
              />
              {currentLimit === null
                ? " (illimité : une extension ne peut pas le plafonner)"
                : null}
            </label>
          );
        })}
      </fieldset>

      <label className="workshop-approval-form__confirm">
        <input
          type="checkbox"
          checked={acquireHits}
          disabled={envelope.acquire_hits_allowed || downloadsUnlimited}
          onChange={(event) => setAcquireHits(event.currentTarget.checked)}
        />
        <span>Autoriser l’acquisition automatique de hits</span>
      </label>
      {envelope.acquire_hits_allowed ? (
        <p role="note">
          Le droit d’acquisition ne peut pas être retiré par une extension :
          utilisez « Révoquer l’enveloppe » pour l’arrêter.
        </p>
      ) : null}
      {downloadsUnlimited ? (
        <p role="note">
          Les téléchargements sont illimités dans cette enveloppe ; une
          extension ne peut pas les plafonner, donc l’acquisition ne peut pas
          être activée ici.
        </p>
      ) : null}
      {acquireHits && !acquisitionValid ? (
        <p role="note">
          L’acquisition nécessite le fournisseur VirusTotal, un budget VT
          positif et un volume maximal supérieur à zéro.
        </p>
      ) : null}

      <label>
        Borne TLP de divulgation
        <select
          value={tlp}
          onChange={(event) =>
            setTlp(event.currentTarget.value as GovernanceTlpInput | "")
          }
        >
          <option value="">
            Conserver la borne actuelle
            {envelope.disclosure.tlp
              ? ` (TLP:${envelope.disclosure.tlp.toUpperCase()})`
              : ""}
          </option>
          {TLP_OPTIONS.filter(
            (option) =>
              tlpRank(option.value) >= tlpRank(envelope.disclosure.tlp),
          ).map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      <label className="workshop-approval-form__confirm">
        <input
          type="checkbox"
          checked={doNotSubmit}
          disabled={envelope.disclosure.do_not_submit}
          onChange={(event) => setDoNotSubmit(event.currentTarget.checked)}
        />
        <span>
          Interdire la soumission du contenu
          {envelope.disclosure.do_not_submit
            ? " (déjà imposée : une extension ne peut pas la retirer)"
            : ""}
        </span>
      </label>
      <label className="workshop-approval-form__confirm">
        <input
          type="checkbox"
          checked={externalLlm}
          disabled={!envelope.disclosure.external_llm_allowed}
          onChange={(event) => setExternalLlm(event.currentTarget.checked)}
        />
        <span>
          Autoriser l’envoi à un LLM externe
          {envelope.disclosure.external_llm_allowed
            ? ""
            : " (interdit : une extension ne peut pas l’autoriser)"}
        </span>
      </label>

      <label>
        Motif de l’extension
        <textarea
          required
          maxLength={4_000}
          value={reason}
          onChange={(event) => setReason(event.currentTarget.value)}
        />
      </label>
      <label className="workshop-approval-form__confirm">
        <input
          type="checkbox"
          checked={confirmed}
          onChange={(event) => setConfirmed(event.currentTarget.checked)}
        />
        <span>
          Je confirme cette extension de l’enveloppe version {envelope.version}{" "}
          et ses fournisseurs, budgets et bornes affichés.
        </span>
      </label>

      {error !== null && isConflict(error) ? (
        <>
          <WorkshopConflictState
            reason={error.message}
            expectedVersion={envelope.version}
          />
          <button
            type="button"
            className="button button--secondary"
            onClick={onReload}
          >
            Recharger l’enveloppe
          </button>
        </>
      ) : error !== null ? (
        <WorkshopTypedErrorState
          error={error}
          title="Extension refusée"
          onRetry={onReload}
          retryLabel="Recharger l’enveloppe"
        />
      ) : null}

      <div>
        <button
          type="submit"
          className="button button--secondary"
          disabled={!canSubmit}
        >
          {extend.isPending ? "Extension en cours…" : "Confirmer l’extension"}
        </button>{" "}
        <button
          type="button"
          className="button button--secondary"
          disabled={extend.isPending}
          onClick={onClose}
        >
          Annuler
        </button>
      </div>
    </form>
  );
}

function RevocationDialog({
  investigationId,
  envelope,
  revoke,
  onClose,
  onReload,
  onSuccess,
}: {
  investigationId: string;
  envelope: EnvelopeLifecycleView;
  revoke: ReturnType<typeof useRevokeInvestigationEnvelope>;
  onClose: () => void;
  onReload: () => void;
  onSuccess: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const reasonRef = useRef<HTMLTextAreaElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const submittingRef = useRef(false);
  const correlationRef = useRef<string | null>(null);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    openerRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    reasonRef.current?.focus({ preventScroll: true });
    return () => {
      if (dialog.open && typeof dialog.close === "function") dialog.close();
      openerRef.current?.focus?.({ preventScroll: true });
    };
  }, []);

  const handleKeyDown = (event: KeyboardEvent<HTMLDialogElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      if (!revoke.isPending) onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const focusable = Array.from(
      dialog.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((element) => !element.hidden);
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!first || !last) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const canSubmit = reason.trim() !== "" && confirmed && !revoke.isPending;

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submittingRef.current || !canSubmit) return;
    submittingRef.current = true;
    const correlationId = correlationRef.current ?? createIdempotencyKey();
    correlationRef.current = correlationId;
    try {
      await revoke.mutateAsync({
        investigation_id: investigationId,
        expected_version: envelope.version,
        reason: reason.trim(),
        correlation_id: correlationId,
      });
      onSuccess();
    } catch {
      // L’erreur typée est rendue depuis `revoke.error`.
    } finally {
      submittingRef.current = false;
    }
  };

  const error = revoke.error ? toWorkshopApiError(revoke.error) : null;

  return (
    <dialog
      ref={dialogRef}
      className="job-card"
      role="dialog"
      aria-modal="true"
      aria-labelledby="overview-envelope-revoke-title"
      aria-describedby="overview-envelope-revoke-description"
      onKeyDown={handleKeyDown}
      onCancel={(event) => {
        event.preventDefault();
        if (!revoke.isPending) onClose();
      }}
    >
      <h3 id="overview-envelope-revoke-title">Révoquer l’enveloppe G2 ?</h3>
      <p id="overview-envelope-revoke-description">
        La révocation coupe les droits externes de l’enveloppe version{" "}
        {envelope.version} : fournisseurs, acquisition d’échantillons et appels
        externes. Les limites locales sont conservées. Cette décision humaine
        est versionnée ; aucune ré-approbation n’est proposée ensuite.
      </p>
      <form
        className="workshop-approval-form"
        onSubmit={(event) => void submit(event)}
      >
        <label>
          Motif obligatoire de la révocation
          <textarea
            ref={reasonRef}
            required
            maxLength={4_000}
            value={reason}
            onChange={(event) => setReason(event.currentTarget.value)}
          />
        </label>
        <label className="workshop-approval-form__confirm">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => setConfirmed(event.currentTarget.checked)}
          />
          <span>
            Je confirme la révocation de l’enveloppe version {envelope.version}{" "}
            et la coupure de ses droits externes.
          </span>
        </label>

        {error !== null && isConflict(error) ? (
          <>
            <WorkshopConflictState
              reason={error.message}
              expectedVersion={envelope.version}
            />
            <button
              type="button"
              className="button button--secondary"
              onClick={() => {
                onReload();
                onClose();
              }}
            >
              Recharger l’enveloppe
            </button>
          </>
        ) : error !== null ? (
          <WorkshopTypedErrorState
            error={error}
            title="Révocation refusée"
            onRetry={onReload}
            retryLabel="Recharger l’enveloppe"
          />
        ) : null}

        <div>
          <button
            type="submit"
            className="button button--danger"
            disabled={!canSubmit}
          >
            {revoke.isPending
              ? "Révocation en cours…"
              : "Confirmer la révocation"}
          </button>{" "}
          <button
            type="button"
            className="button button--secondary"
            disabled={revoke.isPending}
            onClick={onClose}
          >
            Annuler
          </button>
        </div>
      </form>
    </dialog>
  );
}
