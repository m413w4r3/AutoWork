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
                {budget.consumed} consommé ·{" "}
                {budget.remaining ?? "sans plafond"} restant · plafond{" "}
                {budget.limit ?? "non défini"}
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
          <strong>Capacités indisponibles, limites locales :</strong>{" "}
          {investigation.unavailable_tools.join(", ")}. Les autres pistes
          restent consultables.
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
  return (
    <li>
      <article>
        <h3>
          Version {version.version} · {version.status}
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
        {reports.length === 0 ? (
          <p>
            Aucun rapport de test valide : la revue de cette règle est
            indisponible.
          </p>
        ) : (
          reports.map((report) => (
            <DetectionTestReportView
              key={report.observation_id}
              report={report}
            />
          ))
        )}
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

function DetectionTestReportView({ report }: { report: DetectionTestReport }) {
  return (
    <section aria-label={`Rapport de test ${report.observation_id}`}>
      <h4>
        Essais · {report.execution_status ?? "état inconnu"} · validation :{" "}
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
