import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";

import {
  attachIncompleteSourceUrl,
  confirmManualDiscoveryRecovery,
  confirmVisibleDiscoveryRecovery,
  fetchDiscovery,
  fetchDiscoveryRunCandidates,
  fetchDiscoveryRuns,
  launchDiscoveryRun,
  markDiscoverySource,
  previewDiscoveryImport,
  confirmDiscoveryImport,
  previewManualDiscoveryRecovery,
  previewVisibleDiscoveryRecovery,
  requestDiscoveryCompletion,
  reprocessReport as reprocessReportFn,
  type DiscoveryRecoveryPreview,
  type DiscoveryRun,
  type SourceVerificationStatus,
} from "../../api/discovery";
import { renderDiscoveryMarkdown } from "../../discoveryMarkdownExport";
import { JobStatusCard } from "../../components/JobStatusCard";
import { ErrorMessage } from "../../components/ErrorMessage";
import {
  cancelJob,
  fetchJob,
  listJobs,
  terminalJobStatuses,
  type JobView,
} from "../../api/jobs";

function IncompleteSourceUrlForm({
  onSubmit,
  pending,
  error,
}: {
  onSubmit: (url: string) => void;
  pending: boolean;
  error: Error | null;
}) {
  const [url, setUrl] = useState("");
  return (
    <form
      className="incomplete-source-url-form"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        const trimmed = url.trim();
        if (!trimmed) return;
        onSubmit(trimmed);
      }}
    >
      <label>
        Lien manquant
        <input
          type="url"
          required
          placeholder="https://..."
          value={url}
          disabled={pending}
          onChange={(event) => setUrl(event.target.value)}
        />
      </label>
      <button type="submit" disabled={pending || !url.trim()}>
        {pending ? "Association…" : "Associer le lien"}
      </button>
      {error ? (
        <ErrorMessage
          error={error}
          fallback="Le lien n'a pas pu être associé."
        />
      ) : null}
    </form>
  );
}

export function DiscoveryPanel({
  editionId,
  readOnly = false,
}: {
  editionId: string;
  readOnly?: boolean;
}) {
  const queryClient = useQueryClient();
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [reconciliationJobId, setReconciliationJobId] = useState<string | null>(
    null,
  );
  const [axis, setAxis] = useState("initial");
  const [sourceProfile, setSourceProfile] = useState("default");
  const [manualMarkdown, setManualMarkdown] = useState("");
  const [showManualRecovery, setShowManualRecovery] = useState(false);
  // L'import autonome d'une réponse ChatGPT est un flux distinct de la
  // récupération d'un run échoué : état, endpoints et cycle de vie séparés.
  const [showManualImport, setShowManualImport] = useState(false);
  const [manualImportMarkdown, setManualImportMarkdown] = useState("");
  const [manualImportPreview, setManualImportPreview] =
    useState<DiscoveryRecoveryPreview | null>(null);
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
  const discoveryRuns = useQuery({
    queryKey: ["discovery-runs", editionId],
    queryFn: () => fetchDiscoveryRuns(editionId),
    refetchInterval: ({ state }) => {
      const runs = state.data;
      return runs?.some(
        (run) =>
          run.execution?.status === "queued" ||
          run.execution?.status === "running",
      )
        ? 2_000
        : false;
    },
  });
  const runs = useMemo(
    () =>
      [...(discoveryRuns.data ?? [])].sort(
        (left, right) =>
          new Date(right.created_at).getTime() -
          new Date(left.created_at).getTime(),
      ),
    [discoveryRuns.data],
  );
  const discoveryRunById = useMemo(
    () => new Map(runs.map((run) => [run.run_id, run])),
    [runs],
  );
  useEffect(() => {
    if (runs.length === 0) {
      setSelectedRunId(null);
      return;
    }
    if (!selectedRunId || !runs.some((run) => run.run_id === selectedRunId)) {
      setSelectedRunId(runs[0]?.run_id ?? null);
    }
  }, [runs, selectedRunId]);
  const selectedRun =
    runs.find((run) => run.run_id === selectedRunId) ?? runs[0];
  const selectedRunCandidates = useQuery({
    // Nested under ["discovery", editionId] so every discovery invalidation
    // (job completion, source verification, URL correction) refreshes it.
    queryKey: ["discovery", editionId, "run-candidates", selectedRun?.run_id],
    queryFn: () => fetchDiscoveryRunCandidates(editionId, selectedRun!.run_id),
    enabled: Boolean(selectedRun),
  });
  const selectedJobId = selectedRun?.execution?.job_id ?? null;
  const selectedJob = useQuery({
    queryKey: ["job", selectedJobId],
    queryFn: () => fetchJob(selectedJobId!),
    enabled: Boolean(selectedJobId),
    refetchInterval: ({ state }) =>
      state.data && terminalJobStatuses.has(state.data.status) ? false : 2_000,
  });
  const reconciliationJob = useQuery({
    queryKey: ["job", reconciliationJobId],
    queryFn: () => fetchJob(reconciliationJobId!),
    enabled: Boolean(reconciliationJobId),
    refetchInterval: ({ state }) =>
      state.data && terminalJobStatuses.has(state.data.status) ? false : 2_000,
  });
  const reconciliationJobs = useQuery({
    queryKey: ["jobs", editionId, "reconcile_discovery"],
    queryFn: () => listJobs("edition", editionId, "reconcile_discovery"),
    refetchInterval: ({ state }) =>
      state.data?.some(
        (job) => job.status === "queued" || job.status === "running",
      )
        ? 2_000
        : false,
  });
  const mergeReconciling =
    reconciliationJobs.data?.some(
      (job) => job.status === "queued" || job.status === "running",
    ) ?? false;
  const lastJob: JobView | null = selectedJob.data ?? null;
  const jobId = selectedJobId;
  const discovery = useQuery({
    queryKey: ["discovery", editionId, search, minimum, sourceStatus, sort],
    queryFn: () =>
      fetchDiscovery(editionId, {
        search,
        minTechnicalPotential: minimum,
        sourceStatus,
        sort,
      }),
  });
  useEffect(() => {
    if (
      reconciliationJob.data &&
      terminalJobStatuses.has(reconciliationJob.data.status)
    ) {
      setReconciliationJobId(null);
      void queryClient.invalidateQueries({
        queryKey: ["discovery", editionId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["discovery-runs", editionId],
      });
      // A discovery wave moves the active snapshot, so both boards rebuilt
      // from it go stale (AW-008: Selection replaced the editorial board).
      void queryClient.invalidateQueries({
        queryKey: ["fusion", editionId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["selection-board", editionId],
      });
    }
  }, [editionId, queryClient, reconciliationJob.data]);
  const launch = useMutation({
    mutationFn: ({
      payload,
      idempotencyKey,
    }: {
      payload: { complementary_axis: string; source_profile: string };
      idempotencyKey: string;
    }) => launchDiscoveryRun(editionId, payload, idempotencyKey),
    onSuccess: (run) => {
      setSelectedRunId(run.run_id);
      void queryClient.invalidateQueries({
        queryKey: ["discovery-runs", editionId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["discovery", editionId],
      });
      // A discovery wave moves the active snapshot, so both boards rebuilt
      // from it go stale (AW-008: Selection replaced the editorial board).
      void queryClient.invalidateQueries({
        queryKey: ["fusion", editionId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["selection-board", editionId],
      });
    },
  });
  const markSource = useMutation({
    mutationFn: ({
      candidateId,
      sourceId,
      status,
    }: {
      candidateId: string;
      sourceId: string;
      status: SourceVerificationStatus;
    }) => markDiscoverySource(editionId, candidateId, sourceId, status),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["discovery", editionId] }),
  });
  const attachUrl = useMutation({
    mutationFn: ({
      candidateId,
      incompleteSourceId,
      url,
    }: {
      candidateId: string;
      incompleteSourceId: string;
      url: string;
    }) =>
      attachIncompleteSourceUrl(
        editionId,
        candidateId,
        incompleteSourceId,
        url,
      ),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["discovery", editionId] }),
  });
  const reprocessReport = useMutation({
    mutationFn: ({
      runId,
      researchModelRunId,
      idempotencyKey,
    }: {
      runId: string;
      researchModelRunId: string;
      idempotencyKey: string;
    }) =>
      reprocessReportFn(editionId, runId, researchModelRunId, idempotencyKey),
    onSuccess: (_result, variables) => {
      // Le retraitement reste rattaché au run d'origine : seule sa révision
      // de batch change.
      setSelectedRunId(variables.runId);
      void queryClient.invalidateQueries({
        queryKey: ["discovery-runs", editionId],
      });
    },
  });
  const recoveryRunId =
    lastJob &&
    ["waiting_human", "failed", "cancelled"].includes(lastJob.status) &&
    typeof lastJob.error_details?.model_run_id === "string"
      ? lastJob.error_details.model_run_id
      : null;
  // Une récupération poursuit le DiscoveryRun d'origine : elle ne change pas
  // l'identité affichée, seulement l'état de son exécution.
  const refreshRecoveredJob = useCallback(() => {
    setRecoveryPreview(null);
    setShowManualRecovery(false);
    void queryClient.invalidateQueries({
      queryKey: ["discovery-runs", editionId],
    });
    void queryClient.invalidateQueries({
      queryKey: ["discovery", editionId],
    });
  }, [editionId, queryClient]);
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
  const previewImport = useMutation({
    mutationFn: () =>
      previewDiscoveryImport(
        editionId,
        manualImportMarkdown,
        sourceProfile,
        axis.trim() || "manual-import",
      ),
    onSuccess: setManualImportPreview,
  });
  const confirmImport = useMutation({
    mutationFn: ({ idempotencyKey }: { idempotencyKey: string }) => {
      if (!manualImportPreview) {
        throw new Error("Aucun aperçu d’import à confirmer");
      }
      return confirmDiscoveryImport(
        editionId,
        manualImportMarkdown,
        manualImportPreview.sha256,
        sourceProfile,
        idempotencyKey,
        axis.trim() || "manual-import",
      );
    },
    onSuccess: (result) => {
      // L'import est parsé localement, mais la consolidation en sujets tourne
      // dans un job de réconciliation asynchrone déclenché côté backend. Le
      // suivre comme les autres jobs de découverte : rafraîchir tout de suite
      // ferait la course avec ce job et laisserait la sélection des sujets
      // vide (0 sujet consolidé) quand l'import n'apporte qu'un seul candidat.
      setShowManualImport(false);
      setManualImportMarkdown("");
      setManualImportPreview(null);
      setSelectedRunId(result.run_id);
      setReconciliationJobId(result.reconciliation_job_id);
      void queryClient.invalidateQueries({
        queryKey: ["discovery-runs", editionId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["discovery", editionId],
      });
      // A discovery wave moves the active snapshot, so both boards rebuilt
      // from it go stale (AW-008: Selection replaced the editorial board).
      void queryClient.invalidateQueries({
        queryKey: ["fusion", editionId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["selection-board", editionId],
      });
    },
  });
  const abandonRecovery = useMutation({
    mutationFn: () => cancelJob(jobId!),
    onSuccess: () => {
      setRecoveryPreview(null);
      setShowManualRecovery(false);
      void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
      void queryClient.invalidateQueries({
        queryKey: ["discovery-runs", editionId],
      });
    },
  });
  const candidateData = discovery.data?.candidates;
  const candidates = useMemo(() => candidateData ?? [], [candidateData]);
  const discoveryMarkdownExport = useMemo(
    () => renderDiscoveryMarkdown(candidates),
    [candidates],
  );
  const batches = discovery.data?.batches ?? [];
  const handleJobTerminal = useCallback(() => {
    void queryClient.invalidateQueries({
      queryKey: ["discovery-runs", editionId],
    });
    void queryClient.invalidateQueries({
      queryKey: ["discovery", editionId],
    });
    void queryClient.invalidateQueries({
      queryKey: ["fusion", editionId],
    });
    void queryClient.invalidateQueries({
      queryKey: ["selection-board", editionId],
    });
  }, [editionId, queryClient]);

  return (
    <section className="discovery-panel" aria-labelledby="discovery-heading">
      <div className="discovery-heading">
        <div>
          <p className="eyebrow">Découverte mensuelle</p>
          <h2 id="discovery-heading">Sujets candidats</h2>
        </div>
        <a href={`/editions/${editionId}/fusion`}>Voir la fusion</a>
        {/* Les deux actions restent disponibles tout au long du cycle de vie :
            avant la première recherche, après un import, après plusieurs lots. */}
        {!readOnly ? (
          <div className="input-choice-buttons">
            <button
              className="button"
              disabled={launch.isPending || mergeReconciling}
              onClick={() =>
                launch.mutate({
                  idempotencyKey: crypto.randomUUID(),
                  payload: {
                    complementary_axis: axis.trim() || "initial",
                    source_profile: sourceProfile,
                  },
                })
              }
            >
              {launch.isPending ? "Lancement…" : "Nouvelle recherche ChatGPT"}
            </button>
            <button
              className="button button--secondary"
              disabled={confirmImport.isPending}
              onClick={() => setShowManualImport((open) => !open)}
            >
              Coller une réponse ChatGPT
            </button>
          </div>
        ) : null}
      </div>
      {readOnly ? (
        <p className="workflow-read-only-note" role="note">
          Historique de découverte : les recherches et corrections sont
          désactivées.
        </p>
      ) : null}
      <section
        className="discovery-run-history"
        aria-labelledby="run-history-heading"
      >
        <h3 id="run-history-heading">Historique des recherches</h3>
        {discoveryRuns.isPending ? (
          <p role="status">Chargement de l’historique…</p>
        ) : null}
        {discoveryRuns.isError ? (
          <p role="alert" className="error-message">
            Impossible de récupérer l’historique des recherches.
          </p>
        ) : null}
        {runs.length === 0 && !discoveryRuns.isPending ? (
          <p>Aucune recherche enregistrée.</p>
        ) : null}
        <ol>
          {runs.map((run: DiscoveryRun) => {
            const execution = run.execution;
            const isSelected = run.run_id === selectedRun?.run_id;
            const manualImport = run.input_mode === "manual_import";
            return (
              <li key={run.run_id}>
                <button
                  className="button button--secondary"
                  aria-pressed={isSelected}
                  onClick={() => setSelectedRunId(run.run_id)}
                >
                  {new Date(run.created_at).toLocaleString("fr-FR")}
                </button>
                <div>
                  <p>
                    <strong>{run.complementary_axis}</strong> ·{" "}
                    {run.source_profile} ·{" "}
                    {manualImport ? "Import manuel" : "Recherche ChatGPT"}
                  </p>
                  {manualImport && !execution ? (
                    <p role="status">Import manuel · résultat disponible</p>
                  ) : null}
                  {execution ? (
                    <>
                      <p>
                        État :{" "}
                        {execution.status === "queued"
                          ? "En attente"
                          : execution.status === "running"
                            ? "En cours"
                            : execution.status === "waiting_human"
                              ? "Validation humaine requise"
                              : execution.status === "succeeded"
                                ? "Terminée"
                                : execution.status === "failed"
                                  ? "Échec"
                                  : "Annulée"}
                      </p>
                      {execution.status === "queued" ||
                      execution.status === "running" ? (
                        <p>
                          Progression : {execution.progress_current}/
                          {execution.progress_total || "—"}
                          {execution.user_message
                            ? ` — ${execution.user_message}`
                            : ""}
                        </p>
                      ) : null}
                      {execution.status === "waiting_human" ? (
                        <p role="status">
                          L’intervention d’un analyste est requise.
                          {execution.user_message
                            ? ` ${execution.user_message}`
                            : ""}
                        </p>
                      ) : null}
                      {execution.status === "failed" ? (
                        <p role="alert">
                          {execution.error_code
                            ? `${execution.error_code} — `
                            : ""}
                          {execution.error_message || "La recherche a échoué."}
                        </p>
                      ) : null}
                    </>
                  ) : null}
                  {run.result?.archived_report_url ? (
                    <a
                      href={run.result.archived_report_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      Consulter le rapport Markdown archivé
                    </a>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ol>
        {selectedRun ? (
          <section
            className="discovery-run-candidates"
            aria-label={`Candidats de la recherche ${selectedRun.complementary_axis}`}
          >
            <h4>Candidats produits par cette recherche</h4>
            {selectedRunCandidates.isPending ? (
              <p role="status">Chargement des candidats de la recherche…</p>
            ) : null}
            {selectedRunCandidates.isError ? (
              <p role="alert" className="error-message">
                Impossible de récupérer les candidats de cette recherche.
              </p>
            ) : null}
            {!selectedRunCandidates.isPending &&
            !selectedRunCandidates.isError ? (
              <>
                <p>
                  {selectedRunCandidates.data?.length ?? 0} candidat(s)
                  canonique(s)
                </p>
                {selectedRunCandidates.data?.length ? (
                  <ul>
                    {selectedRunCandidates.data.slice(0, 5).map((candidate) => (
                      <li key={candidate.id}>{candidate.title}</li>
                    ))}
                  </ul>
                ) : null}
              </>
            ) : null}
          </section>
        ) : null}
      </section>
      {mergeReconciling ? (
        <p role="status">
          Le bridge ChatGPT est occupé à évaluer la dernière contribution pour
          la fusion : attendez que cette évaluation se termine avant de lancer
          une nouvelle recherche.
        </p>
      ) : null}
      <ol className="discovery-steps" aria-label="Étapes de découverte">
        <li>1. Recherche ChatGPT</li>
        <li>2. Analyse locale du rapport</li>
        <li>3. Sélection éditoriale</li>
      </ol>
      <label className="axis-field">
        Axe de recherche
        <input
          value={axis}
          disabled={readOnly}
          onChange={(event) => setAxis(event.target.value)}
          placeholder="initial ou axe complémentaire"
        />
      </label>
      <label className="axis-field">
        Profil de sources
        <input
          value={sourceProfile}
          disabled={readOnly}
          onChange={(event) => setSourceProfile(event.target.value)}
        />
      </label>
      <p className="verification-warning" role="note">
        Les métadonnées et comptes IOC de découverte sont provisoires. Ils
        seront vérifiés depuis les documents archivés après la sélection.
      </p>
      {launch.error ? (
        <ErrorMessage error={launch.error} fallback="Recherche impossible." />
      ) : null}
      {!readOnly && showManualImport ? (
        <section
          className="manual-import"
          aria-labelledby="manual-import-heading"
        >
          <h3 id="manual-import-heading">Réponse ChatGPT existante</h3>
          <p>
            Utilisez cette option si vous avez déjà exécuté le prompt de
            découverte dans ChatGPT et disposez de sa réponse Markdown.
          </p>
          <label htmlFor="manual-discovery-import">Réponse ChatGPT</label>
          <textarea
            id="manual-discovery-import"
            rows={14}
            value={manualImportMarkdown}
            onChange={(event) => {
              setManualImportMarkdown(event.target.value);
              setManualImportPreview(null);
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
                    setManualImportMarkdown(text);
                    setManualImportPreview(null);
                  });
                }
              }}
            />
          </label>
          <div className="action-list">
            <button
              className="button button--secondary"
              disabled={!manualImportMarkdown.trim() || previewImport.isPending}
              onClick={() => previewImport.mutate()}
            >
              Prévisualiser
            </button>
            <button
              className="button button--secondary"
              onClick={() => {
                setShowManualImport(false);
                setManualImportPreview(null);
              }}
            >
              Fermer
            </button>
          </div>
          {manualImportPreview ? (
            <article
              className="recovery-preview"
              aria-label="Aperçu de l’import"
            >
              <p>
                {manualImportPreview.subject_count} sujets ·{" "}
                {manualImportPreview.publication_count} publications ·{" "}
                {manualImportPreview.ioc_count} IOC provisoires
              </p>
              <ul>
                {manualImportPreview.subjects.map((subject) => (
                  <li key={subject}>{subject}</li>
                ))}
              </ul>
              {manualImportPreview.warnings.length ? (
                <details>
                  <summary>
                    {manualImportPreview.warnings.length} avertissement(s)
                  </summary>
                  <ul>
                    {manualImportPreview.warnings.map((warning) => (
                      <li key={warning}>{warning}</li>
                    ))}
                  </ul>
                </details>
              ) : null}
              <div className="action-list">
                <button
                  className="button"
                  disabled={confirmImport.isPending}
                  onClick={() =>
                    confirmImport.mutate({
                      idempotencyKey: crypto.randomUUID(),
                    })
                  }
                >
                  Confirmer et intégrer
                </button>
                <button
                  className="button button--secondary"
                  onClick={() => setManualImportPreview(null)}
                >
                  Modifier
                </button>
              </div>
            </article>
          ) : null}
          {previewImport.isError || confirmImport.isError ? (
            <p role="alert" className="error-message">
              L’import de la réponse ChatGPT a échoué.
            </p>
          ) : null}
        </section>
      ) : null}
      {jobId ? (
        <JobStatusCard
          jobId={jobId}
          readOnly={readOnly}
          onTerminal={handleJobTerminal}
          onReprocessReport={(researchModelRunId) =>
            selectedRun
              ? reprocessReport.mutate({
                  runId: selectedRun.run_id,
                  researchModelRunId,
                  idempotencyKey: crypto.randomUUID(),
                })
              : undefined
          }
        />
      ) : null}
      {!readOnly && jobId && recoveryRunId ? (
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
            {lastJob?.status === "waiting_human" ? (
              <button
                className="button button--secondary"
                disabled={completionRecovery.isPending}
                onClick={() => completionRecovery.mutate()}
              >
                Demander à ChatGPT de terminer
              </button>
            ) : null}
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
        <p>
          <a
            className="button button--secondary"
            download="decouverte.md"
            href={`data:text/markdown;charset=utf-8,${encodeURIComponent(discoveryMarkdownExport)}`}
          >
            Exporter en Markdown
          </a>{" "}
          <span className="stats-caption">
            Même schéma SUBJECT/PUBLICATION que les réponses ChatGPT —
            réutilisable tel quel via « Coller une réponse ChatGPT » pour
            retrouver cet état (sujets, sources, IOC), sans les informations de
            fusion. Seuls les candidats correspondant aux filtres ci-dessous
            sont exportés.
          </span>
        </p>
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
        <div className="candidate-list">
          {candidates.map((candidate) => (
            <article className="candidate-card" key={candidate.id}>
              <div className="candidate-card__heading">
                <h3>{candidate.title}</h3>
                <span>Technique {candidate.technical_potential}/4</span>
              </div>
              <p>
                <strong>Recherche d’origine :</strong>{" "}
                {candidate.discovery_run_id}
                {discoveryRunById.get(candidate.discovery_run_id)
                  ? ` · ${discoveryRunById.get(candidate.discovery_run_id)?.complementary_axis}`
                  : ""}
              </p>
              {candidate.event_date ? (
                <p>
                  <strong>Date de l’événement :</strong> {candidate.event_date}
                </p>
              ) : null}
              {candidate.context_only ? (
                <p>
                  <strong>Contexte uniquement</strong>
                </p>
              ) : null}
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
                      disabled={readOnly || markSource.isPending}
                      onChange={(event) =>
                        markSource.mutate({
                          candidateId: candidate.id,
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
                    {!readOnly ? (
                      <IncompleteSourceUrlForm
                        pending={
                          attachUrl.isPending &&
                          attachUrl.variables?.incompleteSourceId === source.id
                        }
                        error={
                          attachUrl.isError &&
                          attachUrl.variables?.incompleteSourceId === source.id
                            ? attachUrl.error
                            : null
                        }
                        onSubmit={(url) =>
                          attachUrl.mutate({
                            candidateId: candidate.id,
                            incompleteSourceId: source.id,
                            url,
                          })
                        }
                      />
                    ) : null}
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
              {!readOnly ? (
                <div className="editorial-actions">
                  <button
                    className="button button--secondary"
                    disabled={reprocessReport.isPending}
                    onClick={() =>
                      reprocessReport.mutate({
                        runId: batch.discovery_run_id,
                        researchModelRunId: batch.discovery_model_run_id,
                        idempotencyKey: crypto.randomUUID(),
                      })
                    }
                  >
                    Retraiter le rapport archivé
                  </button>
                  <button
                    className="button button--secondary"
                    disabled={launch.isPending || mergeReconciling}
                    onClick={() => {
                      if (
                        window.confirm(
                          "Relancer la recherche web créera une nouvelle conversation ChatGPT et conservera le rapport actuel. Continuer ?",
                        )
                      )
                        launch.mutate({
                          idempotencyKey: crypto.randomUUID(),
                          payload: {
                            complementary_axis: axis.trim() || "initial",
                            source_profile: sourceProfile,
                          },
                        });
                    }}
                  >
                    Relancer la recherche web
                  </button>
                </div>
              ) : null}
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
