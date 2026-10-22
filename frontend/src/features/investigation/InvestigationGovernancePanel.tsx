import { useEffect, useState, type FormEvent } from "react";

import type {
  BudgetDimension,
  InvestigationView,
} from "../../api/investigation";
import { InvestigationApiError } from "../../api/investigation";
import {
  useApproveInvestigationEnvelope,
  useExtendInvestigationEnvelope,
  useInvestigationEnvelope,
  useRequestTargetedAnalysis,
  useRevokeInvestigationEnvelope,
  type GovernanceTlpBound,
  type GovernanceTlpInput,
  type InvestigationEnvelopeDecisionInput,
} from "../../api/investigationGovernance";

const BUDGET_DIMENSIONS: BudgetDimension[] = [
  "bridge_turns",
  "vt_units",
  "shodan_credits",
  "downloads",
  "tool_seconds",
  "context_tokens",
  "context_bytes",
];

const BUDGET_LABELS: Record<BudgetDimension, string> = {
  bridge_turns: "Tours Bridge",
  vt_units: "Unités VirusTotal",
  shodan_credits: "Crédits Shodan",
  downloads: "Téléchargements",
  tool_seconds: "Temps outils (secondes)",
  context_tokens: "Contexte (tokens)",
  context_bytes: "Contexte (octets)",
};

const TLP_OPTIONS: Array<{ value: GovernanceTlpBound; label: string }> = [
  { value: "white", label: "TLP:WHITE" },
  { value: "clear", label: "TLP:CLEAR" },
  { value: "green", label: "TLP:GREEN" },
  { value: "amber", label: "TLP:AMBER" },
  { value: "red", label: "TLP:RED" },
];
const TLP_RANK: Record<GovernanceTlpBound, number> = {
  white: 1,
  clear: 1,
  green: 2,
  amber: 3,
  red: 4,
};

type EnvelopeMode = "approve" | "extend" | "revoke";

function makeCorrelationId(): string {
  return (
    globalThis.crypto?.randomUUID?.() ??
    `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`
  );
}

function displayError(error: unknown): string {
  if (error instanceof InvestigationApiError) {
    return `${error.message} (${error.code}, HTTP ${error.status})`;
  }
  return error instanceof Error ? error.message : "Une erreur est survenue.";
}

function envelopeBudgetValues(
  envelope: ReturnType<typeof useInvestigationEnvelope>["data"],
): Record<BudgetDimension, string> {
  return Object.fromEntries(
    BUDGET_DIMENSIONS.map((dimension) => [
      dimension,
      envelope?.budget[dimension].limit?.toString() ?? "",
    ]),
  ) as Record<BudgetDimension, string>;
}

function budgetLimits(
  values: Record<BudgetDimension, string>,
): Partial<Record<BudgetDimension, number>> {
  const result: Partial<Record<BudgetDimension, number>> = {};
  for (const dimension of BUDGET_DIMENSIONS) {
    const rawValue = values[dimension].trim();
    if (rawValue !== "") result[dimension] = Number(rawValue);
  }
  return result;
}

export function InvestigationGovernancePanel({
  investigation,
}: {
  investigation: InvestigationView;
}) {
  const envelopeQuery = useInvestigationEnvelope(investigation.id);
  const approve = useApproveInvestigationEnvelope();
  const extend = useExtendInvestigationEnvelope();
  const revoke = useRevokeInvestigationEnvelope();
  const requestAnalysis = useRequestTargetedAnalysis();
  const envelope = envelopeQuery.data;
  const [mode, setMode] = useState<EnvelopeMode>("approve");
  const [providers, setProviders] = useState<string[]>([]);
  const [limits, setLimits] = useState<Record<BudgetDimension, string>>(
    envelopeBudgetValues(undefined),
  );
  const [acquireHits, setAcquireHits] = useState(false);
  const [maxVolume, setMaxVolume] = useState("");
  const [tlp, setTlp] = useState<GovernanceTlpInput | "">("");
  const [doNotSubmit, setDoNotSubmit] = useState(true);
  const [externalLlmAllowed, setExternalLlmAllowed] = useState(false);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [requestText, setRequestText] = useState("");
  const [targetKind, setTargetKind] = useState<
    "" | "hash" | "ioc" | "claim_id"
  >("");
  const [targetValue, setTargetValue] = useState("");
  const [requestAccepted, setRequestAccepted] = useState(false);
  const [requestResumed, setRequestResumed] = useState(false);

  useEffect(() => {
    if (!envelope) return;
    setMode(envelope.approved ? "extend" : "approve");
    setProviders(envelope.allowed_providers);
    setLimits(envelopeBudgetValues(envelope));
    setAcquireHits(envelope.acquire_hits_allowed);
    setMaxVolume(String(envelope.max_acquire_volume));
    setTlp(envelope.disclosure.tlp ?? "");
    setDoNotSubmit(envelope.disclosure.do_not_submit);
    setExternalLlmAllowed(false);
    setReason("");
    setConfirmed(false);
  }, [envelope]);

  const selectedProviders = new Set(providers);
  const requestTrimmed = requestText.trim();
  const minimumTlpRank = envelope?.disclosure.tlp
    ? TLP_RANK[envelope.disclosure.tlp]
    : 0;
  const targetIsValid =
    targetKind === "" ||
    (targetValue.trim().length > 0 &&
      (targetKind !== "hash" || /^[0-9a-f]{64}$/.test(targetValue.trim())));
  const requestCanSubmit =
    requestTrimmed.length > 0 &&
    requestTrimmed.length <= 2_000 &&
    targetIsValid &&
    !requestAnalysis.isPending;
  const mutationPending =
    approve.isPending || extend.isPending || revoke.isPending;
  const g2Reason = reason.trim();
  const vtBudget = limits.vt_units.trim()
    ? Number(limits.vt_units)
    : (envelope?.budget.vt_units.limit ?? 0);
  const acquisitionBudgetIsValid =
    !acquireHits ||
    (providers.includes("vt") && Number(maxVolume) > 0 && vtBudget > 0);
  const g2CanSubmit =
    g2Reason.length > 0 &&
    g2Reason.length <= 4_000 &&
    confirmed &&
    !mutationPending &&
    (mode === "revoke" ||
      (acquisitionBudgetIsValid &&
        maxVolume.trim() !== "" &&
        Number.isInteger(Number(maxVolume)) &&
        Number(maxVolume) >= 0 &&
        BUDGET_DIMENSIONS.every((dimension) => {
          const value = limits[dimension].trim();
          return (
            value === "" ||
            (Number.isInteger(Number(value)) && Number(value) >= 0)
          );
        })));

  function toggleProvider(provider: string) {
    setProviders((current) =>
      current.includes(provider)
        ? current.filter((item) => item !== provider)
        : [...current, provider],
    );
  }

  async function submitEnvelope(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!envelope || !g2CanSubmit || !g2Reason) return;
    if (mode === "revoke") {
      try {
        await revoke.mutateAsync({
          investigation_id: investigation.id,
          expected_version: envelope.version,
          reason: g2Reason,
          correlation_id: makeCorrelationId(),
        });
      } catch {
        return;
      }
      return;
    }

    const input: InvestigationEnvelopeDecisionInput = {
      investigation_id: investigation.id,
      expected_version: envelope.approved ? envelope.version : null,
      allowed_providers: providers,
      budget_limits: budgetLimits(limits),
      acquire_hits_allowed: acquireHits,
      max_acquire_volume: Number(maxVolume),
      disclosure: {
        tlp: tlp || null,
        do_not_submit: doNotSubmit,
        external_llm_allowed: externalLlmAllowed,
      },
      reason: g2Reason,
      correlation_id: makeCorrelationId(),
    };
    try {
      if (mode === "approve") await approve.mutateAsync(input);
      else await extend.mutateAsync(input);
    } catch {
      return;
    }
  }

  async function submitRequest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!requestCanSubmit) return;
    setRequestAccepted(false);
    const wasPaused = investigation.stop_reason !== null;
    try {
      await requestAnalysis.mutateAsync({
        investigation_id: investigation.id,
        text: requestTrimmed,
        target:
          targetKind && targetValue.trim()
            ? { kind: targetKind, value: targetValue.trim() }
            : null,
        expected_version: investigation.version,
      });
    } catch {
      return;
    }
    setRequestResumed(wasPaused);
    setRequestAccepted(true);
    setRequestText("");
  }

  return (
    <>
      <section
        id="investigation-envelope-panel"
        className="job-card"
        aria-labelledby="investigation-envelope-heading"
      >
        <h2 id="investigation-envelope-heading">Enveloppe G2</h2>
        {envelopeQuery.isPending ? (
          <p role="status">Chargement de l’enveloppe…</p>
        ) : envelopeQuery.isError ? (
          <p className="error-message" role="alert">
            L’enveloppe ne peut pas être chargée :{" "}
            {displayError(envelopeQuery.error)}
          </p>
        ) : envelope ? (
          <>
            <p>
              {envelope.approved
                ? `Enveloppe approuvée · version ${envelope.version}`
                : "Proposition locale non approuvée · aucun droit externe accordé"}
            </p>
            <p>
              Fournisseurs autorisés :{" "}
              {envelope.allowed_providers.join(", ") || "aucun"}
            </p>
            <p>
              Acquisition de hits :{" "}
              {envelope.acquire_hits_allowed ? "autorisée" : "interdite"}
              {` · volume maximal : ${envelope.max_acquire_volume} fichier(s)`}
            </p>
            <dl>
              {BUDGET_DIMENSIONS.map((dimension) => {
                const value = envelope.budget[dimension];
                return (
                  <div key={dimension}>
                    <dt>{BUDGET_LABELS[dimension]}</dt>
                    <dd>
                      {value.consumed} consommé · {value.reserved} réservé ·
                      plafond {value.limit ?? "illimité"} · restant{" "}
                      {value.remaining ?? "illimité"}
                    </dd>
                  </div>
                );
              })}
            </dl>
            <p>
              Divulgation : TLP:
              {envelope.disclosure.tlp?.toUpperCase() ?? "non défini"} ·{" "}
              do_not_submit :{" "}
              {envelope.disclosure.do_not_submit ? "oui" : "non"} · LLM externe
              :{" "}
              {envelope.disclosure.external_llm_allowed
                ? "autorisé"
                : "interdit"}
            </p>
            {envelope.approved ? (
              <dl>
                <div>
                  <dt>Décision G2</dt>
                  <dd>
                    {envelope.approval_decision_id ?? "Identifiant non fourni"}
                  </dd>
                </div>
                <div>
                  <dt>Acteur authentifié</dt>
                  <dd>{envelope.approval_actor_id ?? "Non renseigné"}</dd>
                </div>
                <div>
                  <dt>Décidée le</dt>
                  <dd>{envelope.approved_at ?? "Non renseignée"}</dd>
                </div>
              </dl>
            ) : (
              <p role="note">
                Rien n’est pré-approuvé. La proposition ne permet aucun pivot
                externe ni acquisition.
              </p>
            )}

            <div role="group" aria-label="Actions G2">
              {!envelope.approved ? (
                <button
                  id="investigation-envelope-approve"
                  type="button"
                  className="button button--secondary"
                  aria-pressed={mode === "approve"}
                  onClick={() => setMode("approve")}
                >
                  Approuver
                </button>
              ) : (
                <>
                  <button
                    id="investigation-envelope-extend"
                    type="button"
                    className="button button--secondary"
                    aria-pressed={mode === "extend"}
                    onClick={() => {
                      setMode("extend");
                      setReason("");
                      setConfirmed(false);
                    }}
                  >
                    Étendre l’enveloppe
                  </button>{" "}
                  <button
                    type="button"
                    className="button button--secondary"
                    aria-pressed={mode === "revoke"}
                    onClick={() => {
                      setMode("revoke");
                      setReason("");
                      setConfirmed(false);
                    }}
                  >
                    Révoquer
                  </button>
                </>
              )}
            </div>

            {mode === "revoke" && envelope.approved ? (
              <form onSubmit={(event) => void submitEnvelope(event)}>
                <label>
                  Raison obligatoire de révocation
                  <textarea
                    required
                    maxLength={4_000}
                    value={reason}
                    onChange={(event) => setReason(event.currentTarget.value)}
                  />
                </label>
                <p>
                  La révocation retire les fournisseurs et les droits
                  d’acquisition externes tout en conservant les limites locales.
                </p>
                <label>
                  <input
                    type="checkbox"
                    checked={confirmed}
                    onChange={(event) =>
                      setConfirmed(event.currentTarget.checked)
                    }
                  />
                  Je confirme la révocation de cette enveloppe, version{" "}
                  {envelope.version}.
                </label>
                <button type="submit" disabled={!g2CanSubmit}>
                  Confirmer la révocation
                </button>
              </form>
            ) : (
              <form onSubmit={(event) => void submitEnvelope(event)}>
                <fieldset>
                  <legend>Fournisseurs autorisés</legend>
                  {envelope.available_providers.length === 0 ? (
                    <p>
                      Aucun fournisseur externe n’est actuellement disponible.
                    </p>
                  ) : (
                    envelope.available_providers.map((provider) => (
                      <label key={provider}>
                        <input
                          type="checkbox"
                          checked={selectedProviders.has(provider)}
                          onChange={() => toggleProvider(provider)}
                        />
                        {provider}
                      </label>
                    ))
                  )}
                </fieldset>
                <fieldset>
                  <legend>Budget maximal par dimension</legend>
                  {BUDGET_DIMENSIONS.map((dimension) => (
                    <label key={dimension}>
                      {BUDGET_LABELS[dimension]}
                      <input
                        type="number"
                        min={0}
                        step={1}
                        value={
                          dimension === "downloads"
                            ? maxVolume
                            : limits[dimension]
                        }
                        onChange={(event) => {
                          const value = event.currentTarget.value;
                          if (dimension === "downloads") setMaxVolume(value);
                          setLimits((current) => ({
                            ...current,
                            [dimension]: value,
                          }));
                        }}
                      />
                    </label>
                  ))}
                </fieldset>
                <label>
                  <input
                    type="checkbox"
                    checked={acquireHits}
                    disabled={
                      envelope.approved && envelope.acquire_hits_allowed
                    }
                    onChange={(event) =>
                      setAcquireHits(event.currentTarget.checked)
                    }
                  />
                  Autoriser l’acquisition automatique de hits
                </label>
                {envelope.approved && envelope.acquire_hits_allowed ? (
                  <p>
                    Une extension ne peut pas retirer ce droit. Utilisez «
                    Révoquer » pour l’arrêter.
                  </p>
                ) : null}
                {acquireHits && !acquisitionBudgetIsValid ? (
                  <p className="verification-warning" role="note">
                    L’acquisition nécessite le fournisseur vt, un budget VT
                    positif et un volume maximal supérieur à zéro.
                  </p>
                ) : null}
                <label>
                  Borne TLP de divulgation
                  <select
                    value={tlp}
                    onChange={(event) =>
                      setTlp(
                        event.currentTarget.value as GovernanceTlpInput | "",
                      )
                    }
                  >
                    <option value="">Borne héritée de l’article</option>
                    {TLP_OPTIONS.filter(
                      (option) => TLP_RANK[option.value] >= minimumTlpRank,
                    ).map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                    <option value="amber+strict">TLP:AMBER+STRICT</option>
                  </select>
                </label>
                <label>
                  <input
                    type="checkbox"
                    checked={doNotSubmit}
                    disabled={envelope.disclosure.do_not_submit}
                    onChange={(event) =>
                      setDoNotSubmit(event.currentTarget.checked)
                    }
                  />
                  Interdire la soumission du contenu
                </label>
                <label>
                  <input
                    type="checkbox"
                    checked={externalLlmAllowed}
                    disabled={!envelope.disclosure.external_llm_allowed}
                    onChange={(event) =>
                      setExternalLlmAllowed(event.currentTarget.checked)
                    }
                  />
                  Autoriser l’envoi à un LLM externe
                </label>
                <p>
                  Les pivots passifs dans cette enveloppe ne demandent plus de
                  clic ; l’acquisition de hits est automatique jusqu’à{" "}
                  {maxVolume || "0"} fichier(s) si elle est autorisée ; la
                  qualification reste humaine.
                </p>
                <label>
                  Raison obligatoire
                  <textarea
                    required
                    maxLength={4_000}
                    value={reason}
                    onChange={(event) => setReason(event.currentTarget.value)}
                  />
                </label>
                <label>
                  <input
                    type="checkbox"
                    checked={confirmed}
                    onChange={(event) =>
                      setConfirmed(event.currentTarget.checked)
                    }
                  />
                  Je confirme cette décision G2 et ses fournisseurs, budgets et
                  bornes affichés.
                </label>
                <button type="submit" disabled={!g2CanSubmit}>
                  {mode === "approve"
                    ? "Confirmer l’approbation"
                    : "Confirmer l’extension"}
                </button>
              </form>
            )}
            {approve.error || extend.error || revoke.error ? (
              <p className="error-message" role="alert">
                La décision G2 a échoué :{" "}
                {displayError(approve.error ?? extend.error ?? revoke.error)}
              </p>
            ) : null}
            {approve.isSuccess || extend.isSuccess || revoke.isSuccess ? (
              <p role="status">La décision G2 a été enregistrée.</p>
            ) : null}
          </>
        ) : null}
      </section>

      <section className="job-card" aria-labelledby="targeted-analysis-heading">
        <h2 id="targeted-analysis-heading">Demander une analyse ciblée</h2>
        <form onSubmit={(event) => void submitRequest(event)}>
          <label>
            Demande (2 000 caractères maximum)
            <textarea
              required
              maxLength={2_000}
              value={requestText}
              onChange={(event) => setRequestText(event.currentTarget.value)}
            />
          </label>
          <label>
            Type de cible (facultatif)
            <select
              value={targetKind}
              onChange={(event) =>
                setTargetKind(event.currentTarget.value as typeof targetKind)
              }
            >
              <option value="">Aucune cible</option>
              <option value="hash">SHA-256</option>
              <option value="ioc">IOC</option>
              <option value="claim_id">Affirmation de l’article</option>
            </select>
          </label>
          {targetKind ? (
            <label>
              Cible (256 caractères maximum)
              <input
                required
                maxLength={256}
                aria-invalid={!targetIsValid}
                value={targetValue}
                onChange={(event) => setTargetValue(event.currentTarget.value)}
              />
              {targetKind === "hash" ? (
                <span>
                  La cible SHA-256 doit contenir 64 caractères hexadécimaux.
                </span>
              ) : null}
            </label>
          ) : null}
          <button type="submit" disabled={!requestCanSubmit}>
            {requestAnalysis.isPending ? "Envoi…" : "Demander cette analyse"}
          </button>
        </form>
        {requestAnalysis.error ? (
          <p className="error-message" role="alert">
            La demande ciblée a échoué : {displayError(requestAnalysis.error)}
          </p>
        ) : null}
        {requestAccepted ? (
          <p role="status">
            Demande prise en compte pour le prochain tour.
            {requestResumed
              ? " L’enquête en pause a été relancée automatiquement."
              : ""}
          </p>
        ) : null}
      </section>
    </>
  );
}
