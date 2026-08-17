import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useCallback, useEffect, useState } from "react";

import {
  confirmManualDiscoveryRecovery,
  confirmVisibleDiscoveryRecovery,
  fetchDiscovery,
  launchDiscovery,
  markDiscoverySource,
  previewManualDiscoveryRecovery,
  previewVisibleDiscoveryRecovery,
  requestDiscoveryCompletion,
  retryDiscoveryStructuring,
  type DiscoveryRecoveryPreview,
  type SourceVerificationStatus,
} from "./api/discovery";
import {
  ApiError,
  createEdition,
  deleteEdition,
  type Edition,
  type EditionFields,
  type EditionStatus,
  getEdition,
  listEditions,
  transitionEdition,
  type Tlp,
} from "./api/editions";
import { JobStatusCard } from "./components/JobStatusCard";
import { EditorialBoard } from "./components/EditorialBoard";
import { SubjectWorkbench } from "./components/SubjectWorkbench";
import {
  cancelJob,
  terminalJobStatuses,
  type JobStatus,
  type JobView,
} from "./api/jobs";

const statusLabels: Record<EditionStatus, string> = {
  draft: "Brouillon",
  discovery: "Découverte",
  selection: "Sélection",
  production: "Production",
  review: "Revue",
  assembling: "Assemblage",
  published: "Publiée",
  archived: "Archivée",
};

function usePathname() {
  const [pathname, setPathname] = useState(window.location.pathname);
  useEffect(() => {
    const update = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);
  return pathname;
}

function navigate(path: string) {
  window.history.pushState({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

function Link({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <a
      href={to}
      onClick={(event) => {
        event.preventDefault();
        navigate(to);
      }}
    >
      {children}
    </a>
  );
}

export function App() {
  const pathname = usePathname();
  const detail = pathname.match(/^\/editions\/([^/]+)$/);
  const subject = pathname.match(/^\/subjects\/([^/]+)$/);
  return (
    <main>
      <header className="app-header">
        <Link to="/editions">CTI Bulletin</Link>
        <span>Utilisateur local : dev-analyst</span>
      </header>
      {subject ? (
        <SubjectWorkbench subjectId={subject[1]!} />
      ) : pathname === "/editions/new" ? (
        <EditionCreatePage />
      ) : detail ? (
        <EditionDetailPage editionId={detail[1]!} />
      ) : (
        <EditionListPage />
      )}
    </main>
  );
}

function EditionListPage() {
  const [countryCode, setCountryCode] = useState("");
  const [period, setPeriod] = useState("");
  const [status, setStatus] = useState<EditionStatus | "">("");
  const editions = useQuery({
    queryKey: ["editions", countryCode, period, status],
    queryFn: () => listEditions({ countryCode, period, status }),
  });

  return (
    <>
      <section className="page-heading">
        <div>
          <p className="eyebrow">Pilotage mensuel</p>
          <h1>Éditions</h1>
          <p>Créez une édition et suivez son passage jusqu’à la publication.</p>
        </div>
        <button className="button" onClick={() => navigate("/editions/new")}>
          Nouvelle édition
        </button>
      </section>
      <section className="filter-bar" aria-label="Filtres des éditions">
        <label>
          Code pays
          <input
            value={countryCode}
            maxLength={2}
            onChange={(event) =>
              setCountryCode(event.target.value.toUpperCase())
            }
          />
        </label>
        <label>
          Période
          <input
            type="month"
            value={period}
            onChange={(event) => setPeriod(event.target.value)}
          />
        </label>
        <label>
          Statut
          <select
            value={status}
            onChange={(event) =>
              setStatus(event.target.value as EditionStatus | "")
            }
          >
            <option value="">Tous</option>
            {Object.entries(statusLabels).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </section>
      {editions.isPending ? (
        <p role="status">Chargement des éditions…</p>
      ) : null}
      {editions.isError ? (
        <ErrorMessage
          error={editions.error}
          fallback="Impossible de charger les éditions."
        />
      ) : null}
      {editions.data?.total === 0 ? (
        <section className="empty-state">
          <h2>Aucune édition</h2>
          <p>Créez la première édition mensuelle pour commencer.</p>
        </section>
      ) : null}
      {editions.data?.items.length ? (
        <section className="edition-grid" aria-label="Liste des éditions">
          {editions.data.items.map((edition) => (
            <article className="edition-card" key={edition.id}>
              <div className="badge-row">
                <StatusBadge status={edition.status} />
                <TlpBadge tlp={edition.tlp} />
              </div>
              <h2>{edition.country}</h2>
              <p>{formatPeriod(edition.period_start)}</p>
              <progress max={100} value={edition.progress_percent}>
                {edition.progress_percent} %
              </progress>
              <Link to={`/editions/${edition.id}`}>Ouvrir l’édition</Link>
            </article>
          ))}
        </section>
      ) : null}
    </>
  );
}

function EditionCreatePage() {
  const [error, setError] = useState<Error | null>(null);
  const mutation = useMutation({ mutationFn: createEdition });

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    const data = new FormData(event.currentTarget);
    try {
      const edition = await mutation.mutateAsync(fieldsFromForm(data));
      navigate(`/editions/${edition.id}`);
    } catch (caught) {
      setError(caught instanceof Error ? caught : new Error("Erreur inconnue"));
    }
  }

  return (
    <section className="form-page">
      <p className="eyebrow">Nouvelle édition</p>
      <h1>Créer une édition mensuelle</h1>
      <p>La période couvre automatiquement le mois complet.</p>
      {error ? (
        <ErrorMessage error={error} fallback="Création impossible." />
      ) : null}
      <form className="edition-form" onSubmit={(event) => void submit(event)}>
        <label>
          Pays
          <input name="country" required minLength={2} />
        </label>
        <label>
          Code pays
          <input
            name="country_code"
            required
            pattern="[A-Za-z]{2}"
            maxLength={2}
          />
        </label>
        <label>
          Période
          <input name="period" type="month" required />
        </label>
        <label>
          TLP
          <select name="tlp" defaultValue="AMBER">
            {(["CLEAR", "GREEN", "AMBER", "AMBER+STRICT", "RED"] as Tlp[]).map(
              (tlp) => (
                <option key={tlp}>{tlp}</option>
              ),
            )}
          </select>
        </label>
        <label>
          Langues
          <input
            name="languages"
            defaultValue="fr,en"
            required
            aria-describedby="languages-help"
          />
        </label>
        <small id="languages-help">
          Codes séparés par des virgules, par exemple fr,en,fa.
        </small>
        <fieldset className="indicative-targets">
          <legend>Paramètres indicatifs</legend>
          <p>Ces objectifs ne limitent jamais la sélection éditoriale.</p>
          <label>
            Objectif indicatif d’articles principaux — sans limite de sélection
            <input
              name="target_major_articles"
              type="number"
              min={0}
              max={20}
              defaultValue={2}
              required
            />
          </label>
          <label>
            Objectif indicatif de brèves — sans limite de sélection
            <input
              name="target_briefs"
              type="number"
              min={0}
              max={100}
              defaultValue={6}
              required
            />
          </label>
        </fieldset>
        <label>
          Profil de sources
          <input
            name="source_profile"
            defaultValue="default"
            required
            pattern="[a-z0-9._-]+"
          />
        </label>
        <label>
          Édition précédente
          <input
            name="previous_edition_id"
            type="text"
            inputMode="text"
            pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
            aria-describedby="previous-edition-help"
          />
        </label>
        <small id="previous-edition-help">
          UUID optionnel d’une édition déjà existante.
        </small>
        <div className="form-actions">
          <button
            type="button"
            className="button button--secondary"
            onClick={() => navigate("/editions")}
          >
            Annuler
          </button>
          <button className="button" disabled={mutation.isPending}>
            {mutation.isPending ? "Création…" : "Créer l’édition"}
          </button>
        </div>
      </form>
    </section>
  );
}

function EditionDetailPage({ editionId }: { editionId: string }) {
  const [discoveryRunning, setDiscoveryRunning] = useState(() =>
    Boolean(window.localStorage.getItem(discoveryJobStorageKey(editionId))),
  );
  const queryClient = useQueryClient();
  const [showDeletion, setShowDeletion] = useState(false);
  const [deletionConfirmation, setDeletionConfirmation] = useState("");
  const edition = useQuery({
    queryKey: ["edition", editionId],
    queryFn: () => getEdition(editionId),
  });
  const transition = useMutation({
    mutationFn: ({
      current,
      target,
    }: {
      current: Edition;
      target: EditionStatus;
    }) => transitionEdition(current, target),
    onSuccess: (updated) => {
      queryClient.setQueryData(["edition", editionId], updated);
      void queryClient.invalidateQueries({ queryKey: ["editions"] });
    },
  });
  const deletion = useMutation({
    mutationFn: deleteEdition,
    onSuccess: () => {
      queryClient.removeQueries({ queryKey: ["edition", editionId] });
      void queryClient.invalidateQueries({ queryKey: ["editions"] });
      navigate("/editions");
    },
  });

  if (edition.isPending) return <p role="status">Chargement de l’édition…</p>;
  if (edition.isError)
    return (
      <ErrorMessage error={edition.error} fallback="Édition inaccessible." />
    );
  const current = edition.data;
  return (
    <section className="detail-page">
      <Link to="/editions">← Toutes les éditions</Link>
      <div className="detail-heading">
        <div>
          <p className="eyebrow">{formatPeriod(current.period_start)}</p>
          <h1>{current.country}</h1>
          <div className="badge-row">
            <StatusBadge status={current.status} />
            <TlpBadge tlp={current.tlp} />
          </div>
        </div>
        <p>Version {current.version}</p>
      </div>
      <section className="progress-panel" aria-labelledby="global-progress">
        <h2 id="global-progress">Progression globale</h2>
        <progress max={100} value={current.progress_percent}>
          {current.progress_percent} %
        </progress>
        <strong>{current.progress_percent} %</strong>
      </section>
      <dl className="edition-facts">
        <div>
          <dt>Langues</dt>
          <dd>{current.languages.join(", ")}</dd>
        </div>
        <div>
          <dt>Objectif indicatif d’articles principaux</dt>
          <dd>{current.target_major_articles}</dd>
        </div>
        <div>
          <dt>Objectif indicatif de brèves</dt>
          <dd>{current.target_briefs}</dd>
        </div>
        <div>
          <dt>Profil de sources</dt>
          <dd>{current.source_profile}</dd>
        </div>
        <div>
          <dt>Édition précédente</dt>
          <dd>
            {current.previous_edition_id ? (
              <Link to={`/editions/${current.previous_edition_id}`}>
                Ouvrir l’édition précédente
              </Link>
            ) : (
              "Aucune"
            )}
          </dd>
        </div>
      </dl>
      <DiscoveryPanel
        editionId={current.id}
        onRunningChange={setDiscoveryRunning}
      />
      {!discoveryRunning ? <EditorialBoard editionId={current.id} /> : null}
      <section className="actions-panel" aria-labelledby="edition-actions">
        <h2 id="edition-actions">Actions disponibles</h2>
        {transition.error ? (
          <ErrorMessage
            error={transition.error}
            fallback="Transition impossible."
          />
        ) : null}
        {current.allowed_transitions.length === 0 ? (
          <p>Aucune transition disponible.</p>
        ) : (
          <div className="action-list">
            {current.allowed_transitions.map((target) => (
              <button
                key={target}
                className={
                  target === "archived" ? "button button--danger" : "button"
                }
                disabled={transition.isPending}
                onClick={() => transition.mutate({ current, target })}
              >
                Passer à « {statusLabels[target]} »
              </button>
            ))}
          </div>
        )}
      </section>
      <section className="danger-zone" aria-labelledby="edition-deletion">
        <h2 id="edition-deletion">Zone dangereuse</h2>
        <p>
          Cette action efface définitivement l’édition et toutes ses données de
          découverte, de sélection et de production. Elle est irréversible.
        </p>
        {!showDeletion ? (
          <button
            type="button"
            className="button button--danger"
            onClick={() => setShowDeletion(true)}
          >
            Supprimer définitivement l’édition
          </button>
        ) : (
          <div className="deletion-confirmation">
            <label htmlFor="edition-deletion-confirmation">
              Pour confirmer, saisissez le nom du pays : {current.country}
            </label>
            <input
              id="edition-deletion-confirmation"
              autoComplete="off"
              value={deletionConfirmation}
              onChange={(event) => setDeletionConfirmation(event.target.value)}
            />
            {deletion.error ? (
              <ErrorMessage
                error={deletion.error}
                fallback="Suppression impossible."
              />
            ) : null}
            <div className="action-list">
              <button
                type="button"
                className="button button--secondary"
                disabled={deletion.isPending}
                onClick={() => {
                  setShowDeletion(false);
                  setDeletionConfirmation("");
                }}
              >
                Annuler
              </button>
              <button
                type="button"
                className="button button--danger"
                disabled={
                  deletion.isPending || deletionConfirmation !== current.country
                }
                onClick={() => deletion.mutate(current)}
              >
                {deletion.isPending
                  ? "Suppression…"
                  : "Effacer toutes les données"}
              </button>
            </div>
          </div>
        )}
      </section>
    </section>
  );
}

function discoveryJobStorageKey(editionId: string) {
  return `cti-discovery-job:${editionId}`;
}

function DiscoveryPanel({
  editionId,
  onRunningChange,
}: {
  editionId: string;
  onRunningChange: (running: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const storageKey = discoveryJobStorageKey(editionId);
  const [jobId, setJobId] = useState<string | null>(() =>
    window.localStorage.getItem(storageKey),
  );
  const [jobStatus, setJobStatus] = useState<JobStatus | null>(null);
  const [lastJob, setLastJob] = useState<JobView | null>(null);
  const [axis, setAxis] = useState("initial");
  const [manualMarkdown, setManualMarkdown] = useState("");
  const [showManualRecovery, setShowManualRecovery] = useState(false);
  const [showInitialInputChoice, setShowInitialInputChoice] = useState(
    !window.localStorage.getItem(storageKey),
  );
  const [recoveryPreview, setRecoveryPreview] = useState<{
    mode: "visible" | "manual";
    value: DiscoveryRecoveryPreview;
  } | null>(null);
  const [search, setSearch] = useState("");
  const [minimum, setMinimum] = useState(0);
  const [sourceStatus, setSourceStatus] = useState<
    SourceVerificationStatus | ""
  >("");
  const [sort, setSort] = useState<
    "newest" | "technical" | "novelty" | "title"
  >("technical");
  const discovery = useQuery({
    queryKey: ["discovery", editionId, search, minimum, sourceStatus, sort],
    queryFn: () =>
      fetchDiscovery(editionId, {
        search,
        minTechnicalPotential: minimum,
        sourceStatus,
        sort,
      }),
    refetchInterval:
      jobId && (jobStatus === null || !terminalJobStatuses.has(jobStatus))
        ? 2_000
        : false,
  });
  const launch = useMutation({
    mutationFn: () => launchDiscovery(editionId, axis.trim() || "initial"),
    onSuccess: (result) => {
      setJobId(result.job_id);
      setJobStatus(null);
      window.localStorage.setItem(storageKey, result.job_id);
      onRunningChange(true);
      void queryClient.invalidateQueries({
        queryKey: ["discovery", editionId],
      });
    },
  });
  const relaunch = useMutation({
    mutationFn: () =>
      launchDiscovery(editionId, axis.trim() || "initial", true),
    onSuccess: (result) => {
      setJobId(result.job_id);
      setJobStatus(null);
      window.localStorage.setItem(storageKey, result.job_id);
      onRunningChange(true);
      void queryClient.invalidateQueries({
        queryKey: ["discovery", editionId],
      });
    },
  });
  const markSource = useMutation({
    mutationFn: ({
      sourceId,
      status,
    }: {
      sourceId: string;
      status: SourceVerificationStatus;
    }) => markDiscoverySource(editionId, sourceId, status),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["discovery", editionId] }),
  });
  const reprocessReport = useMutation({
    mutationFn: (researchModelRunId: string) =>
      retryDiscoveryStructuring(
        editionId,
        researchModelRunId,
        axis.trim() || "initial",
      ),
    onSuccess: (result) => {
      setJobId(result.job_id);
      setJobStatus(null);
      window.localStorage.setItem(storageKey, result.job_id);
      onRunningChange(true);
    },
  });
  // const recoveryRunId =
    // lastJob?.status === "waiting_human" &&
    // lastJob.error_details?.phase === "chatgpt_incomplete" &&
    // typeof lastJob.error_details.model_run_id === "string"
      // ? lastJob.error_details.model_run_id
      // : null;
  const recoveryRunId =
    lastJob &&
    ["waiting_human", "failed", "cancelled"].includes(lastJob.status) &&
    typeof lastJob.error_details?.model_run_id === "string"
      ? lastJob.error_details.model_run_id
      : null;
  const refreshRecoveredJob = useCallback(
    (result: { status: string }) => {
      setJobStatus(result.status as JobStatus);
      setRecoveryPreview(null);
      setShowManualRecovery(false);
      onRunningChange(true);
      void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
    },
    [jobId, onRunningChange, queryClient],
  );
  const visibleRecovery = useMutation({
    mutationFn: () =>
      previewVisibleDiscoveryRecovery(editionId, recoveryRunId!, jobId!),
    onSuccess: (value) => setRecoveryPreview({ mode: "visible", value }),
  });
  const manualRecovery = useMutation({
    mutationFn: () =>
      previewManualDiscoveryRecovery(
        editionId,
        recoveryRunId!,
        jobId!,
        manualMarkdown,
      ),
    onSuccess: (value) => setRecoveryPreview({ mode: "manual", value }),
  });
  const confirmRecovery = useMutation({
    mutationFn: () => {
      if (!recoveryPreview || !recoveryRunId || !jobId) {
        throw new Error("Aucun aperçu de récupération à confirmer");
      }
      return recoveryPreview.mode === "visible"
        ? confirmVisibleDiscoveryRecovery(
            editionId,
            recoveryRunId,
            jobId,
            recoveryPreview.value.sha256,
          )
        : confirmManualDiscoveryRecovery(
            editionId,
            recoveryRunId,
            jobId,
            manualMarkdown,
            recoveryPreview.value.sha256,
          );
    },
    onSuccess: refreshRecoveredJob,
  });
  const completionRecovery = useMutation({
    mutationFn: () =>
      requestDiscoveryCompletion(editionId, recoveryRunId!, jobId!),
    onSuccess: refreshRecoveredJob,
  });
  const abandonRecovery = useMutation({
    mutationFn: () => cancelJob(jobId!),
    onSuccess: (job) => {
      setJobStatus(job.status);
      setLastJob(job);
      setRecoveryPreview(null);
      setShowManualRecovery(false);
      onRunningChange(false);
      void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
    },
  });
  const candidates = discovery.data?.candidates ?? [];
  const batches = discovery.data?.batches ?? [];
  const mergeStats = discovery.data?.merge_stats;
  const searchRunning =
    Boolean(jobId) &&
    (jobStatus === null || !terminalJobStatuses.has(jobStatus));
  const handleJobUpdate = useCallback((job: JobView) => {
    setLastJob(job);
    setJobStatus(job.status);
  }, []);
  const handleJobTerminal = useCallback(() => {
    window.localStorage.removeItem(storageKey);
    onRunningChange(false);
    void queryClient.invalidateQueries({
      queryKey: ["discovery", editionId],
    });
    void queryClient.invalidateQueries({
      queryKey: ["editorial-board", editionId],
    });
  }, [editionId, onRunningChange, queryClient, storageKey]);

  return (
    <section className="discovery-panel" aria-labelledby="discovery-heading">
      <div className="discovery-heading">
        <div>
          <p className="eyebrow">Découverte mensuelle</p>
          <h2 id="discovery-heading">Sujets candidats</h2>
        </div>
        {showInitialInputChoice && !jobId ? (
          <div className="input-choice-buttons">
            <button
              className="button"
              onClick={() => {
                setShowInitialInputChoice(false);
                launch.mutate();
              }}
              disabled={launch.isPending}
            >
              {launch.isPending
                ? "Lancement…"
                : "Rechercher avec ChatGPT"}
            </button>
            <button
              className="button button--secondary"
              onClick={() => {
                setShowInitialInputChoice(false);
                setShowManualRecovery(true);
              }}
            >
              Importer un rapport existant
            </button>
          </div>
        ) : (
          <button
            className="button"
            disabled={launch.isPending || searchRunning}
            onClick={() => launch.mutate()}
          >
            {launch.isPending ? "Lancement…" : "Rechercher les sujets"}
          </button>
        )}
      </div>
      <ol className="discovery-steps" aria-label="Étapes de découverte">
        <li>1. Recherche ChatGPT</li>
        <li>2. Analyse locale du rapport</li>
        <li>3. Sélection éditoriale</li>
      </ol>
      <label className="axis-field">
        Axe de recherche
        <input
          value={axis}
          onChange={(event) => setAxis(event.target.value)}
          placeholder="initial ou axe complémentaire"
        />
      </label>
      <p className="verification-warning" role="note">
        Les métadonnées et comptes IOC de découverte sont provisoires. Ils
        seront vérifiés depuis les documents archivés après la sélection.
      </p>
      {launch.error ? (
        <ErrorMessage error={launch.error} fallback="Recherche impossible." />
      ) : null}
      {jobId ? (
        <JobStatusCard
          jobId={jobId}
          onUpdate={handleJobUpdate}
          onTerminal={handleJobTerminal}
          onReprocessReport={(researchModelRunId) =>
            reprocessReport.mutate(researchModelRunId)
          }
        />
      ) : null}
      {jobId && recoveryRunId ? (
        <section className="recovery-panel" aria-labelledby="recovery-heading">
          <h3 id="recovery-heading">Reprendre la recherche ChatGPT</h3>
          <p>
            ChatGPT s’est arrêté sans produire de réponse finale. La
            conversation a été conservée et peut être reprise.
          </p>
          <div className="action-list">
            <button
              className="button button--secondary"
              disabled={visibleRecovery.isPending}
              onClick={() => visibleRecovery.mutate()}
            >
              Récupérer la réponse déjà affichée
            </button>
            <button
              className="button button--secondary"
              disabled={completionRecovery.isPending}
              onClick={() => completionRecovery.mutate()}
            >
              Demander à ChatGPT de terminer
            </button>
            <button
              className="button button--secondary"
              onClick={() => {
                setShowManualRecovery(true);
                setRecoveryPreview(null);
              }}
            >
              Coller une réponse
            </button>
          </div>
          {showManualRecovery ? (
            <div className="manual-recovery">
              <label htmlFor="manual-discovery-report">Rapport Markdown</label>
              <textarea
                id="manual-discovery-report"
                rows={14}
                value={manualMarkdown}
                onChange={(event) => {
                  setManualMarkdown(event.target.value);
                  setRecoveryPreview(null);
                }}
              />
              <label>
                Charger un fichier .md ou .txt
                <input
                  type="file"
                  accept=".md,.txt,text/markdown,text/plain"
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (file) {
                      void file.text().then((text) => {
                        setManualMarkdown(text);
                        setRecoveryPreview(null);
                      });
                    }
                  }}
                />
              </label>
              <button
                className="button button--secondary"
                disabled={!manualMarkdown.trim() || manualRecovery.isPending}
                onClick={() => manualRecovery.mutate()}
              >
                Prévisualiser le rapport
              </button>
            </div>
          ) : null}
          {recoveryPreview ? (
            <article
              className="recovery-preview"
              aria-label="Aperçu du rapport"
            >
              <h4>Aperçu avant intégration</h4>
              <p>
                {recoveryPreview.value.subject_count} sujets ·{" "}
                {recoveryPreview.value.publication_count} publications ·{" "}
                {recoveryPreview.value.ioc_count} IOC provisoires
              </p>
              <p>
                IOC par type :{" "}
                {Object.entries(recoveryPreview.value.ioc_type_counts)
                  .map(([type, count]) => `${type}: ${count}`)
                  .join(" · ") || "aucun"}
              </p>
              <ul>
                {recoveryPreview.value.subjects.map((subject) => (
                  <li key={subject}>{subject}</li>
                ))}
              </ul>
              {recoveryPreview.value.warnings.length ? (
                <details>
                  <summary>
                    {recoveryPreview.value.warnings.length} avertissement(s)
                  </summary>
                  <ul>
                    {recoveryPreview.value.warnings.map((warning) => (
                      <li key={warning}>{warning}</li>
                    ))}
                  </ul>
                </details>
              ) : null}
              <div className="action-list">
                <button
                  className="button"
                  disabled={confirmRecovery.isPending}
                  onClick={() => confirmRecovery.mutate()}
                >
                  Confirmer et intégrer
                </button>
                <button
                  className="button button--secondary"
                  onClick={() => setRecoveryPreview(null)}
                >
                  Annuler
                </button>
              </div>
            </article>
          ) : null}
          {visibleRecovery.isError ||
          manualRecovery.isError ||
          completionRecovery.isError ||
          confirmRecovery.isError ||
          abandonRecovery.isError ? (
            <p role="alert" className="error-message">
              La récupération n’a pas pu être effectuée.
            </p>
          ) : null}
          <button
            className="button button--danger"
            disabled={abandonRecovery.isPending}
            onClick={() => abandonRecovery.mutate()}
          >
            Abandonner la recherche
          </button>
        </section>
      ) : null}
      <details className="technical-discovery-details">
        <summary>Détails techniques de la découverte</summary>
        <div className="candidate-filters" aria-label="Filtres des candidats">
          <label>
            Recherche
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          <label>
            Potentiel technique minimal
            <select
              value={minimum}
              onChange={(event) => setMinimum(Number(event.target.value))}
            >
              {[0, 1, 2, 3, 4].map((value) => (
                <option key={value} value={value}>
                  {value}/4
                </option>
              ))}
            </select>
          </label>
          <label>
            État des sources
            <select
              value={sourceStatus}
              onChange={(event) =>
                setSourceStatus(
                  event.target.value as SourceVerificationStatus | "",
                )
              }
            >
              <option value="">Tous</option>
              <option value="unverified">Non vérifiée</option>
              <option value="verify_later">À vérifier</option>
              <option value="invalid">Invalide</option>
              <option value="unavailable">Indisponible</option>
            </select>
          </label>
          <label>
            Tri
            <select
              value={sort}
              onChange={(event) => setSort(event.target.value as typeof sort)}
            >
              <option value="technical">Potentiel technique</option>
              <option value="newest">Date de l’événement</option>
              <option value="novelty">Nouveauté</option>
              <option value="title">Titre</option>
            </select>
          </label>
        </div>
        {discovery.isPending ? (
          <p role="status">Chargement des candidats…</p>
        ) : null}
        {discovery.isError ? (
          <ErrorMessage
            error={discovery.error}
            fallback="Candidats inaccessibles."
          />
        ) : null}
        {mergeStats ? (
          <section className="discovery-consolidation-stats">
            <h4>Découverte cumulée</h4>
            <div className="stats-grid">
              <div className="stat-item">
                <span className="stat-label">Contributions</span>
                <span className="stat-value">{mergeStats.raw_batch_count}</span>
              </div>
              <div className="stat-item">
                <span className="stat-label">Candidats bruts</span>
                <span className="stat-value">
                  {mergeStats.raw_candidate_count}
                </span>
              </div>
              <div className="stat-item">
                <span className="stat-label">Sujets consolidés</span>
                <span className="stat-value">
                  {mergeStats.consolidated_candidate_count}
                </span>
              </div>
              <div className="stat-item">
                <span className="stat-label">Publications uniques</span>
                <span className="stat-value">
                  {mergeStats.unique_publication_count}
                </span>
              </div>
              <div className="stat-item">
                <span className="stat-label">
                  Doublons fusionnés
                </span>
                <span className="stat-value">
                  {
                    mergeStats.duplicate_publication_occurrence_count
                  }
                </span>
              </div>
            </div>
          </section>
        ) : null}
        <div className="candidate-list">
          {candidates.map((candidate) => (
            <article className="candidate-card" key={candidate.id}>
              <div className="candidate-card__heading">
                <h3>{candidate.title}</h3>
                <span>Technique {candidate.technical_potential}/4</span>
              </div>
              <p>{candidate.summary}</p>
              <p>
                <strong>Acteur ou campagne proposé :</strong>{" "}
                {candidate.actor_or_campaign ?? "unknown"}
              </p>
              <p>
                <strong>Potentiel technique :</strong>{" "}
                {candidate.technical_potential_reason ?? "Non précisé."}
              </p>
              <p>
                <strong>Artefacts annoncés :</strong>{" "}
                {candidate.likely_artifacts.join(", ") || "non signalée"}
              </p>
              <p>
                <strong>Publications :</strong>{" "}
                {candidate.valid_publication_count ?? candidate.sources.length}{" "}
                valides ·{" "}
                {candidate.incomplete_publication_count ??
                  candidate.incomplete_sources?.length ??
                  0}{" "}
                incomplètes
                {candidate.context_only ? " · contexte uniquement" : ""}
              </p>
              <p className="verification-warning" role="note">
                IOC repérés pendant la recherche — non encore vérifiés depuis
                les sources.
              </p>
              <p>
                <strong>IOC provisoirement visibles :</strong>{" "}
                {candidate.provisional_ioc_count ?? 0} ·{" "}
                {Object.entries(candidate.provisional_ioc_type_counts ?? {})
                  .map(([type, count]) => `${type}: ${count}`)
                  .join(", ") || "aucun"}
                {candidate.has_publisher_ioc_count
                  ? " · total éditeur annoncé"
                  : ""}
              </p>
              {(candidate.provisional_iocs ?? []).length ? (
                <>
                  <p>
                    Exemples :{" "}
                    {(candidate.provisional_iocs ?? [])
                      .slice(0, 5)
                      .map((ioc) => ioc.raw_value)
                      .join(", ")}
                  </p>
                  <details className="research-trace">
                    <summary>
                      Voir la liste complète (
                      {candidate.provisional_ioc_count ?? 0})
                    </summary>
                    <ul>
                      {(candidate.provisional_iocs ?? []).map((ioc) => (
                        <li key={ioc.id}>
                          <code>{ioc.raw_value}</code> — {ioc.proposed_type}
                          {ioc.warnings.length
                            ? ` — ${ioc.warnings.join(", ")}`
                            : ""}
                        </li>
                      ))}
                    </ul>
                  </details>
                </>
              ) : null}
              {candidate.uncertainties.length ? (
                <div className="uncertainties">
                  <strong>Incertitudes</strong>
                  <ul>
                    {candidate.uncertainties.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {(candidate.parsing_warnings ?? []).length ? (
                <div className="uncertainties" role="note">
                  <strong>Avertissements de parsing</strong>
                  <ul>
                    {(candidate.parsing_warnings ?? []).map((warning) => (
                      <li key={warning}>{warning}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
              <h4>Publications proposées</h4>
              <ul className="source-list">
                {candidate.sources.map((source) => (
                  <li key={source.id}>
                    <a href={source.url} target="_blank" rel="noreferrer">
                      {source.title}
                    </a>
                    <span>
                      {source.publisher} ·{" "}
                      {source.published_at ?? "date inconnue"}
                    </span>
                    <span>
                      {source.period_relation ?? "unknown"} · {source.role} ·{" "}
                      {source.verification_status}
                    </span>
                    <span>
                      IOC provisoires : {source.ioc_presence ?? "unknown"} ·
                      déclarés {source.ioc_declared_count ?? "unknown"} ·
                      visibles {source.ioc_visible_count ?? "unknown"}
                    </span>
                    {(source.parsing_warnings ?? []).map((warning) => (
                      <small key={warning}>{warning}</small>
                    ))}
                    <select
                      aria-label={`État de ${source.title}`}
                      value={source.verification_status}
                      disabled={markSource.isPending}
                      onChange={(event) =>
                        markSource.mutate({
                          sourceId: source.id,
                          status: event.target
                            .value as SourceVerificationStatus,
                        })
                      }
                    >
                      <option value="unverified">Non vérifiée</option>
                      <option value="verify_later">À vérifier</option>
                      <option value="invalid">Invalide</option>
                      <option value="unavailable">Indisponible</option>
                    </select>
                  </li>
                ))}
                {(candidate.incomplete_sources ?? []).map((source) => (
                  <li key={source.id}>
                    <strong>{source.title}</strong>
                    <span>
                      Publication incomplète · {source.publisher} · URL{" "}
                      {source.raw_url ?? "absente"}
                    </span>
                    <span>
                      {source.period_relation} · {source.role} · IOC provisoires
                      : {source.ioc_presence}
                    </span>
                    {source.parsing_warnings.map((warning) => (
                      <small key={warning}>{warning}</small>
                    ))}
                  </li>
                ))}
              </ul>
            </article>
          ))}
        </div>
      </details>
      {batches.length ? (
        <details className="research-trace report-diagnostics">
          <summary>Rapport et diagnostic</summary>
          {batches.map((batch) => (
            <article className="diagnostic-batch" key={batch.id}>
              <h3>{batch.complementary_axis}</h3>
              <p>
                Parsing : {batch.parsing_status ?? "historique"} · parseur{" "}
                {batch.parser_version ?? "historique"}
              </p>
              {(batch.parsing_warnings ?? []).length ? (
                <ul>
                  {batch.parsing_warnings.map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              ) : null}
              <dl className="job-diagnostics">
                <div>
                  <dt>ModelRun de recherche</dt>
                  <dd>{batch.discovery_model_run_id}</dd>
                </div>
                <div>
                  <dt>ModelRun de parsing</dt>
                  <dd>{batch.structuring_model_run_id}</dd>
                </div>
                <div>
                  <dt>Correlation ID</dt>
                  <dd>{lastJob?.correlation_id ?? "Non disponible"}</dd>
                </div>
              </dl>
              <a
                href={batch.archived_report_url}
                target="_blank"
                rel="noreferrer"
              >
                Consulter le rapport Markdown ChatGPT archivé
              </a>
              <p>
                Le rapport ChatGPT archivé sera réutilisé. Aucun nouvel appel au
                bridge ne sera effectué.
              </p>
              <div className="editorial-actions">
                <button
                  className="button button--secondary"
                  disabled={reprocessReport.isPending}
                  onClick={() =>
                    reprocessReport.mutate(batch.discovery_model_run_id)
                  }
                >
                  Retraiter le rapport archivé
                </button>
                <button
                  className="button button--secondary"
                  disabled={relaunch.isPending}
                  onClick={() => {
                    if (
                      window.confirm(
                        "Relancer la recherche web créera une nouvelle conversation ChatGPT et conservera le rapport actuel. Continuer ?",
                      )
                    )
                      relaunch.mutate();
                  }}
                >
                  Relancer la recherche web
                </button>
              </div>
              <h3>Requêtes</h3>
              <ul>
                {batch.queries.map((query) => (
                  <li key={query}>{query}</li>
                ))}
              </ul>
              <h3>Citations du modèle</h3>
              <ul>
                {batch.citations.map((citation) => (
                  <li key={`${citation.url}-${citation.label}`}>
                    <a href={citation.url} target="_blank" rel="noreferrer">
                      {citation.label}
                    </a>
                    {citation.excerpt ? <p>{citation.excerpt}</p> : null}
                  </li>
                ))}
              </ul>
              {(batch.unattached_visible_citations ?? []).length ? (
                <>
                  <h3>Citations visibles non rattachées</h3>
                  <ul>
                    {batch.unattached_visible_citations.map((citation) => (
                      <li key={`${citation.canonical_url}-${citation.label}`}>
                        <a href={citation.url} target="_blank" rel="noreferrer">
                          {citation.label}
                        </a>
                      </li>
                    ))}
                  </ul>
                </>
              ) : null}
            </article>
          ))}
        </details>
      ) : null}
    </section>
  );
}

function StatusBadge({ status }: { status: EditionStatus }) {
  return (
    <span className={`badge badge--status badge--${status}`}>
      {statusLabels[status]}
    </span>
  );
}

function TlpBadge({ tlp }: { tlp: Tlp }) {
  return (
    <span className={`badge badge--tlp-${tlp.toLowerCase().replace("+", "-")}`}>
      TLP:{tlp}
    </span>
  );
}

function ErrorMessage({ error, fallback }: { error: Error; fallback: string }) {
  return (
    <p role="alert" className="error-message">
      {error instanceof ApiError ? error.message : fallback}
    </p>
  );
}

function fieldsFromForm(data: FormData): EditionFields {
  const period = formValue(data, "period");
  const year = Number(period.slice(0, 4));
  const month = Number(period.slice(5, 7));
  const finalDay = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return {
    country: formValue(data, "country").trim(),
    country_code: formValue(data, "country_code").trim().toUpperCase(),
    period_start: `${period}-01`,
    period_end: `${period}-${String(finalDay).padStart(2, "0")}`,
    tlp: formValue(data, "tlp") as Tlp,
    languages: formValue(data, "languages")
      .split(",")
      .map((language) => language.trim())
      .filter(Boolean),
    target_major_articles: Number(data.get("target_major_articles")),
    target_briefs: Number(data.get("target_briefs")),
    previous_edition_id: optionalFormValue(data, "previous_edition_id"),
    source_profile: formValue(data, "source_profile").trim(),
  };
}

function formValue(data: FormData, key: string): string {
  const value = data.get(key);
  return typeof value === "string" ? value : "";
}

function optionalFormValue(data: FormData, key: string): string | null {
  const value = formValue(data, key).trim();
  return value || null;
}

function formatPeriod(periodStart: string) {
  return new Intl.DateTimeFormat("fr-FR", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${periodStart}T00:00:00Z`));
}
