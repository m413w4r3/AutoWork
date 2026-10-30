import { useState, type FormEvent } from "react";

import {
  useInvestigationDetections,
  useReviewDetection,
  type DetectionRateMetric,
  type DetectionTestReport,
  type DetectionTestRole,
  type DetectionVersionReview,
  type RuleValidationStatus,
} from "../../api/detection";
import {
  type BudgetDimension,
  type InvestigationFinding,
  type InvestigationObservation,
  type InvestigationSampleAssociation,
  type InvestigationView,
} from "../../api/investigation";
import { InvestigationApiError } from "../../api/investigation";
import {
  useInvestigationActivity,
  useInvestigationActivityHistory,
  type InvestigationActivityBlockerKind,
  type InvestigationActivityInvocation,
} from "../../api/investigationOperator";
import {
  useInvestigationSamples,
  useQualifyInvestigationSample,
  type InvestigationSampleDetail,
  type SampleQualificationState,
} from "../../api/investigationGovernance";

const FINDING_KIND_LABELS: Record<InvestigationFinding["kind"], string> = {
  new_sample: "Nouvel échantillon",
  infection_chain: "Chaîne d’infection",
  capability: "Capacité",
  network_behaviour: "Comportement réseau",
  claim_verification: "Vérification d’une affirmation",
  detection: "Détection",
};

const ASSESSMENT_LABELS: Record<InvestigationFinding["assessment"], string> = {
  supported: "Étayé",
  refuted: "Réfuté",
  qualified: "Nuancé",
  inconclusive: "Non établi",
};

const NOVELTY_LABELS: Record<
  NonNullable<InvestigationFinding["novelty"]>,
  string
> = {
  new_to_article: "Nouveau dans cet article",
  already_documented_but_new_detail: "Déjà documenté, détail nouveau",
  claim_correction: "Correction d’une affirmation",
  not_confirmed: "Nouveauté non confirmée",
};

const SAMPLE_ORIGIN_LABELS: Record<
  InvestigationSampleAssociation["origin"],
  string
> = {
  seed: "Échantillon initial",
  vt_pivot: "Pivot VirusTotal",
  relationship: "Relation observée",
  behavior: "Comportement",
  manual: "Ajout manuel",
};

const RULE_VALIDATION_STATUS_LABELS: Record<RuleValidationStatus, string> = {
  untested: "Non testée",
  synthetic_only: "Testée uniquement sur fichiers synthétiques",
  real_partial: "Évaluation partielle sur fichiers réels",
  real_validated: "Évaluée sur échantillons réels",
};

const DETECTION_ROLE_LABELS: Record<DetectionTestRole, string> = {
  confirmed_positive: "Positifs confirmés",
  out_of_cluster_candidate: "Candidats hors groupe",
  goodware: "Goodware",
  holdout: "Holdout",
};

const SAMPLE_QUALIFICATION_LABELS: Record<
  InvestigationSampleAssociation["qualification"],
  string
> = {
  candidate: "Candidat",
  related: "Associé",
  confirmed: "Confirmé",
  rejected: "Rejeté",
  uncertain: "Incertain",
};

const BUDGET_LABELS: Record<BudgetDimension, string> = {
  bridge_turns: "Tours Bridge",
  vt_units: "Unités VirusTotal",
  shodan_credits: "Crédits Shodan",
  downloads: "Téléchargements",
  tool_seconds: "Temps outils (secondes)",
  context_tokens: "Contexte (tokens)",
  context_bytes: "Contexte (octets)",
};

const BUDGET_ORDER: BudgetDimension[] = [
  "bridge_turns",
  "vt_units",
  "shodan_credits",
  "downloads",
  "tool_seconds",
  "context_tokens",
  "context_bytes",
];

function observationLabel(id: string): string {
  return `Preuve ${id.slice(0, 8)}`;
}

export function InvestigationFindings({
  findings,
  observations,
}: {
  findings: InvestigationFinding[];
  observations: InvestigationObservation[];
}) {
  const byId = new Map(observations.map((item) => [item.id, item]));
  return (
    <section
      className="job-card"
      aria-labelledby="investigation-findings-heading"
    >
      <h2 id="investigation-findings-heading">Constats importants</h2>
      {findings.length === 0 ? (
        <p>Aucun constat n’a encore été enregistré.</p>
      ) : (
        <ul>
          {findings.map((finding) => (
            <li key={finding.id}>
              <h3>{FINDING_KIND_LABELS[finding.kind]}</h3>
              <p>{finding.statement}</p>
              <p>
                <strong>Évaluation :</strong>{" "}
                {ASSESSMENT_LABELS[finding.assessment]}
                {finding.novelty ? (
                  <>
                    {" "}
                    · <strong>Nouveauté :</strong>{" "}
                    {NOVELTY_LABELS[finding.novelty]}
                  </>
                ) : null}
              </p>
              <p>
                <strong>Portée :</strong> {finding.scope || "Non précisée"}
              </p>
              {finding.limitations.length > 0 ? (
                <div>
                  <strong>Limites</strong>
                  <ul>
                    {finding.limitations.map((limitation, index) => (
                      <li key={`${finding.id}-limit-${index}`}>{limitation}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
              <EvidenceReferences refs={finding.observation_refs} byId={byId} />
              {finding.claim_refs.length > 0 ? (
                <p>
                  <strong>Affirmations concernées :</strong>{" "}
                  {finding.claim_refs.join(", ")}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function InvestigationSamples({
  investigationId,
  observations,
}: {
  investigationId: string;
  observations: InvestigationObservation[];
}) {
  const samplesQuery = useInvestigationSamples(investigationId);

  return (
    <section
      className="job-card"
      aria-labelledby="investigation-samples-heading"
    >
      <h2 id="investigation-samples-heading">
        Nouvelles variantes et échantillons
      </h2>
      {samplesQuery.isPending ? (
        <p role="status">Chargement des associations et de leur historique…</p>
      ) : samplesQuery.isError ? (
        <p className="error-message" role="alert">
          Les échantillons ne peuvent pas être chargés :{" "}
          {samplesQuery.error.message}
        </p>
      ) : samplesQuery.data.length === 0 ? (
        <p>Aucun échantillon associé à cette enquête.</p>
      ) : (
        <ul>
          {samplesQuery.data.map((sample) => (
            <SampleQualificationCard
              key={sample.id}
              investigationId={investigationId}
              sample={sample}
              observations={observations}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function SampleQualificationCard({
  investigationId,
  sample,
  observations,
}: {
  investigationId: string;
  sample: InvestigationSampleDetail;
  observations: InvestigationObservation[];
}) {
  const qualify = useQualifyInvestigationSample();
  const [qualification, setQualification] = useState<SampleQualificationState>(
    sample.qualification,
  );
  const [reason, setReason] = useState("");
  const [evidenceRefs, setEvidenceRefs] = useState<string[]>([]);
  const [confirmed, setConfirmed] = useState(false);
  const reasonIsValid =
    reason.trim().length > 0 && reason.trim().length <= 4_000;
  const requiresEvidence =
    qualification === "confirmed" || qualification === "rejected";
  const canSubmit =
    reasonIsValid &&
    confirmed &&
    (!requiresEvidence || evidenceRefs.length > 0) &&
    qualification !== sample.qualification &&
    !qualify.isPending;
  const sampleLabel = sample.sample_sha256.slice(0, 12);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit || !reasonIsValid) return;
    try {
      await qualify.mutateAsync({
        investigation_id: investigationId,
        association_id: sample.id,
        expected_version: sample.version,
        qualification,
        reason: reason.trim(),
        evidence_refs: evidenceRefs,
        correlation_id:
          globalThis.crypto?.randomUUID?.() ??
          `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      });
    } catch {
      return;
    }
    setReason("");
    setEvidenceRefs([]);
    setConfirmed(false);
  }

  return (
    <li>
      <p>
        <strong>SHA-256 :</strong> <code>{sample.sample_sha256}</code>
      </p>
      <p>
        {SAMPLE_ORIGIN_LABELS[sample.origin]} · qualification courante :{" "}
        <strong>{SAMPLE_QUALIFICATION_LABELS[sample.qualification]}</strong> ·
        version CAS {sample.version}
      </p>
      {sample.origin === "seed" ? (
        <p role="note">
          Échantillon déjà présent dans l’article étudié ; il n’est pas présenté
          comme une nouveauté.
        </p>
      ) : null}
      {sample.related_sample_sha256.length > 0 ? (
        <p>
          <strong>Échantillons liés :</strong>{" "}
          {sample.related_sample_sha256.map((sha256) => (
            <code key={sha256}>{sha256} </code>
          ))}
        </p>
      ) : null}
      <p className="verification-warning" role="note">
        Acquisition ≠ confirmation. Un échantillon acquis reste candidat tant
        qu’une décision humaine n’a pas eu lieu. L’acteur est pris de l’identité
        authentifiée côté serveur.
      </p>
      <h3>Historique des qualifications</h3>
      {sample.changes.length === 0 ? (
        <p>Aucun changement de qualification enregistré.</p>
      ) : (
        <ol>
          {sample.changes.map((change, index) => (
            <li key={`${change.changed_at}-${index}`}>
              {SAMPLE_QUALIFICATION_LABELS[change.from_qualification]} →{" "}
              {SAMPLE_QUALIFICATION_LABELS[change.to_qualification]} ·{" "}
              {change.actor_kind} {change.actor_id} · {change.changed_at} ·{" "}
              {change.reason}
            </li>
          ))}
        </ol>
      )}
      <form onSubmit={(event) => void submit(event)}>
        <label>
          Décision G3 pour {sampleLabel}
          <select
            value={qualification}
            onChange={(event) =>
              setQualification(
                event.currentTarget.value as SampleQualificationState,
              )
            }
          >
            <option value="candidate">Candidat</option>
            <option value="related">Lié</option>
            <option value="uncertain">Incertain</option>
            <option value="confirmed">Confirmé</option>
            <option value="rejected">Rejeté</option>
          </select>
        </label>
        <label>
          Raison obligatoire pour {sampleLabel}
          <textarea
            required
            maxLength={4_000}
            value={reason}
            onChange={(event) => setReason(event.currentTarget.value)}
          />
        </label>
        <label>
          Preuves citées
          {requiresEvidence
            ? " (obligatoires pour confirmer ou rejeter)"
            : " (facultatives)"}
          <select
            multiple
            aria-label={`Preuves pour ${sampleLabel}`}
            value={evidenceRefs}
            onChange={(event) =>
              setEvidenceRefs(
                Array.from(
                  event.currentTarget.selectedOptions,
                  (option) => option.value,
                ),
              )
            }
          >
            {observations.map((observation) => (
              <option key={observation.id} value={observation.id}>
                {observation.source} · {observation.observed_at} · SHA-256{" "}
                {observation.sha256}
              </option>
            ))}
          </select>
        </label>
        <label>
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => setConfirmed(event.currentTarget.checked)}
          />
          Je confirme cette décision de qualification humaine pour {sampleLabel}
          .
        </label>
        <button type="submit" disabled={!canSubmit}>
          {qualification === "confirmed"
            ? "Confirmer"
            : qualification === "rejected"
              ? "Rejeter"
              : qualification === "related"
                ? "Marquer lié"
                : qualification === "uncertain"
                  ? "Marquer incertain"
                  : "Marquer candidat"}
        </button>
      </form>
      {qualify.error ? (
        <p className="error-message" role="alert">
          La qualification G3 a échoué : {displayGovernanceError(qualify.error)}
        </p>
      ) : null}
      {qualify.isSuccess ? (
        <p role="status">
          Qualification enregistrée avec l’acteur authentifié.
        </p>
      ) : null}
    </li>
  );
}

function displayGovernanceError(error: unknown): string {
  if (error instanceof InvestigationApiError) {
    return `${error.message} (${error.code}, HTTP ${error.status})`;
  }
  return error instanceof Error ? error.message : "Une erreur est survenue.";
}

export function ClaimVerification({
  findings,
  observations,
}: {
  findings: InvestigationFinding[];
  observations: InvestigationObservation[];
}) {
  const claims = findings.filter(
    (finding) =>
      finding.kind === "claim_verification" || finding.claim_refs.length > 0,
  );
  const byId = new Map(observations.map((item) => [item.id, item]));
  const verdict: Record<InvestigationFinding["assessment"], string> = {
    supported: "confirmé",
    qualified: "nuancé",
    refuted: "contredit",
    inconclusive: "non établi",
  };

  return (
    <section
      className="job-card"
      aria-labelledby="investigation-claims-heading"
    >
      <h2 id="investigation-claims-heading">
        Vérification des affirmations de l’article
      </h2>
      {claims.length === 0 ? (
        <p>Aucune affirmation de l’article n’a encore été vérifiée.</p>
      ) : (
        <ul>
          {claims.map((finding) => (
            <li key={finding.id}>
              <p>{finding.statement}</p>
              <p>
                <strong>Verdict :</strong> {verdict[finding.assessment]}
              </p>
              {finding.assessment === "supported" ? (
                <EvidenceReferences
                  label="Éléments favorables"
                  refs={finding.observation_refs}
                  byId={byId}
                />
              ) : finding.assessment === "refuted" ? (
                <EvidenceReferences
                  label="Éléments contradictoires"
                  refs={finding.observation_refs}
                  byId={byId}
                />
              ) : finding.assessment === "qualified" ? (
                <EvidenceReferences
                  label="Éléments qui conduisent à nuancer"
                  refs={finding.observation_refs}
                  byId={byId}
                />
              ) : (
                <EvidenceReferences
                  label="Éléments examinés, sans conclusion suffisante"
                  refs={finding.observation_refs}
                  byId={byId}
                />
              )}
              {finding.limitations.map((limitation, index) => (
                <p key={`${finding.id}-claim-limit-${index}`}>
                  <strong>Réserve :</strong> {limitation}
                </p>
              ))}
              {finding.claim_refs.length > 0 ? (
                <p>
                  <strong>Référence d’affirmation :</strong>{" "}
                  {finding.claim_refs.join(", ")}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function InvestigationObservations({
  observations,
}: {
  observations: InvestigationObservation[];
}) {
  return (
    <section
      className="job-card"
      aria-labelledby="investigation-evidence-heading"
    >
      <h2 id="investigation-evidence-heading">Relations et observations</h2>
      {observations.length === 0 ? (
        <p>Aucune observation n’a encore été enregistrée.</p>
      ) : (
        <ul>
          {observations.map((observation) => (
            <li key={observation.id} id={`evidence-${observation.id}`}>
              <p>
                <strong>{observation.kind}</strong> · {observation.source} ·{" "}
                {observation.source_version}
              </p>
              <p>
                Observé le{" "}
                <time dateTime={observation.observed_at}>
                  {observation.observed_at}
                </time>
              </p>
              <p>
                Référence {observation.id} · SHA-256{" "}
                <code>{observation.sha256}</code>
              </p>
              <p>Objet : {observation.object_type}</p>
              {observation.limitations.length > 0 ? (
                <ul>
                  {observation.limitations.map((limitation, index) => (
                    <li key={`${observation.id}-limit-${index}`}>
                      {limitation}
                    </li>
                  ))}
                </ul>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      <p>
        Seules les métadonnées de preuve sont exposées ici ; aucun blob ni
        contenu brut n’est chargé.
      </p>
    </section>
  );
}

export function InvestigationBudgetAndDecisions({
  investigation,
}: {
  investigation: InvestigationView;
}) {
  return (
    <section
      className="job-card"
      aria-labelledby="investigation-budget-heading"
    >
      <h2 id="investigation-budget-heading">Budget et décisions</h2>
      <h3>Budget consommé et restant</h3>
      <dl>
        {BUDGET_ORDER.map((dimension) => {
          const budget = investigation.budget[dimension];
          return (
            <div key={dimension}>
              <dt>{BUDGET_LABELS[dimension]}</dt>
              <dd>
                {budget.consumed} consommé · {budget.reserved} réservé ·{" "}
                {budget.remaining ?? "illimité"} restant · plafond{" "}
                {budget.limit ?? "illimité"}
              </dd>
            </div>
          );
        })}
      </dl>
      <h3>Enveloppe d’investigation</h3>
      <p>
        {investigation.envelope_approved
          ? "Enveloppe approuvée"
          : "Décision requise : enveloppe G2 non approuvée"}
        {investigation.acquire_hits_allowed
          ? " · acquisition de hits autorisée"
          : " · acquisition de hits non autorisée"}
      </p>
      <p>
        Fournisseurs autorisés :{" "}
        {investigation.allowed_providers.join(", ") || "aucun"}
      </p>
      {!investigation.envelope_approved ? (
        <p className="verification-warning" role="note">
          La décision G2 doit préciser les fournisseurs, le budget, le droit
          d’acquisition et les bornes de divulgation. La proposition reste sans
          approbation tant qu’un analyste ne l’a pas décidée.{" "}
          <a href="#investigation-envelope-approve">Approuver l’enveloppe</a>
        </p>
      ) : null}
      <h3>Décisions bloquées</h3>
      {investigation.blocked_decisions.length === 0 ? (
        <p>Aucune décision bloquée.</p>
      ) : (
        <ul>
          {investigation.blocked_decisions.map((decision, index) => (
            <li
              key={`${decision.code}-${decision.tool_name ?? "global"}-${index}`}
            >
              {decisionLabel(decision.code)}
              {decision.tool_name ? ` · outil : ${decision.tool_name}` : ""}
              {decision.code === "permission_required" ||
              decision.code === "policy_denied" ||
              decision.code === "needs_review" ? (
                <>
                  {" · "}
                  <a
                    href={
                      investigation.envelope_approved
                        ? "#investigation-envelope-extend"
                        : "#investigation-envelope-approve"
                    }
                  >
                    {investigation.envelope_approved
                      ? "Étendre l’enveloppe G2"
                      : "Approuver l’enveloppe G2"}
                  </a>
                </>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {investigation.unavailable_tools.length > 0 ? (
        <div role="note">
          <strong>Capacités indisponibles :</strong>
          <ul>
            {investigation.unavailable_tools.map((tool) => (
              <li key={tool}>
                <code>{tool}</code> ·{" "}
                {unavailableToolReason(tool, investigation)}
              </li>
            ))}
          </ul>
          Les autres pistes restent consultables.
        </div>
      ) : null}
      {investigation.available_tools.length > 0 ? (
        <p>Outils disponibles : {investigation.available_tools.join(", ")}</p>
      ) : null}
    </section>
  );
}

function decisionLabel(code: string): string {
  if (code === "permission_required")
    return "Permission requise pour poursuivre";
  if (code === "policy_denied") return "Opération refusée par la politique";
  if (code === "needs_review") return "Opération en attente d’une revue";
  return `Décision bloquée : ${code}`;
}

function unavailableToolReason(
  tool: string,
  investigation: InvestigationView,
): string {
  const reason = investigation.unavailable_tool_reasons[tool];
  const normalized = reason?.toLowerCase() ?? "";
  if (
    normalized.includes("g2") ||
    normalized.includes("disclosure") ||
    normalized.includes("permission_required")
  ) {
    return investigation.envelope_approved
      ? "Divulgation ou autorisation G2 à vérifier."
      : "Approbation de l’enveloppe G2 requise.";
  }
  if (normalized.includes("budget") || normalized.includes("limit")) {
    return "Plafond de budget atteint pour cette capacité.";
  }
  if (
    normalized.includes("operator_mode") ||
    normalized.includes("qualification") ||
    normalized.includes("stage")
  ) {
    return "Étape opérateur ou qualification humaine requise.";
  }
  if (reason)
    return `Dépendance ou worker indisponible : ${reason.replaceAll("_", " ")}.`;

  const dimension = capabilityBudgetDimension(tool);
  if (dimension !== null) {
    const budget = investigation.budget[dimension];
    if (budget.limit !== null && budget.remaining === 0) {
      return `${BUDGET_LABELS[dimension]} : plafond atteint.`;
    }
  }
  if (
    !investigation.envelope_approved &&
    (tool === "advisor.consult" ||
      tool.startsWith("vt.") ||
      tool.startsWith("shodan."))
  ) {
    return "Approbation de l’enveloppe G2 requise.";
  }
  return "Non disponible à cette étape de l’enquête.";
}

function capabilityBudgetDimension(tool: string): BudgetDimension | null {
  if (tool === "advisor.consult") return "bridge_turns";
  if (tool === "vt.download_sample") return "downloads";
  if (tool.startsWith("vt.")) return "vt_units";
  if (tool.startsWith("shodan.")) return "shodan_credits";
  return null;
}

function EvidenceReferences({
  refs,
  byId,
  label = "Preuves",
}: {
  refs: string[];
  byId: Map<string, InvestigationObservation>;
  label?: string;
}) {
  return (
    <p>
      <strong>{label} :</strong>{" "}
      {refs.length === 0
        ? "aucune référence"
        : refs.map((id, index) => {
            const observation = byId.get(id);
            return (
              <span key={id}>
                {index > 0 ? ", " : ""}
                {observation ? (
                  <a href={`#evidence-${id}`}>
                    {observationLabel(id)} · {observation.source} ·{" "}
                    {observation.observed_at}
                  </a>
                ) : (
                  observationLabel(id)
                )}
              </span>
            );
          })}
    </p>
  );
}

export function DetectionReviewPanel({
  investigationId,
}: {
  investigationId: string;
}) {
  const review = useReviewDetection(investigationId);
  const detectionsQuery = useInvestigationDetections(investigationId);

  if (detectionsQuery.isPending)
    return <p role="status">Chargement des règles…</p>;
  if (detectionsQuery.isError) {
    return (
      <p className="error-message" role="alert">
        Impossible de charger les règles : {detectionsQuery.error.message}
      </p>
    );
  }

  return (
    <section
      className="job-card"
      aria-labelledby="investigation-detections-heading"
    >
      <h2 id="investigation-detections-heading">
        Règles et essais facultatifs
      </h2>
      {detectionsQuery.data.versions.length === 0 ? (
        <p>
          Aucune règle proposée. Une enquête utile peut se conclure sans YARA.
        </p>
      ) : (
        <ol>
          {detectionsQuery.data.versions.map((version) => (
            <DetectionVersionCard
              key={version.version_id}
              version={version}
              pending={
                review.isPending &&
                review.variables?.version_id === version.version_id
              }
              error={review.error}
              onReview={(input) => review.mutate(input)}
            />
          ))}
        </ol>
      )}
    </section>
  );
}

function DetectionVersionCard({
  version,
  pending,
  error,
  onReview,
}: {
  version: DetectionVersionReview;
  pending: boolean;
  error: Error | null;
  onReview: (input: {
    version_id: string;
    expected_source_sha256: string;
    reason: string;
    known_limitations?: string;
    action: "approve" | "reject";
  }) => void;
}) {
  const [reason, setReason] = useState("");
  const [limitations, setLimitations] = useState("");
  const reports = version.test_reports;
  const latestReport = reports[reports.length - 1];
  const historicalReports = reports.slice(0, -1);
  return (
    <li>
      <article>
        <h3>
          {version.status === "candidate" || version.status === "experimental"
            ? "Candidate"
            : version.status}{" "}
          · version {version.version}
        </h3>
        <p>
          <strong>Validation du corpus :</strong>{" "}
          {RULE_VALIDATION_STATUS_LABELS[version.validation_status]}
        </p>
        {version.status === "candidate" || version.status === "experimental" ? (
          <p className="verification-warning" role="note">
            Cette règle reste une candidate. Un match est une correspondance
            d’octets et ne confirme ni famille, ni intention, ni comportement.
          </p>
        ) : null}
        <p>
          Candidate {version.candidate_id} · version {version.version_id} ·
          SHA-256 <code>{version.source_sha256}</code>
        </p>
        {version.parent_version_id ? (
          <p>Version précédente : {version.parent_version_id}</p>
        ) : null}
        <details>
          <summary>Afficher la source YARA</summary>
          <pre>{version.source}</pre>
        </details>
        {version.comparisons.length > 0 ? (
          <details>
            <summary>Comparaisons entre versions</summary>
            {version.comparisons.map((comparison, index) => (
              <pre key={`${version.version_id}-comparison-${index}`}>
                {JSON.stringify(comparison, null, 2)}
              </pre>
            ))}
          </details>
        ) : null}
        {latestReport === undefined ? (
          <p>
            Aucun rapport de test valide : la revue de cette règle est
            indisponible.
          </p>
        ) : (
          <DetectionTestReportView
            report={latestReport}
            label="Dernier résultat pertinent"
          />
        )}
        {historicalReports.length > 0 ? (
          <details>
            <summary>Historique des essais ({reports.length})</summary>
            {historicalReports.map((report) => (
              <DetectionTestReportView
                key={report.observation_id}
                report={report}
                label="Essai historique"
              />
            ))}
          </details>
        ) : null}
        {version.latest_decision ? (
          <p>
            Dernière décision : {version.latest_decision.type} par{" "}
            {version.latest_decision.actor} · {version.latest_decision.reason}
          </p>
        ) : null}
        {version.status === "candidate" || version.status === "tested" ? (
          <fieldset>
            <legend>Revue humaine de la version exacte</legend>
            <label>
              Raison
              <textarea
                value={reason}
                onChange={(event) => setReason(event.currentTarget.value)}
              />
            </label>
            <label>
              Limites connues (requises pour approuver)
              <textarea
                value={limitations}
                onChange={(event) => setLimitations(event.currentTarget.value)}
              />
            </label>
            {error && error instanceof Error ? (
              <p className="error-message" role="alert">
                {error.message}
              </p>
            ) : null}
            <button
              type="button"
              className="button button--secondary"
              disabled={
                pending ||
                !reason.trim() ||
                !limitations.trim() ||
                reports.length === 0
              }
              onClick={() =>
                onReview({
                  action: "approve",
                  version_id: version.version_id,
                  expected_source_sha256: version.source_sha256,
                  reason,
                  known_limitations: limitations,
                })
              }
            >
              Approuver cette version
            </button>{" "}
            <button
              type="button"
              className="button button--secondary"
              disabled={pending || !reason.trim()}
              onClick={() =>
                onReview({
                  action: "reject",
                  version_id: version.version_id,
                  expected_source_sha256: version.source_sha256,
                  reason,
                })
              }
            >
              Rejeter cette version
            </button>
          </fieldset>
        ) : null}
      </article>
    </li>
  );
}

function DetectionTestReportView({
  report,
  label,
}: {
  report: DetectionTestReport;
  label: string;
}) {
  return (
    <section aria-label={`${label} ${report.observation_id}`}>
      <h4>
        {label} · {report.execution_status ?? "état inconnu"} · validation :{" "}
        {report.validation_status}
      </h4>
      {report.engine_version ? <p>Moteur : {report.engine_version}</p> : null}
      {report.coverage_statement ? <p>{report.coverage_statement}</p> : null}
      {report.selection_bias ? (
        <p>Biais de sélection : {report.selection_bias}</p>
      ) : null}
      <ul>
        {report.roles.map((roleReport, index) => (
          <li key={`${roleReport.role}-${index}`}>
            <strong>{DETECTION_ROLE_LABELS[roleReport.role]}</strong> ·{" "}
            {roleReport.matches.length} correspondance(s) ·{" "}
            {roleReport.non_matches.length} sans correspondance ·{" "}
            {roleReport.non_measurable.length} non mesurable(s)
            {roleReport.errors.length > 0
              ? ` · ${roleReport.errors.length} erreur(s)`
              : ""}
            {roleReport.provenance_counts
              ? ` · ${roleReport.provenance_counts.real} réel(s), ${roleReport.provenance_counts.synthetic} synthétique(s)`
              : ""}
            {roleReport.test_cases?.length ? (
              <ul>
                {roleReport.test_cases.map((testCase) => (
                  <li key={testCase.sha256}>
                    <code>{testCase.sha256}</code> · {testCase.provenance} ·{" "}
                    {testCase.outcome}
                  </li>
                ))}
              </ul>
            ) : null}
          </li>
        ))}
      </ul>
      {report.corpus_sha256 ? (
        <p>
          Corpus SHA-256 <code>{report.corpus_sha256}</code>
        </p>
      ) : null}
      <dl aria-label="Taux de détection et intervalles de confiance">
        <DetectionRate
          label="Sensibilité sur positifs réels"
          metric={report.metrics?.real_positive_sensitivity}
        />
        <DetectionRate
          label="Faux positifs sur goodware réel"
          metric={report.metrics?.real_goodware_false_positive_rate}
        />
        <DetectionRate
          label="Sensibilité sur holdout réel"
          metric={report.metrics?.real_holdout_sensitivity}
        />
        <DetectionRate
          label="Faux positifs sur négatifs holdout réels"
          metric={report.metrics?.real_holdout_false_positive_rate}
        />
      </dl>
      {report.match_interpretation ? (
        <p role="note">{report.match_interpretation}</p>
      ) : null}
      {report.statistics_interpretation ? (
        <p role="note">{report.statistics_interpretation}</p>
      ) : null}
      {report.known_false_positive_status === "present" ? (
        <p>
          Faux positifs observés :{" "}
          {report.known_false_positives.map((item) => (
            <code key={item}>{item} </code>
          ))}
        </p>
      ) : report.known_false_positive_status === "none_observed" ? (
        <p>Aucun faux positif observé dans les cas goodware mesurés.</p>
      ) : (
        <p>Les faux positifs goodware ne sont pas évalués dans ce rapport.</p>
      )}
      {report.limitations.length > 0 ? (
        <ul>
          {report.limitations.map((limitation, index) => (
            <li key={`${report.observation_id}-limit-${index}`}>
              {limitation}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function DetectionRate({
  label,
  metric,
}: {
  label: string;
  metric: DetectionRateMetric | undefined;
}) {
  if (!metric) return null;
  const {
    numerator,
    denominator,
    estimate,
    confidence_interval_95: interval,
  } = metric;
  const isCalculable =
    metric.status === "calculated" && estimate !== null && interval !== null;
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        {isCalculable
          ? `${(estimate * 100).toFixed(1)} % (95 % Wilson : ${(interval.lower * 100).toFixed(1)}–${(interval.upper * 100).toFixed(1)} %)`
          : "Non calculable"}{" "}
        · {numerator}/{denominator} mesuré(s)
      </dd>
    </div>
  );
}

const ACTIVITY_BUDGET_ORDER: BudgetDimension[] = [
  "bridge_turns",
  "vt_units",
  "downloads",
  "tool_seconds",
  "context_tokens",
  "context_bytes",
  "shodan_credits",
];

const ACTIVITY_BUDGET_LABELS: Record<BudgetDimension, string> = {
  bridge_turns: "Tours Bridge",
  vt_units: "Unités VT",
  shodan_credits: "Crédits Shodan",
  downloads: "Téléchargements",
  tool_seconds: "Secondes d’analyse locale",
  context_tokens: "Tokens de contexte",
  context_bytes: "Octets de contexte",
};

const BLOCKER_LABELS: Record<InvestigationActivityBlockerKind, string> = {
  permission_missing: "Permission manquante",
  service_unavailable: "Service externe indisponible",
  external_effect_ambiguous: "Effet externe ambigu — réconciliation",
  budget_limit: "Limite de budget",
  evidence_insufficient: "Preuve insuffisante",
  analytical_limitation: "Limitation analytique",
  policy_block: "Blocage de politique",
  tool_not_executable: "Outil non exécutable",
  technical_block: "Blocage technique",
};

const ANALYTIC_STAGES = [
  { id: 0, label: "Acquisition / VT initial" },
  { id: 1, label: "Plan Bridge" },
  { id: 2, label: "Analyse statique" },
  { id: 3, label: "Protocole / crypto" },
  { id: 4, label: "YARA Bridge" },
  { id: 5, label: "Validation YARA" },
  { id: 6, label: "Pivots VT" },
  { id: 7, label: "Nouveaux candidats" },
  { id: 8, label: "Boucle de revalidation" },
  { id: 9, label: "Note de l’analyste" },
] as const;

type ActivityStageId = (typeof ANALYTIC_STAGES)[number]["id"];
type ActivityStageStatus =
  | "todo"
  | "in_progress"
  | "partial"
  | "blocked"
  | "complete"
  | "not_applicable"
  | "unknown";

const ACTIVITY_STAGE_STATUS_LABELS: Record<ActivityStageStatus, string> = {
  todo: "À faire",
  in_progress: "En cours",
  partial: "Partielle · clôture inconnue",
  blocked: "Bloquée",
  complete: "Terminée",
  not_applicable: "Non applicable",
  unknown: "Non déterminée",
};

export function InvestigationActivityCockpit({
  investigationId,
  onOpenJournal,
}: {
  investigationId: string;
  onOpenJournal: () => void;
}) {
  const [historyEnabled, setHistoryEnabled] = useState(false);
  const activityQuery = useInvestigationActivity(investigationId);
  const firstPage = activityQuery.data;
  const historyQuery = useInvestigationActivityHistory(
    investigationId,
    firstPage?.next_cursor ?? null,
    historyEnabled,
  );
  const historyPages = historyQuery.data?.pages ?? [];
  const canLoadOlder = historyEnabled
    ? Boolean(historyQuery.hasNextPage)
    : Boolean(firstPage?.has_more);
  const invocations = deduplicateInvocations(
    [
      ...(firstPage ? [firstPage] : []),
      ...historyPages,
    ].flatMap((page) => page.timeline),
  );
  const latestActivityAt = firstPage?.last_activity_at ?? null;
  const stageGroups = new Map<ActivityStageId, InvestigationActivityInvocation[]>();
  const unclassified: InvestigationActivityInvocation[] = [];
  for (const invocation of invocations) {
    const stageId = activityStageFor(invocation);
    if (stageId === null) {
      unclassified.push(invocation);
      continue;
    }
    stageGroups.set(stageId, [...(stageGroups.get(stageId) ?? []), invocation]);
  }
  const activeStageIds = new Set(
    [...stageGroups.entries()]
      .filter(([, stageInvocations]) =>
        stageInvocations.some(
          (invocation) => invocation.status === "pending" || invocation.status === "running",
        ),
      )
      .map(([stageId]) => stageId),
  );
  const activeStageLabels = ANALYTIC_STAGES.filter((stage) =>
    activeStageIds.has(stage.id),
  ).map((stage) => `${stage.id}. ${stage.label}`);

  if (activityQuery.isPending && !firstPage) {
    return <p role="status">Chargement de l’activité persistée…</p>;
  }
  if (!firstPage) {
    return (
      <p className="error-message" role="alert">
        La projection compacte d’activité est indisponible
        {activityQuery.error ? ` : ${activityQuery.error.message}` : "."}
      </p>
    );
  }

  const investigation = firstPage.investigation;
  const budget = firstPage.budget;
  const openHypotheses = firstPage.working_state?.open_hypothesis_count;

  return (
    <div className="investigation-activity">
      <section className="job-card" aria-labelledby="activity-summary-heading">
        <h2 id="activity-summary-heading">Investigation en cours</h2>
        {activityQuery.isError ? (
          <p className="verification-warning" role="note">
            L’actualisation a échoué; les dernières données persistées reçues sont affichées.
          </p>
        ) : null}
        <p>
          {activityStatusLabel(investigation.status, investigation.stop_reason)} ·
          version d’état canonique{" "}
          {firstPage.working_state?.version ?? "inconnue"} · version CAS {investigation.version}
        </p>
        <dl>
          <div>
            <dt>Enquête</dt>
            <dd><code>{investigation.id}</code></dd>
          </div>
          <div>
            <dt>Synthèse</dt>
            <dd>
              <code>{investigation.synthesis_artifact_id}</code> · SHA-256{" "}
              <code>{investigation.synthesis_sha256 ?? "Indisponible"}</code>
            </dd>
          </div>
          <div>
            <dt>Responsable principal</dt>
            <dd>{firstPage.operator.principal ?? "Non renseigné par AutoWork"}</dd>
          </div>
          <div>
            <dt>Dernière activité persistée</dt>
            <dd>
              {latestActivityAt ? formatActivityTime(latestActivityAt) : "Inconnue"}
              {latestActivityAt
                ? ` · ${activitySourceLabel(firstPage.last_activity_source)}`
                : null}
            </dd>
          </div>
          <div>
            <dt>Étape(s) avec invocation en cours</dt>
            <dd>
              {activeStageLabels.length > 0
                ? activeStageLabels.join(" · ")
                : "Aucune invocation active dans les opérations chargées; étape actuelle inconnue"}
            </dd>
          </div>
          <div>
            <dt>Jobs AutoWork actifs</dt>
            <dd>{firstPage.active_backend_job_count}</dd>
          </div>
          <div>
            <dt>Sous-agents actifs</dt>
            <dd>{firstPage.operator.active_subagent_count ?? "Inconnu — non mesurable"}</dd>
          </div>
          <div>
            <dt>Hypothèses ouvertes</dt>
            <dd>{openHypotheses ?? "Inconnu"}</dd>
          </div>
        </dl>
        {!latestActivityAt ? (
          <p className="verification-warning" role="note">
            Aucune activité récente n’est disponible dans cette projection. Cela ne permet pas
            de conclure que l’agent est arrêté.
          </p>
        ) : null}
      </section>

      <section className="job-card" aria-labelledby="activity-steps-heading">
        <h2 id="activity-steps-heading">Progression analytique · étapes 0 à 9</h2>
        <p role="note">
          Les rattachements d’activité reposent sur les noms d’outils et l’objectif déclaré.
          Une invocation réussie ne clôt pas une étape; aucun état de clôture analytique fiable
          n’est persisté pour ces dix étapes. Les compteurs et coûts par étape couvrent les
          opérations chargées; l’historique peut être étendu.
        </p>
        <ol className="activity-stage-list">
          {ANALYTIC_STAGES.map((stage) => {
            const stageInvocations = stageGroups.get(stage.id) ?? [];
            const stageStatus = activityStageStatus(stageInvocations);
            const evidenceRefs = [
              ...new Set(stageInvocations.flatMap((invocation) => invocation.observation_refs)),
            ];
            return (
              <li key={stage.id}>
                <details>
                  <summary>
                    <strong>
                      {stage.id}. {stage.label}
                    </strong>
                    <span>{ACTIVITY_STAGE_STATUS_LABELS[stageStatus]}</span>
                    <span>{stageInvocations.length} activité(s)</span>
                  </summary>
                  <dl>
                    <div><dt>Responsable</dt><dd>Inconnu</dd></div>
                    <div>
                      <dt>Activités chargées</dt>
                      <dd>{stageInvocations.length || "Aucun signal rattaché"}</dd>
                    </div>
                    <div>
                      <dt>Preuves liées</dt>
                      <dd>
                        {evidenceRefs.length > 0 ? (
                          evidenceRefs.map((reference) => (
                            <code key={reference}>{reference} </code>
                          ))
                        ) : (
                          "Aucune référence liée dans les opérations chargées"
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>Actions ouvertes</dt>
                      <dd>
                        {openHypotheses === undefined ? (
                          "Non déterminées"
                        ) : (
                          <>
                            {openHypotheses} hypothèse(s) ouverte(s); rattachement à cette
                            étape inconnu
                          </>
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>Coût des opérations chargées</dt>
                      <dd>{summarizeStageBudget(stageInvocations)}</dd>
                    </div>
                    <div>
                      <dt>Blocages</dt>
                      <dd>{summarizeStageBlockers(stageInvocations)}</dd>
                    </div>
                    {stageInvocations.length > 0 ? (
                      <div>
                        <dt>Résultats observés</dt>
                        <dd>
                          {stageInvocations.map((invocation) => (
                            <span key={invocation.id}>
                              {invocation.tool_name} · {invocation.status}{" "}
                            </span>
                          ))}
                        </dd>
                      </div>
                    ) : null}
                  </dl>
                </details>
              </li>
            );
          })}
        </ol>
        {unclassified.length > 0 ? (
          <p>
            {unclassified.length} activité(s) sans rattachement fiable à une étape.
          </p>
        ) : null}
      </section>

      <section className="job-card" aria-labelledby="activity-timeline-heading">
        <h2 id="activity-timeline-heading">Chronologie des opérations</h2>
        {invocations.length === 0 ? (
          <p>Aucune invocation persistée dans cette page.</p>
        ) : (
          <ol className="activity-timeline">
            {invocations.map((invocation) => (
              <li key={invocation.id}>
                <details>
                  <summary>
                    <strong>{invocation.tool_name}</strong> · {invocation.status} ·{" "}
                    {formatActivityTime(invocation.created_at)}
                  </summary>
                  <p>
                    Agent : non identifié · objectif déclaré :{" "}
                    {invocation.purpose_excerpt || "inconnu"}
                  </p>
                  <p>Durée d’exécution : inconnue. Aucune mesure fiable n’est persistée.</p>
                  <p>
                    Coût/budget consommé : {formatInvocationBudget(invocation.consumed_budget)} ·
                    réservé : {formatInvocationBudget(invocation.reserved_budget)}
                  </p>
                  {invocation.repeated_input_count_in_page > 1 ? (
                    <p role="note">
                      Entrée identique observée {invocation.repeated_input_count_in_page} fois
                      dans cette page; les paramètres restent masqués.
                    </p>
                  ) : null}
                  <p>
                    Invocation canonique <code>{invocation.id}</code> v{invocation.version} ·
                    schéma {invocation.schema_version}
                  </p>
                  {invocation.observations.length > 0 ? (
                    <section aria-label="Observations liées">
                      <h3>Observations produites</h3>
                      <ul>
                        {invocation.observations.map((observation) => (
                          <li key={observation.id}>
                            <code>{observation.id}</code> · {observation.kind} · SHA-256{" "}
                            <code>{observation.sha256}</code> · {observation.source} v
                            {observation.source_version}
                          </li>
                        ))}
                      </ul>
                    </section>
                  ) : invocation.observation_refs.length > 0 ? (
                    <p>
                      Références d’observation canoniques :{" "}
                      {invocation.observation_refs.map((id) => (
                        <code key={id}>{id} </code>
                      ))}
                      Les métadonnées de cette page ne contiennent pas leur résumé.
                    </p>
                  ) : (
                    <p>Aucune observation liée à cette invocation.</p>
                  )}
                </details>
              </li>
            ))}
          </ol>
        )}
        {canLoadOlder ? (
          <button
            type="button"
            className="button button--secondary"
            disabled={historyQuery.isFetchingNextPage || (historyEnabled && historyQuery.isPending)}
            onClick={() => {
              if (!historyEnabled) setHistoryEnabled(true);
              else void historyQuery.fetchNextPage();
            }}
          >
            {historyQuery.isFetchingNextPage || (historyEnabled && historyQuery.isPending)
              ? "Chargement…"
              : "Charger les opérations précédentes"}
          </button>
        ) : null}
        {historyQuery.isError ? (
          <div className="error-message" role="alert">
            Les opérations précédentes n’ont pas pu être chargées.
            <button
              type="button"
              className="button button--secondary"
              onClick={() => void historyQuery.refetch()}
            >
              Réessayer
            </button>
          </div>
        ) : null}
      </section>

      <section className="job-card" aria-labelledby="activity-agents-heading">
        <h2 id="activity-agents-heading">Agents et délégations</h2>
        <p>
          AutoWork ne reçoit pas les événements de sessions Paseo. L’identité de session, le
          modèle, l’effort, la mission et les résultats de délégation sont donc inconnus; les
          métadonnées d’orchestration ne constituent pas des preuves CTI.
        </p>
        <p>Sous-agents actifs : inconnu — non mesurable dans les sources AutoWork disponibles.</p>
        <p>
          Jobs de traitement AutoWork actifs : {firstPage.active_backend_job_count}; ce compteur
          ne mesure pas les sous-agents.
        </p>
      </section>

      <section className="job-card" aria-labelledby="activity-blockers-heading">
        <h2 id="activity-blockers-heading">Blocages et limites</h2>
        {firstPage.blockers.length === 0 ? (
          <p>Aucun blocage typé n’est présent dans les signaux consultés.</p>
        ) : (
          <ul>
            {firstPage.blockers.map((blocker, index) => (
              <li key={`${blocker.code}-${blocker.invocation_id ?? index}`}>
                <strong>{BLOCKER_LABELS[blocker.kind]}</strong> · {blocker.message}
                {blocker.invocation_id ? (
                  <> · invocation <code>{blocker.invocation_id}</code></>
                ) : null}
                {blocker.kind === "external_effect_ambiguous" ? (
                  <p>État externe réel : {blocker.external_effect_state ?? "Inconnu"}.</p>
                ) : null}
                <p>Prochaine action : {blocker.next_action}</p>
                {blocker.kind === "permission_missing" ? (
                  <button
                    type="button"
                    className="button button--secondary"
                    onClick={onOpenJournal}
                  >
                    Ouvrir le journal de revue
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="job-card" aria-labelledby="activity-costs-heading">
        <h2 id="activity-costs-heading">Budgets et répétitions</h2>
        {budget ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Dimension</th>
                  <th>Consommé</th>
                  <th>Réservé</th>
                  <th>Restant</th>
                  <th>Limite</th>
                </tr>
              </thead>
              <tbody>
                {ACTIVITY_BUDGET_ORDER.map((dimension) => {
                  const amount = budget[dimension];
                  return (
                    <tr key={dimension}>
                      <th scope="row">{ACTIVITY_BUDGET_LABELS[dimension]}</th>
                      <td>{amount.consumed}</td>
                      <td>{amount.reserved}</td>
                      <td>{amount.remaining ?? "Inconnu"}</td>
                      <td>{amount.limit ?? "Inconnue"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <p role="note">
            Budget : consommé, réservé et restant inconnus (aucune enveloppe lisible).
          </p>
        )}
        <p>
          Les estimations monétaires ou de coût fournisseur ne sont pas disponibles dans cette
          projection.
        </p>
        <p>
          Les répétitions sont comptées par empreinte interne des paramètres sur la page chargée;
          aucune entrée brute n’est exposée.
        </p>
        <h3>Invocations avec consommation enregistrée</h3>
        {invocations.some((invocation) => hasMeasuredSpend(invocation)) ? (
          <ul>
            {invocations
              .filter((invocation) => hasMeasuredSpend(invocation))
              .map((invocation) => (
                <li key={invocation.id}>
                  {formatActivityTime(invocation.created_at)} · {invocation.tool_name} ·{" "}
                  {formatInvocationBudget(invocation.consumed_budget)}
                  {invocation.repeated_input_count_in_page > 1
                    ? ` · répété ${invocation.repeated_input_count_in_page} fois sur cette page`
                    : ""}
                </li>
              ))}
          </ul>
        ) : (
          <p>Aucun coût individuel mesuré dans les opérations chargées.</p>
        )}
      </section>
    </div>
  );
}

function deduplicateInvocations(
  invocations: InvestigationActivityInvocation[],
): InvestigationActivityInvocation[] {
  const byId = new Map<string, InvestigationActivityInvocation>();
  for (const invocation of invocations) byId.set(invocation.id, invocation);
  return [...byId.values()].sort((left, right) => right.created_at.localeCompare(left.created_at));
}

function activityStageFor(
  invocation: InvestigationActivityInvocation,
): ActivityStageId | null {
  const tool = invocation.tool_name.toLocaleLowerCase("fr-FR");
  const purpose = invocation.purpose_excerpt.toLocaleLowerCase("fr-FR");
  if (tool === "advisor.consult") {
    if (purpose.includes("yara") || purpose.includes("règle")) return 4;
    if (purpose.includes("plan")) return 1;
    if (purpose.includes("revalid")) return 8;
    if (purpose.includes("note") || purpose.includes("avis")) return 9;
    return null;
  }
  if (tool === "vt.file_report") return 0;
  if (tool === "opinion.compose" || tool === "opinion.note" || tool === "opinion.revise") {
    return 9;
  }
  if (
    tool === "vt.relationships" ||
    tool === "vt.intelligence_search" ||
    tool === "vt.object_report"
  ) {
    return 6;
  }
  if (
    tool === "sample.analyze" ||
    tool.startsWith("assembly.") ||
    tool === "types.list" ||
    tool === "methods.list" ||
    tool === "method.decompile" ||
    tool === "strings.list" ||
    tool === "resources.list" ||
    tool === "references.imports"
  ) {
    return 2;
  }
  if (tool.startsWith("network.protocol.") || tool.includes("crypto")) return 3;
  if (tool === "rule.propose_bridge") return 4;
  if (tool.startsWith("rule.")) return 5;
  if (tool.includes("yara") || tool.includes("ruleworkbench")) return 5;
  if (tool === "vt.download_sample" || tool === "sample.set_qualification") return 7;
  if (purpose.includes("revalid")) return 8;
  if (purpose.includes("analyst note") || purpose.includes("note de l’analyste")) return 9;
  return null;
}

function activityStageStatus(
  invocations: InvestigationActivityInvocation[],
): ActivityStageStatus {
  if (invocations.length === 0) return "unknown";
  if (invocations.some((item) => item.status === "pending" || item.status === "running")) {
    return "in_progress";
  }
  const latest = [...invocations].sort((left, right) =>
    right.updated_at.localeCompare(left.updated_at),
  )[0];
  if (
    latest &&
    ["needs_review", "policy_denied", "unavailable", "failed"].includes(latest.status)
  ) {
    return "blocked";
  }
  return "partial";
}

function summarizeStageBudget(invocations: InvestigationActivityInvocation[]): string {
  if (invocations.length === 0) return "Inconnu";
  const consumed = new Map<BudgetDimension, number>();
  const reserved = new Map<BudgetDimension, number>();
  for (const invocation of invocations) {
    for (const [dimension, amount] of Object.entries(invocation.consumed_budget)) {
      if (amount) {
        const key = dimension as BudgetDimension;
        consumed.set(key, (consumed.get(key) ?? 0) + amount);
      }
    }
    for (const [dimension, amount] of Object.entries(invocation.reserved_budget)) {
      if (amount) {
        const key = dimension as BudgetDimension;
        reserved.set(key, (reserved.get(key) ?? 0) + amount);
      }
    }
  }
  return `consommé : ${formatBudgetMap(consumed)} · réservé : ${formatBudgetMap(reserved)}`;
}

function summarizeStageBlockers(invocations: InvestigationActivityInvocation[]): string {
  const ambiguous = invocations.filter((item) => item.status === "needs_review");
  if (ambiguous.length > 0) {
    return ambiguous.map((item) => `${item.tool_name} · needs_review`).join("; ");
  }
  const latest = [...invocations].sort((left, right) =>
    right.updated_at.localeCompare(left.updated_at),
  )[0];
  if (
    latest &&
    ["policy_denied", "unavailable", "failed"].includes(latest.status)
  ) {
    return `Dernière invocation : ${latest.tool_name} · ${latest.status}; motif détaillé non disponible`;
  }
  return "Aucun blocage opérationnel explicite";
}

function formatInvocationBudget(values: Partial<Record<BudgetDimension, number>>): string {
  const valuesByDimension = new Map<BudgetDimension, number>();
  for (const dimension of ACTIVITY_BUDGET_ORDER) {
    const value = values[dimension];
    if (value) valuesByDimension.set(dimension, value);
  }
  return formatBudgetMap(valuesByDimension);
}

function hasMeasuredSpend(invocation: InvestigationActivityInvocation): boolean {
  return Object.values(invocation.consumed_budget).some(
    (amount) => typeof amount === "number" && amount > 0,
  );
}

function formatBudgetMap(values: Map<BudgetDimension, number>): string {
  const parts = ACTIVITY_BUDGET_ORDER.flatMap((dimension) => {
    const value = values.get(dimension);
    return value ? [`${ACTIVITY_BUDGET_LABELS[dimension]} : ${value}`] : [];
  });
  return parts.length > 0 ? parts.join(" · ") : "0 mesuré";
}

function formatActivityTime(value: string): string {
  return new Intl.DateTimeFormat("fr-FR", {
    dateStyle: "short",
    timeStyle: "medium",
  }).format(new Date(value));
}

function activitySourceLabel(source: string | null): string {
  if (source === "investigation_record") return "mise à jour du dossier AutoWork";
  if (source === "invocation") return "invocation persistée";
  if (source === "observation") return "observation persistée";
  if (source === "backend_job_heartbeat") return "heartbeat du job AutoWork";
  return "source inconnue";
}

function activityStatusLabel(
  status: InvestigationActivityPage["investigation"]["status"],
  stopReason: InvestigationActivityPage["investigation"]["stop_reason"],
): string {
  if (stopReason === "permission_required") return "Permission requise";
  if (stopReason === "model_unavailable") return "Service modèle indisponible";
  if (status === "queued") return "En attente";
  if (status === "running") return "En cours";
  if (status === "awaiting_review") return "À réviser";
  if (status === "completed") return "Terminée";
  if (status === "exhausted") return "Budget atteint";
  if (status === "failed") return "Interrompue";
  return "Annulée";
}
