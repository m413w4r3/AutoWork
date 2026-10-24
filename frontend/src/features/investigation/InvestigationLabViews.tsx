import { useState } from "react";

import {
  useInvestigationExperiments,
  type ConsolidatedClaimVerdict,
  type InvestigationExperiment,
} from "../../api/investigationExperiments";
import {
  useInvestigationPivotFrontier,
  type PivotFrontierEdge,
  type PivotFrontierNode,
  type PivotFrontierPath,
} from "../../api/investigationPivots";
import {
  useArticleClaimSources,
  type ArticleClaimSource,
} from "../../api/investigationOperator";
import type {
  InvestigationFinding,
  InvestigationView,
} from "../../api/investigation";
import { useInvestigationDetections } from "../../api/detection";
import { AnalystOpinionEditor } from "./AnalystOpinionEditor";
import { InvestigationGovernancePanel } from "./InvestigationGovernancePanel";
import {
  ClaimVerification,
  DetectionReviewPanel,
  InvestigationBudgetAndDecisions,
  InvestigationFindings,
  InvestigationObservations,
  InvestigationSamples,
} from "./InvestigationPanels";
import { InvestigationReadPanels } from "./InvestigationReadPanels";
import { PublicationLineagePanel } from "./PublicationLineagePanel";

type LabView =
  "claims" | "pivots" | "samples" | "network" | "journal" | "publication";

const VIEWS: Array<{ id: LabView; label: string }> = [
  { id: "claims", label: "Claims / Hypothèses" },
  { id: "pivots", label: "Pivots" },
  { id: "samples", label: "Échantillons" },
  { id: "network", label: "Réseau & détections" },
  { id: "journal", label: "Journal & revue" },
  { id: "publication", label: "PDF & lignage" },
];

const VERDICT_LABELS: Record<string, string> = {
  supports: "Soutient",
  refutes: "Réfute",
  qualifies: "Nuance",
  inconclusive: "Non concluant",
  blocked: "Bloqué",
};

const CLAIM_STATUS_LABELS: Record<string, string> = {
  supported: "Soutenu",
  refuted: "Réfuté",
  qualified: "Nuancé",
  inconclusive: "Non concluant",
};

const PIVOT_LEVEL_LABELS: Record<string, string> = {
  documented: "Documenté dans l’article",
  discovered: "Découvert pendant l’enquête",
  candidate: "Candidat non confirmé",
  associated: "Associé",
  rejected: "Rejeté",
  confirmed: "IOC confirmé",
};

const PIVOT_STATE_LABELS: Record<string, string> = {
  queued: "En attente",
  executed: "Exécuté",
  exhausted: "Épuisé",
  blocked: "Bloqué",
};

const G2_LABELS: Record<string, string> = {
  required: "Approbation G2 requise",
  approved: "Approuvé en G2",
  blocked: "Bloqué en G2",
  not_applicable: "G2 non applicable",
};

const OUTCOME_LABELS: Record<string, string> = {
  observed: "Observé",
  not_observed: "Non observé",
  unknown: "Inconnu",
};

const COVERAGE_LABELS: Record<string, string> = {
  complete: "complète",
  limited: "limitée",
  none: "absente",
};

const AVAILABILITY_LABELS: Record<string, string> = {
  available: "disponible",
  limited: "limitée",
  unavailable: "indisponible",
};

const DISCLOSURE_LABELS: Record<string, string> = {
  allowed: "Divulgation autorisée",
  blocked: "Divulgation bloquée",
  unknown: "Divulgation inconnue",
  not_applicable: "Divulgation non applicable",
};

export function InvestigationLabViews({
  investigation,
  editionId,
  showG2Prompt = false,
}: {
  investigation: InvestigationView;
  editionId: string;
  showG2Prompt?: boolean;
}) {
  const [activeView, setActiveView] = useState<LabView>("claims");
  const [selectedClaimId, setSelectedClaimId] = useState("");
  const panelId = "investigation-lab-view";

  return (
    <section className="job-card" aria-label="Laboratoire de preuve">
      {showG2Prompt ? (
        <p className="verification-warning" role="note">
          Permission requise pour poursuivre. Consultez et décidez l’enveloppe
          G2 ici:{" "}
          <button
            type="button"
            className="button button--secondary"
            onClick={() => setActiveView("journal")}
          >
            Ouvrir l’onglet Journal & revue
          </button>
          .
        </p>
      ) : null}
      <div role="group" aria-label="Vues du laboratoire de preuve">
        {VIEWS.map((view) => (
          <button
            key={view.id}
            type="button"
            className="button button--secondary"
            aria-pressed={activeView === view.id}
            aria-controls={panelId}
            onClick={() => setActiveView(view.id)}
          >
            {view.label}
          </button>
        ))}
      </div>
      <section
        id={panelId}
        role="region"
        aria-label={VIEWS.find((view) => view.id === activeView)?.label}
      >
        {activeView === "claims" ? (
          <ClaimsView
            investigation={investigation}
            selectedClaimId={selectedClaimId}
            onSelectClaim={setSelectedClaimId}
            onOpenPivots={() => setActiveView("pivots")}
            onOpenJournal={() => setActiveView("journal")}
          />
        ) : null}
        {activeView === "pivots" ? (
          <PivotFrontierPanel investigationId={investigation.id} />
        ) : null}
        {activeView === "samples" ? (
          <SamplesView investigation={investigation} />
        ) : null}
        {activeView === "network" ? (
          <NetworkAndDetectionsView investigationId={investigation.id} />
        ) : null}
        {activeView === "journal" ? (
          <JournalView
            investigation={investigation}
            editionId={editionId}
            selectedClaimId={selectedClaimId}
            onSelectClaim={setSelectedClaimId}
          />
        ) : null}
        {activeView === "publication" ? (
          <PublicationLineagePanel
            investigationId={investigation.id}
            subjectId={investigation.subject_id}
          />
        ) : null}
      </section>
    </section>
  );
}

function ClaimsView({
  investigation,
  selectedClaimId,
  onSelectClaim,
  onOpenPivots,
  onOpenJournal,
}: {
  investigation: InvestigationView;
  selectedClaimId: string;
  onSelectClaim: (claimId: string) => void;
  onOpenPivots: () => void;
  onOpenJournal: () => void;
}) {
  const articlesQuery = useArticleClaimSources(investigation.id);
  const experimentsQuery = useInvestigationExperiments(investigation.id);
  const articleClaims =
    articlesQuery.data?.pages.flatMap((page) => page.items) ?? [];
  const experimentClaims = experimentsQuery.data?.claims ?? [];
  const experiments = experimentsQuery.data?.experiments ?? [];
  const options = makeClaimOptions(
    articleClaims,
    experimentClaims,
    experiments,
  );
  const claimIds = selectedClaimId
    ? [selectedClaimId]
    : options.map((option) => option.id);

  return (
    <div>
      <section className="job-card" aria-labelledby="claim-source-heading">
        <h2 id="claim-source-heading">Affirmations de l’article</h2>
        {articlesQuery.isPending ? (
          <p role="status">Chargement des affirmations source…</p>
        ) : articlesQuery.isError ? (
          <p className="verification-warning" role="note">
            Le texte source paginé est indisponible :{" "}
            {articlesQuery.error.message}. Les identifiants et verdicts
            d’expérience restent consultables.
          </p>
        ) : articleClaims.length === 0 ? (
          <p>Aucune affirmation source n’est exposée pour cette enquête.</p>
        ) : (
          <ul>
            {articleClaims.map((claim) => (
              <li key={claim.id}>
                <p>
                  <strong>{claim.statement}</strong>
                </p>
                <p>
                  Identifiant <code>{claim.id}</code> · références source :{" "}
                  {claim.source_refs.length > 0
                    ? claim.source_refs.map((reference) => (
                        <code key={reference}>{reference} </code>
                      ))
                    : "aucune référence liée"}
                </p>
              </li>
            ))}
          </ul>
        )}
        {articlesQuery.hasNextPage ? (
          <button
            type="button"
            className="button button--secondary"
            disabled={articlesQuery.isFetchingNextPage}
            onClick={() => void articlesQuery.fetchNextPage()}
          >
            {articlesQuery.isFetchingNextPage
              ? "Chargement…"
              : "Charger les affirmations suivantes"}
          </button>
        ) : null}
      </section>

      <section className="job-card" aria-labelledby="experiment-claims-heading">
        <h2 id="experiment-claims-heading">
          Hypothèses et cahier d’expériences
        </h2>
        {options.length > 0 ? (
          <label htmlFor="experiment-claim-filter">
            Filtrer par affirmation
            <select
              id="experiment-claim-filter"
              value={selectedClaimId}
              onChange={(event) => onSelectClaim(event.currentTarget.value)}
            >
              <option value="">Toutes les affirmations</option>
              {options.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {experimentsQuery.isPending ? (
          <p role="status">Chargement de la chronologie append-only…</p>
        ) : experimentsQuery.isError ? (
          <p className="error-message" role="alert">
            La chronologie d’expériences est indisponible :{" "}
            {experimentsQuery.error.message}
          </p>
        ) : claimIds.length === 0 ? (
          <p>Aucune affirmation ou expérience n’est enregistrée.</p>
        ) : (
          <ul>
            {claimIds.map((claimId) => (
              <ClaimExperimentCard
                key={claimId}
                claimId={claimId}
                sourceClaim={articleClaims.find(
                  (claim) => claim.id === claimId,
                )}
                summary={experimentClaims.find(
                  (claim) => claim.claim_id === claimId,
                )}
                experiments={experiments.filter((item) =>
                  item.claim_ids.includes(claimId),
                )}
                hypothesis={investigation.hypotheses.find((item) =>
                  experiments.some(
                    (experiment) =>
                      experiment.claim_ids.includes(claimId) &&
                      experiment.hypothesis_id === item.id,
                  ),
                )}
                onOpenPivots={onOpenPivots}
                onOpenJournal={onOpenJournal}
              />
            ))}
          </ul>
        )}
        <InvestigationReadPanels
          investigationId={investigation.id}
          view="claims"
        />
        <ClaimVerification
          findings={investigation.findings}
          observations={investigation.observations}
        />
        <InvestigationFindings
          findings={
            selectedClaimId
              ? investigation.findings.filter((finding) =>
                  finding.claim_refs.includes(selectedClaimId),
                )
              : investigation.findings
          }
          observations={investigation.observations}
        />
      </section>
    </div>
  );
}

function ClaimExperimentCard({
  claimId,
  sourceClaim,
  summary,
  experiments,
  hypothesis,
  onOpenPivots,
  onOpenJournal,
}: {
  claimId: string;
  sourceClaim: ArticleClaimSource | undefined;
  summary: ConsolidatedClaimVerdict | undefined;
  experiments: InvestigationExperiment[];
  hypothesis: InvestigationView["hypotheses"][number] | undefined;
  onOpenPivots: () => void;
  onOpenJournal: () => void;
}) {
  const hasOpenUnknowns = experiments.some(
    (experiment) =>
      experiment.actual_outcome === "unknown" ||
      experiment.observation_coverage !== "complete" ||
      experiment.availability !== "available" ||
      experiment.verdict === "inconclusive" ||
      experiment.verdict === "blocked",
  );

  return (
    <li>
      <article className="job-card">
        <h3>{sourceClaim?.statement ?? `Affirmation ${claimId}`}</h3>
        <p>
          Identifiant source : <code>{claimId}</code> · statut :{" "}
          {summary
            ? (CLAIM_STATUS_LABELS[summary.status] ?? summary.status)
            : "non testé"}
        </p>
        {sourceClaim && sourceClaim.source_refs.length > 0 ? (
          <p>
            Bibliographie / références :{" "}
            {sourceClaim.source_refs.map((reference) => (
              <code key={reference}>{reference} </code>
            ))}
          </p>
        ) : null}
        {hypothesis ? (
          <p>
            Hypothèse liée : {hypothesis.statement} · état {hypothesis.status}
          </p>
        ) : null}
        {summary?.contradictory ? (
          <p className="verification-warning" role="note">
            Des expériences soutiennent et réfutent cette affirmation. Les deux
            résultats restent visibles; le statut consolidé est « nuancé ».
          </p>
        ) : null}
        {!summary && experiments.length === 0 ? (
          <p>Aucune expérience enregistrée : affirmation non testée.</p>
        ) : null}
        {hasOpenUnknowns ? (
          <p role="note">
            Inconnues restantes : visibilité, disponibilité ou verdict non
            concluant indiqué dans les expériences ci-dessous.
          </p>
        ) : null}
        {experiments.length > 0 ? (
          <ol>
            {experiments.map((experiment) => (
              <ExperimentDetail
                key={experiment.id}
                experiment={experiment}
                onOpenPivots={onOpenPivots}
                onOpenJournal={onOpenJournal}
              />
            ))}
          </ol>
        ) : null}
      </article>
    </li>
  );
}

function ExperimentDetail({
  experiment,
  onOpenPivots,
  onOpenJournal,
}: {
  experiment: InvestigationExperiment;
  onOpenPivots: () => void;
  onOpenJournal: () => void;
}) {
  return (
    <li>
      <details open>
        <summary>
          Expérience v{experiment.version} ·{" "}
          {VERDICT_LABELS[experiment.verdict]} · consignée le{" "}
          <time dateTime={experiment.recorded_at}>
            {experiment.recorded_at}
          </time>
        </summary>
        {experiment.verdict_assertion_label ? (
          <p className="verification-warning" role="note">
            {experiment.verdict_assertion_label}
          </p>
        ) : null}
        {experiment.superseded_by?.length ? (
          <p className="verification-warning" role="note">
            Remplacée par :{" "}
            {experiment.superseded_by.map((experimentId, index) => (
              <span key={experimentId}>
                {index > 0 ? ", " : ""}
                <code>{experimentId}</code>
              </span>
            ))}
          </p>
        ) : null}
        <dl>
          <dt>Protocole / méthode</dt>
          <dd>
            {experiment.method} · mode {experiment.method_mode}
          </dd>
          <dt>Prédiction</dt>
          <dd>{experiment.prediction}</dd>
          <dt>Critère de réfutation</dt>
          <dd>{experiment.falsification_criterion}</dd>
          <dt>Entrées vérifiées</dt>
          <dd>
            {experiment.input_refs.map((input) => (
              <p key={`${input.input_type}-${input.input_id}`}>
                {input.input_type} · {input.input_id} · SHA-256{" "}
                {input.sha256 ? <code>{input.sha256}</code> : "non fourni"}
              </p>
            ))}
          </dd>
          <dt>Outil et versions</dt>
          <dd>
            {experiment.tool_name} {experiment.tool_version} · schéma{" "}
            {experiment.tool_schema_version} · disponibilité{" "}
            {AVAILABILITY_LABELS[experiment.availability]}
          </dd>
          <dt>Paramètres filtrés</dt>
          <dd>
            <pre>{JSON.stringify(experiment.tool_arguments, null, 2)}</pre>
          </dd>
          {experiment.preconditions.length > 0 ? (
            <>
              <dt>Préconditions</dt>
              <dd>{experiment.preconditions.join(" · ")}</dd>
            </>
          ) : null}
          <dt>Résultat attendu</dt>
          <dd>{experiment.expected}</dd>
          <dt>Résultat observé</dt>
          <dd>{experiment.actual}</dd>
          <dt>Couverture observée</dt>
          <dd>
            {OUTCOME_LABELS[experiment.actual_outcome]} · couverture{" "}
            {COVERAGE_LABELS[experiment.observation_coverage]}
          </dd>
          <dt>Portée</dt>
          <dd>{experiment.scope || "Non précisée"}</dd>
          <dt>Incidence sur l’affirmation</dt>
          <dd>{experiment.implication || "Non précisée"}</dd>
          <dt>Origine et version source</dt>
          <dd>
            {experiment.origin} · {experiment.author} · état enquête v
            {experiment.working_state_version} · enveloppe G2 v
            {experiment.g2_envelope_version}
          </dd>
          {experiment.tool_invocation_refs.length > 0 ? (
            <>
              <dt>Références d’invocation</dt>
              <dd>
                {experiment.tool_invocation_refs.map((item) => (
                  <span key={item.invocation_id}>
                    {item.invocation_id} v{item.version};{" "}
                  </span>
                ))}
                <span>Une invocation seule ne constitue pas le verdict.</span>
              </dd>
            </>
          ) : null}
          <dt>Observations persistées</dt>
          <dd>
            {experiment.observation_refs.length === 0
              ? "Aucune observation liée"
              : experiment.observation_refs.map((observation) => (
                  <button
                    key={observation.observation_id}
                    type="button"
                    className="button button--secondary"
                    onClick={onOpenJournal}
                  >
                    Observation {observation.observation_id.slice(0, 8)} ·
                    SHA-256 {observation.sha256.slice(0, 16)}…
                  </button>
                ))}
          </dd>
          {experiment.limitations.length > 0 ? (
            <>
              <dt>Limites</dt>
              <dd>
                <ul>
                  {experiment.limitations.map((limitation, index) => (
                    <li key={`${experiment.id}-limitation-${index}`}>
                      {limitation}
                    </li>
                  ))}
                </ul>
              </dd>
            </>
          ) : null}
          {experiment.next_pivots.length > 0 ? (
            <>
              <dt>Nouvelles pistes</dt>
              <dd>
                {experiment.next_pivots.map((pivotId) => (
                  <button
                    key={pivotId}
                    type="button"
                    className="button button--secondary"
                    onClick={onOpenPivots}
                  >
                    Ouvrir le pivot <code>{pivotId.slice(0, 12)}</code>
                  </button>
                ))}
              </dd>
            </>
          ) : null}
        </dl>
      </details>
    </li>
  );
}

function PivotFrontierPanel({ investigationId }: { investigationId: string }) {
  const query = useInvestigationPivotFrontier(investigationId);
  const pages = query.data?.pages ?? [];
  const nodes = pages.flatMap((page) => page.nodes);
  const edges = pages.flatMap((page) => page.edges);
  const paths = pages.flatMap((page) => page.paths);
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const edgeById = new Map(edges.map((edge) => [edge.id, edge]));

  return (
    <section className="job-card" aria-labelledby="pivot-frontier-heading">
      <h2 id="pivot-frontier-heading">Frontière de pivots</h2>
      {query.isPending ? (
        <p role="status">Chargement des chemins de pivots…</p>
      ) : query.isError ? (
        <p className="error-message" role="alert">
          Les pivots sont indisponibles : {query.error.message}
        </p>
      ) : nodes.length === 0 ? (
        <p>Aucun pivot n’est enregistré pour cette enquête.</p>
      ) : (
        <>
          <p>
            Frontier SHA-256 <code>{pages[0]?.frontier_sha256}</code> · source
            article SHA-256 <code>{pages[0]?.source_sha256}</code>
          </p>
          <section aria-labelledby="pivot-paths-heading">
            <h3 id="pivot-paths-heading">
              Chemins hiérarchiques et provenance
            </h3>
            {paths.length === 0 ? (
              <p>
                Aucun chemin complet n’est disponible dans les pages chargées.
              </p>
            ) : (
              <ol>
                {paths.map((path) => (
                  <PivotPathView
                    key={path.id}
                    path={path}
                    nodeById={nodeById}
                    edgeById={edgeById}
                  />
                ))}
              </ol>
            )}
          </section>
          <section aria-labelledby="pivot-candidates-heading">
            <h3 id="pivot-candidates-heading">Candidats non confirmés</h3>
            <p>
              Un candidat reste une piste. Il n’est pas un positif de famille
              confirmé.
            </p>
            <NodeList
              nodes={nodes.filter((node) => node.level === "candidate")}
            />
          </section>
          <section aria-labelledby="pivot-confirmed-heading">
            <h3 id="pivot-confirmed-heading">Pivots associés ou confirmés</h3>
            <NodeList
              nodes={nodes.filter(
                (node) =>
                  node.level === "associated" || node.level === "confirmed",
              )}
            />
          </section>
          <section aria-labelledby="pivot-other-heading">
            <h3 id="pivot-other-heading">
              Sources, découvertes et autres statuts
            </h3>
            <NodeList
              nodes={nodes.filter(
                (node) =>
                  node.level !== "candidate" &&
                  node.level !== "associated" &&
                  node.level !== "confirmed",
              )}
            />
          </section>
          {query.hasNextPage ? (
            <button
              type="button"
              className="button button--secondary"
              disabled={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
            >
              {query.isFetchingNextPage
                ? "Chargement…"
                : "Charger les pivots suivants"}
            </button>
          ) : null}
        </>
      )}
    </section>
  );
}

function PivotPathView({
  path,
  nodeById,
  edgeById,
}: {
  path: PivotFrontierPath;
  nodeById: Map<string, PivotFrontierNode>;
  edgeById: Map<string, PivotFrontierEdge>;
}) {
  return (
    <li>
      <details open>
        <summary>
          Chemin vers <code>{path.target_id.slice(0, 12)}</code>
        </summary>
        <ol>
          {path.node_ids.map((nodeId, index) => {
            const node = nodeById.get(nodeId);
            const edge = edgeById.get(path.edge_ids[index - 1] ?? "");
            return (
              <li key={`${path.id}-${nodeId}`}>
                {edge ? (
                  <p>
                    Relation « {edge.relation} » · {edge.reason} · source{" "}
                    {edge.source_version} · SHA-256{" "}
                    <code>{edge.source_sha256}</code>
                  </p>
                ) : null}
                {node ? (
                  <p>
                    {PIVOT_LEVEL_LABELS[node.level]} · {node.indicator_type} ·{" "}
                    <code>{node.value}</code> · source {node.source} v
                    {node.source_version} · {node.reason}
                  </p>
                ) : (
                  <p>
                    Nœud absent des pages chargées : <code>{nodeId}</code>
                  </p>
                )}
              </li>
            );
          })}
        </ol>
      </details>
    </li>
  );
}

function NodeList({ nodes }: { nodes: PivotFrontierNode[] }) {
  if (nodes.length === 0) return <p>Aucun pivot dans cette catégorie.</p>;
  return (
    <ul>
      {nodes.map((node) => (
        <li key={node.id}>
          <PivotNodeCard node={node} />
        </li>
      ))}
    </ul>
  );
}

function PivotNodeCard({ node }: { node: PivotFrontierNode }) {
  return (
    <article>
      <h4>
        {node.indicator_type} · <code>{node.value}</code>
      </h4>
      <p>
        {PIVOT_LEVEL_LABELS[node.level]} · {PIVOT_STATE_LABELS[node.state]} ·{" "}
        priorité {node.priority_score}/100 · coût estimé {node.estimated_cost}
      </p>
      <p>
        Pourquoi ce candidat ? {node.reason} · confiance{" "}
        {Math.round(node.confidence * 100)} %
      </p>
      <p>
        Provenance : {node.source} v{node.source_version} ·{" "}
        {node.novelty_vs_article ? "nouveau vs article" : "déjà dans l’article"}{" "}
        · {node.novelty_vs_verified ? "nouveau vs vérifié" : "déjà vérifié"}
      </p>
      <p>
        Source SHA-256 <code>{node.source_sha256}</code>
        {node.source_observation_id ? (
          <>
            {" "}
            · observation <code>{node.source_observation_id}</code>
          </>
        ) : null}
      </p>
      <p>
        {G2_LABELS[node.g2_state]} · {DISCLOSURE_LABELS[node.disclosure_state]}
      </p>
      {node.state === "blocked" || node.state === "exhausted" ? (
        <p className="verification-warning" role="note">
          Blocage / limite : {node.state_reason}
        </p>
      ) : (
        <p>État : {node.state_reason}</p>
      )}
      <dl>
        <dt>Facteurs de priorité</dt>
        <dd>
          Nouveauté article {node.priority_factors.novelty_vs_article} ·{" "}
          nouveauté vérifiée {node.priority_factors.novelty_vs_verified} ·{" "}
          profondeur {node.priority_factors.depth} · coût{" "}
          {node.priority_factors.cost}
        </dd>
      </dl>
      {node.limitations.length > 0 ? (
        <ul>
          {node.limitations.map((limitation, index) => (
            <li key={`${node.id}-limitation-${index}`}>{limitation}</li>
          ))}
        </ul>
      ) : null}
    </article>
  );
}

function SamplesView({ investigation }: { investigation: InvestigationView }) {
  return (
    <div>
      <p role="note">
        L’API expose les hashes, qualifications, provenance, historique humain
        et comparaisons de traits. Le type natif/.NET, la famille supposée par
        échantillon et la liste des moteurs exécutés ne sont pas présents dans
        ces vues.
      </p>
      <InvestigationSamples
        investigationId={investigation.id}
        observations={investigation.observations}
      />
      <InvestigationReadPanels
        investigationId={investigation.id}
        view="samples"
      />
    </div>
  );
}

function NetworkAndDetectionsView({
  investigationId,
}: {
  investigationId: string;
}) {
  const detectionQuery = useInvestigationDetections(investigationId);
  return (
    <div>
      <p role="note">
        Les modèles de protocole exposent champs, offsets, références de
        capture, niveaux de preuve et essais train/holdout quand ils existent.
        L’API d’import PCAP n’a pas de lecture/liste des captures ni de décodage
        de paquets pour l’interface.
      </p>
      <InvestigationReadPanels
        investigationId={investigationId}
        view="network"
      />
      {detectionQuery.data?.versions.some((version) =>
        version.test_reports.some((report) => {
          const positives = report.roles.find(
            (role) => role.role === "confirmed_positive",
          );
          const measured = new Set([
            ...(positives?.matches ?? []),
            ...(positives?.non_matches ?? []),
          ]);
          return !positives?.test_cases?.some(
            (testCase) =>
              testCase.provenance === "real" && measured.has(testCase.sha256),
          );
        }),
      ) ? (
        <p className="verification-warning" role="note">
          Au moins un rapport ne contient aucun positif confirmé réel mesuré; sa
          sensibilité n’est pas calculable. Un match ne confirme pas une
          famille.
        </p>
      ) : null}
      <DetectionReviewPanel investigationId={investigationId} />
    </div>
  );
}

function JournalView({
  investigation,
  editionId,
  selectedClaimId,
  onSelectClaim,
}: {
  investigation: InvestigationView;
  editionId: string;
  selectedClaimId: string;
  onSelectClaim: (claimId: string) => void;
}) {
  const articlesQuery = useArticleClaimSources(investigation.id);
  const experimentsQuery = useInvestigationExperiments(investigation.id);
  const articleClaims =
    articlesQuery.data?.pages.flatMap((page) => page.items) ?? [];
  const experimentClaims = experimentsQuery.data?.claims ?? [];
  const experiments = experimentsQuery.data?.experiments ?? [];
  const options = makeClaimOptions(
    articleClaims,
    experimentClaims,
    experiments,
  );
  const linkedObservationIds = selectedClaimId
    ? new Set(
        experiments
          .filter((experiment) =>
            experiment.claim_ids.includes(selectedClaimId),
          )
          .flatMap((experiment) =>
            experiment.observation_refs.map(
              (reference) => reference.observation_id,
            ),
          ),
      )
    : null;
  const visibleObservations = linkedObservationIds
    ? investigation.observations.filter((observation) =>
        linkedObservationIds.has(observation.id),
      )
    : investigation.observations;

  return (
    <div>
      <section
        className="job-card"
        aria-labelledby="investigation-journal-heading"
      >
        <h2 id="investigation-journal-heading">
          Versions et dernières décisions
        </h2>
        <p>
          Enquête version CAS {investigation.version} · état{" "}
          {investigation.status}
          {investigation.opinion_draft
            ? ` · avis ${investigation.opinion_draft.status} v${investigation.opinion_draft.version}`
            : " · aucun avis enregistré"}
        </p>
        {investigation.findings.length > 0 ? (
          <ol>
            {investigation.findings.map((finding) => (
              <FindingRevisionSummary key={finding.id} finding={finding} />
            ))}
          </ol>
        ) : (
          <p>Aucun finding versionné n’est enregistré.</p>
        )}
        <p className="verification-warning" role="note">
          L’historique complet des événements de décision G2/G3 n’a pas
          d’endpoint de lecture dédié. L’état courant de l’enveloppe G2 et les
          historiques de qualification des échantillons restent affichés dans
          leurs panneaux respectifs.
        </p>
      </section>
      {options.length > 0 ? (
        <label htmlFor="journal-claim-filter">
          Filtrer le journal par affirmation
          <select
            id="journal-claim-filter"
            value={selectedClaimId}
            onChange={(event) => onSelectClaim(event.currentTarget.value)}
          >
            <option value="">Toutes les affirmations</option>
            {options.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      {selectedClaimId && experimentsQuery.isError ? (
        <p className="error-message" role="alert">
          Impossible de filtrer les observations par affirmation :{" "}
          {experimentsQuery.error.message}
        </p>
      ) : null}
      {selectedClaimId && linkedObservationIds?.size === 0 ? (
        <p>
          Aucune observation n’est liée à cette affirmation par une expérience.
        </p>
      ) : null}
      <InvestigationObservations observations={visibleObservations} />
      <InvestigationFindings
        findings={investigation.findings}
        observations={investigation.observations}
      />
      <InvestigationBudgetAndDecisions investigation={investigation} />
      <InvestigationGovernancePanel investigation={investigation} />
      <InvestigationReadPanels
        investigationId={investigation.id}
        view="journal"
      />
      <AnalystOpinionEditor
        investigationId={investigation.id}
        currentDraft={investigation.opinion_draft}
        compositionHref={`/editions/${encodeURIComponent(editionId)}/composition`}
      />
    </div>
  );
}

function FindingRevisionSummary({
  finding,
}: {
  finding: InvestigationFinding;
}) {
  return (
    <li>
      <p>
        {finding.statement} · {finding.assessment} · version {finding.version} ·{" "}
        <time dateTime={finding.created_at}>{finding.created_at}</time>
      </p>
      <p>
        Identité logique <code>{finding.logical_id}</code> · version courante{" "}
        <code>{finding.current_version_id}</code>
      </p>
      {finding.revision_chain?.length > 1 ? (
        <ol aria-label={`Chaîne de révisions du finding ${finding.id}`}>
          {finding.revision_chain.map((revision) => (
            <li key={revision.id}>
              v{revision.version} · <code>{revision.id}</code> ·{" "}
              <time dateTime={revision.created_at}>{revision.created_at}</time>
            </li>
          ))}
        </ol>
      ) : null}
    </li>
  );
}

function makeClaimOptions(
  articleClaims: ArticleClaimSource[],
  summaries: ConsolidatedClaimVerdict[],
  experiments: InvestigationExperiment[],
): Array<{ id: string; label: string }> {
  const sourceById = new Map(articleClaims.map((claim) => [claim.id, claim]));
  const ids = new Set([
    ...articleClaims.map((claim) => claim.id),
    ...summaries.map((claim) => claim.claim_id),
    ...experiments.flatMap((experiment) => experiment.claim_ids),
  ]);
  return [...ids].sort().map((id) => ({
    id,
    label: sourceById.get(id)?.statement ?? id,
  }));
}
