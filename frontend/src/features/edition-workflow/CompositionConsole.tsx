import {
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useMemo, useState } from "react";

import {
  getEditionComposition,
  listSubjects,
  putEditionComposition,
  type CompositionDisposition,
  type EditionComposition,
  type EditionCompositionEntryInput,
  type EditionCompositionPut,
  type Subject,
} from "../../api/editions";
import {
  acceptEditionComposition,
  excludeCompositionArtifact,
  getEditionCompositionValidation,
} from "../../api/publication";
import { ApiError } from "../../api/editions";
import {
  getEditionProduction,
  getPublicationArtifact,
  getSubjectProductionRuns,
  type ArtifactResponse,
  type ProductionBoard,
  type ProductionRunSummary,
} from "../../api/production";
import { EditionPreviewPanel } from "./EditionPreviewPanel";

interface CompositionEdit {
  entries: EditionCompositionEntryInput[];
  expectedVersion: number;
}

interface CompositionCandidate {
  subject: Subject;
  productionStatus: string;
  productionRunId: string | null;
  artifact: ArtifactResponse | null;
  artifactRun: ProductionRunSummary | null;
  verifiedArtifactReady: boolean;
  latestBatchId: string | null;
  artifactBatchId: string | null;
}

function hashLabel(value: string | null): string {
  return value ? `${value.slice(0, 12)}…` : "indisponible";
}

function runStateLabel(value: string | null | undefined): string {
  if (!value) return "Non démarrée";
  const labels: Record<string, string> = {
    queued: "En attente",
    running: "En cours",
    ready: "Prête",
    needs_review: "À revoir",
    failed: "Échec",
    cancelled: "Annulée",
  };
  return labels[value] ?? value;
}

function entryInput(
  entry: EditionComposition["entries"][number],
): EditionCompositionEntryInput {
  return {
    subject_id: entry.subject_id,
    disposition: entry.disposition,
    position: entry.position,
    production_run_id: entry.production_run_id,
    pipeline_generation: entry.pipeline_generation,
    artifact_id: entry.artifact_id,
    artifact_version: entry.artifact_version,
    artifact_input_hash: entry.artifact_input_hash,
    provenance_batch_id: entry.provenance_batch_id,
    review_decision_id: entry.review_decision_id,
    document_sha256: entry.document_sha256,
  };
}

function orderedEntries(
  entries: EditionCompositionEntryInput[],
): EditionCompositionEntryInput[] {
  const publicationOrder = entries
    .filter((entry) => entry.disposition === "included")
    .slice()
    .sort(
      (left, right) =>
        (left.position ?? Number.MAX_SAFE_INTEGER) -
        (right.position ?? Number.MAX_SAFE_INTEGER),
    );
  const positions = new Map(
    publicationOrder.map((entry, index) => [entry.subject_id, index + 1]),
  );
  return entries.map((entry) => ({
    ...entry,
    position:
      entry.disposition === "included"
        ? (positions.get(entry.subject_id) ?? null)
        : null,
  }));
}

function batchForRun(
  board: ProductionBoard | undefined,
  runId: string | null,
): string | null {
  if (!board || !runId) return null;
  const batches = [board.active_batch, ...board.recent_batches].filter(
    (batch) => batch !== null,
  );
  return (
    batches.find((batch) =>
      batch.item_details?.some((item) => item.run_id === runId),
    )?.batch_id ?? null
  );
}

function apiErrorText(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) {
    const actualVersion = error.details?.actual_version;
    const versionDetail =
      typeof actualVersion === "number"
        ? ` La version courante est ${actualVersion}.`
        : "";
    return `La composition a changé pendant votre action. Elle a été rechargée depuis l’API ; votre modification n’a pas été réappliquée.${versionDetail}`;
  }
  return error instanceof Error
    ? error.message
    : "La composition n’a pas pu être mise à jour.";
}

function validationErrorText(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) {
    return "La version de composition est périmée. Rechargez la composition et vérifiez les diagnostics avant de réessayer.";
  }
  return error instanceof Error
    ? error.message
    : "L’acceptation finale n’a pas pu être envoyée.";
}

function diagnosticDetails(
  details: Record<string, unknown> | null,
): string | null {
  if (!details || Object.keys(details).length === 0) return null;
  return JSON.stringify(details);
}

export function CompositionConsole({
  editionId,
  readOnly = false,
}: {
  editionId: string;
  readOnly?: boolean;
}) {
  const queryClient = useQueryClient();
  const [exclusionReasons, setExclusionReasons] = useState<
    Record<string, string>
  >({});
  const [excludingSubjectId, setExcludingSubjectId] = useState<string | null>(
    null,
  );
  const [pendingDecisionIds, setPendingDecisionIds] = useState<
    Record<string, string>
  >({});
  const [isExclusionPending, setIsExclusionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmedVersion, setConfirmedVersion] = useState<number | null>(null);
  const [showPreview, setShowPreview] = useState(false);

  const subjectsQuery = useQuery({
    queryKey: ["edition-subjects", editionId],
    queryFn: () => listSubjects(editionId),
  });
  const compositionQuery = useQuery({
    queryKey: ["edition-composition", editionId],
    queryFn: () => getEditionComposition(editionId),
  });
  const validationQuery = useQuery({
    queryKey: ["edition-composition-validation", editionId],
    queryFn: () => getEditionCompositionValidation(editionId),
  });
  const productionBoardQuery = useQuery({
    queryKey: ["production-board", editionId],
    queryFn: () => getEditionProduction(editionId),
  });
  const subjects = useMemo(
    () => (Array.isArray(subjectsQuery.data) ? subjectsQuery.data : []),
    [subjectsQuery.data],
  );
  const runQueries = useQueries({
    queries: subjects.map((subject) => ({
      queryKey: ["subject-production-runs", subject.id],
      queryFn: () => getSubjectProductionRuns(subject.id),
      enabled: Boolean(
        productionBoardQuery.data?.subjects.find(
          (item) => item.subject_id === subject.id,
        )?.latest_run_id,
      ),
      retry: false,
    })),
  });
  const artifactQueries = useQueries({
    queries: subjects.map((subject) => ({
      queryKey: ["subject-publication-artifact", subject.id],
      queryFn: () => getPublicationArtifact(subject.id),
      enabled: Boolean(
        productionBoardQuery.data?.subjects.find(
          (item) => item.subject_id === subject.id,
        )?.latest_run_id,
      ),
      retry: false,
    })),
  });

  const candidates = useMemo(() => {
    const board = productionBoardQuery.data;
    return subjects.map((subject, index): CompositionCandidate => {
      const productionSubject = board?.subjects.find(
        (item) => item.subject_id === subject.id,
      );
      const runs = runQueries[index]?.data ?? [];
      const artifact = artifactQueries[index]?.data ?? null;
      const artifactRun = artifact?.production_run_id
        ? (runs.find((run) => run.run_id === artifact.production_run_id) ??
          null)
        : null;
      const productionRunId = productionSubject?.latest_run_id ?? null;
      const ready = Boolean(
        artifact &&
        artifact.status === "verified" &&
        artifact.canonical_sha256 &&
        artifact.production_run_id &&
        artifactRun,
      );
      return {
        subject,
        productionStatus: board
          ? runStateLabel(productionSubject?.latest_status)
          : productionBoardQuery.isError
            ? "Indisponible"
            : "Chargement",
        productionRunId,
        artifact,
        artifactRun,
        verifiedArtifactReady: ready,
        latestBatchId: batchForRun(board, productionRunId),
        artifactBatchId: batchForRun(
          board,
          artifact?.production_run_id ?? null,
        ),
      };
    });
  }, [
    artifactQueries,
    productionBoardQuery.data,
    productionBoardQuery.isError,
    runQueries,
    subjects,
  ]);

  const composition = compositionQuery.data?.composition ?? null;
  const existingEntries = composition?.entries.map(entryInput) ?? [];
  const currentVersion = composition?.version ?? 0;

  const invalidateComposition = () => {
    void queryClient.invalidateQueries({
      queryKey: ["edition-composition", editionId],
    });
    void queryClient.invalidateQueries({
      queryKey: ["edition-composition-validation", editionId],
    });
    void queryClient.invalidateQueries({
      queryKey: ["edition-composition-preview", editionId],
    });
    void queryClient.invalidateQueries({
      queryKey: ["edition-release", editionId],
    });
  };

  const updateComposition = useMutation({
    mutationFn: (edit: CompositionEdit) => {
      const payload: EditionCompositionPut = {
        expected_version: edit.expectedVersion,
        entries: orderedEntries(edit.entries),
      };
      return putEditionComposition(editionId, payload, crypto.randomUUID());
    },
    retry: false,
    onSuccess: () => {
      setActionError(null);
      setConfirmedVersion(null);
      invalidateComposition();
    },
    onError: (error) => {
      setActionError(apiErrorText(error));
      if (error instanceof ApiError && error.status === 409) {
        setConfirmedVersion(null);
        invalidateComposition();
      }
    },
  });

  const accept = useMutation({
    mutationFn: (version: number) =>
      acceptEditionComposition(editionId, version),
    retry: false,
    onSuccess: () => {
      setConfirmedVersion(null);
      invalidateComposition();
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        setConfirmedVersion(null);
        invalidateComposition();
      }
    },
  });

  if (
    subjectsQuery.isPending ||
    compositionQuery.isPending ||
    validationQuery.isPending
  ) {
    return <p role="status">Chargement de la composition…</p>;
  }
  if (
    subjectsQuery.isError ||
    compositionQuery.isError ||
    validationQuery.isError
  ) {
    const error =
      subjectsQuery.error ?? compositionQuery.error ?? validationQuery.error;
    return (
      <p className="error-message" role="alert">
        La composition éditoriale est inaccessible : {String(error)}
      </p>
    );
  }
  if (!compositionQuery.data || !validationQuery.data) return null;

  const compositionRead = compositionQuery.data;
  const validation = validationQuery.data;
  const canEdit = !readOnly && compositionRead.axes.composition_eligibility;
  const mutationPending = updateComposition.isPending || isExclusionPending;
  const entries = composition?.entries ?? [];
  const included = entries
    .filter((entry) => entry.disposition === "included")
    .slice()
    .sort((left, right) => (left.position ?? 0) - (right.position ?? 0));
  const entryBySubject = new Map(
    entries.map((entry) => [entry.subject_id, entry]),
  );
  const displayedCandidates = candidates.slice().sort((left, right) => {
    const leftPosition = entryBySubject.get(left.subject.id)?.position;
    const rightPosition = entryBySubject.get(right.subject.id)?.position;
    if (leftPosition && rightPosition) return leftPosition - rightPosition;
    if (leftPosition) return -1;
    if (rightPosition) return 1;
    return left.subject.title.localeCompare(right.subject.title, "fr");
  });
  const canAccept =
    !readOnly &&
    Boolean(composition) &&
    validation.can_accept &&
    confirmedVersion === currentVersion;

  const saveEntries = (nextEntries: EditionCompositionEntryInput[]) =>
    updateComposition.mutate({
      entries: orderedEntries(nextEntries),
      expectedVersion: currentVersion,
    });

  const setDisposition = (
    subjectId: string,
    disposition: CompositionDisposition,
    artifact?: CompositionCandidate["artifact"],
    artifactRun?: CompositionCandidate["artifactRun"],
    reviewDecisionId?: string,
  ) => {
    const rest = existingEntries.filter(
      (entry) => entry.subject_id !== subjectId,
    );
    if (disposition === "included" && artifact && artifactRun) {
      rest.push({
        subject_id: subjectId,
        disposition,
        position: included.length + 1,
        production_run_id: artifact.production_run_id,
        pipeline_generation: artifactRun.pipeline_generation,
        artifact_id: artifact.artifact_id,
        artifact_version: artifact.version,
        artifact_input_hash: artifact.input_hash,
        provenance_batch_id: batchForRun(
          productionBoardQuery.data,
          artifact.production_run_id ?? null,
        ),
        review_decision_id: null,
        document_sha256: artifact.canonical_sha256,
      });
    } else if (disposition === "excluded") {
      rest.push({
        subject_id: subjectId,
        disposition,
        position: null,
        review_decision_id: reviewDecisionId ?? null,
      });
    } else {
      rest.push({
        subject_id: subjectId,
        disposition: "deferred",
        position: null,
      });
    }
    saveEntries(rest);
  };

  const moveIncluded = (subjectId: string, offset: -1 | 1) => {
    const from = included.findIndex((entry) => entry.subject_id === subjectId);
    const to = from + offset;
    if (from < 0 || to < 0 || to >= included.length) return;
    const next = included.slice();
    [next[from], next[to]] = [next[to]!, next[from]!];
    const orderedIds = new Map(
      next.map((entry, index) => [entry.subject_id, index + 1]),
    );
    saveEntries(
      existingEntries.map((entry) => ({
        ...entry,
        position:
          entry.disposition === "included"
            ? (orderedIds.get(entry.subject_id) ?? null)
            : null,
      })),
    );
  };

  const handleExclude = async (candidate: CompositionCandidate) => {
    const reason = exclusionReasons[candidate.subject.id]?.trim() ?? "";
    if (!reason) return;
    const currentEntry = entryBySubject.get(candidate.subject.id);
    const pinnedIdentity =
      currentEntry?.disposition === "included" &&
      currentEntry.production_run_id &&
      currentEntry.pipeline_generation !== null &&
      currentEntry.artifact_id &&
      currentEntry.artifact_version &&
      currentEntry.artifact_input_hash
        ? {
            productionRunId: currentEntry.production_run_id,
            pipelineGeneration: currentEntry.pipeline_generation,
            artifactId: currentEntry.artifact_id,
            artifactVersion: currentEntry.artifact_version,
            inputHash: currentEntry.artifact_input_hash,
          }
        : null;
    const artifact = candidate.artifact;
    const run = candidate.artifactRun;
    if (
      !pinnedIdentity &&
      (!artifact ||
        !run ||
        !artifact.production_run_id ||
        !artifact.canonical_sha256)
    ) {
      setActionError(
        "Aucun artifact exact et vérifié ne peut porter cette décision.",
      );
      return;
    }
    setActionError(null);
    setIsExclusionPending(true);
    try {
      let decisionId = pendingDecisionIds[candidate.subject.id];
      if (!decisionId) {
        const decision = await excludeCompositionArtifact(
          editionId,
          {
            subjectId: candidate.subject.id,
            productionRunId:
              pinnedIdentity?.productionRunId ?? artifact!.production_run_id!,
            pipelineGeneration:
              pinnedIdentity?.pipelineGeneration ?? run!.pipeline_generation,
            artifactId: pinnedIdentity?.artifactId ?? artifact!.artifact_id,
            artifactVersion:
              pinnedIdentity?.artifactVersion ?? artifact!.version,
            inputHash: pinnedIdentity?.inputHash ?? artifact!.input_hash,
          },
          reason,
        );
        decisionId = decision.id;
        setPendingDecisionIds((current) => ({
          ...current,
          [candidate.subject.id]: decision.id,
        }));
      }
      const next = existingEntries.filter(
        (entry) => entry.subject_id !== candidate.subject.id,
      );
      next.push({
        subject_id: candidate.subject.id,
        disposition: "excluded",
        position: null,
        review_decision_id: decisionId,
      });
      await updateComposition.mutateAsync({
        entries: orderedEntries(next),
        expectedVersion: currentVersion,
      });
      setPendingDecisionIds((current) => {
        const updated = { ...current };
        delete updated[candidate.subject.id];
        return updated;
      });
      setExclusionReasons((current) => ({
        ...current,
        [candidate.subject.id]: "",
      }));
      setExcludingSubjectId(null);
    } catch (error) {
      setActionError(apiErrorText(error));
    } finally {
      setIsExclusionPending(false);
      setExcludingSubjectId(null);
    }
  };

  const discoveryUpdateAvailable =
    compositionRead.axes.discovery_update_available === true;

  return (
    <section
      className="composition-console"
      aria-labelledby="composition-title"
    >
      <header className="composition-console__heading">
        <div>
          <p className="eyebrow">Composition éditoriale</p>
          <h2 id="composition-title">Sujets et ordre de publication</h2>
        </div>
        <p>
          {composition
            ? `Version ${composition.version} · mise à jour le ${new Intl.DateTimeFormat("fr-FR", { dateStyle: "medium", timeStyle: "short" }).format(new Date(composition.updated_at))}`
            : "Aucune composition enregistrée"}
        </p>
      </header>

      {discoveryUpdateAvailable ? (
        <aside className="composition-console__warning" role="note">
          Une mise à jour Discovery est disponible. Cet avertissement ne bloque
          pas l’acceptation de la composition.
        </aside>
      ) : null}

      <section aria-labelledby="composition-reasons-title">
        <h3 id="composition-reasons-title">
          État et diagnostics de composition
        </h3>
        <p>
          {compositionRead.axes.composition_eligibility
            ? "La composition peut être modifiée."
            : "La composition n’est pas éligible à une modification pour le moment."}
        </p>
        <p>
          Validité de la production :{" "}
          {compositionRead.axes.production_validity === null
            ? "non évaluée"
            : compositionRead.axes.production_validity
              ? "valide"
              : "à corriger"}
        </p>
        {compositionRead.reasons.length > 0 ? (
          <ul className="composition-console__diagnostics">
            {compositionRead.reasons.map((reason, index) => (
              <li
                key={`${reason.code}-${reason.subject_id ?? "edition"}-${index}`}
              >
                <code>{reason.code}</code> — {reason.message}
                {reason.subject_id ? (
                  <span> · sujet {reason.subject_id}</span>
                ) : null}
                {diagnosticDetails(reason.details) ? (
                  <details>
                    <summary>Détails structurés</summary>
                    <pre>{diagnosticDetails(reason.details)}</pre>
                  </details>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p>Aucun motif de composition signalé.</p>
        )}
      </section>

      {actionError ? (
        <div className="error-message" role="alert">
          <p>{actionError}</p>
          {actionError.includes("version de composition") ||
          actionError.includes("La composition a changé") ? (
            <button
              type="button"
              className="button button--secondary"
              disabled={compositionQuery.isFetching}
              onClick={() => {
                void compositionQuery.refetch();
                void validationQuery.refetch();
              }}
            >
              {compositionQuery.isFetching
                ? "Rechargement…"
                : "Recharger la composition"}
            </button>
          ) : null}
        </div>
      ) : null}

      {subjectsQuery.data.length === 0 ? (
        <p>Aucun sujet sélectionné dans cette édition.</p>
      ) : (
        <div className="composition-console__subjects">
          {displayedCandidates.map((candidate) => {
            const subjectId = candidate.subject.id;
            const entry = entryBySubject.get(subjectId);
            const position = entry?.position;
            const currentPinMatches = Boolean(
              entry?.disposition === "included" &&
              candidate.artifact?.artifact_id === entry.artifact_id &&
              candidate.artifact.version === entry.artifact_version &&
              candidate.artifact.input_hash === entry.artifact_input_hash &&
              candidate.artifact.production_run_id === entry.production_run_id,
            );
            const canInclude =
              candidate.verifiedArtifactReady && canEdit && !mutationPending;
            const batchLabel = candidate.latestBatchId
              ? candidate.latestBatchId.slice(0, 8)
              : "non renseigné";
            const blockerDiagnostics =
              validation.diagnostics?.filter(
                (diagnostic) => diagnostic.subject_id === subjectId,
              ) ?? [];
            const validationItem = validation.items.find(
              (item) => item.subject_id === subjectId,
            );
            return (
              <article className="composition-console__subject" key={subjectId}>
                <div className="composition-console__subject-heading">
                  <div>
                    <h4>{candidate.subject.title}</h4>
                    <p>
                      Production : {candidate.productionStatus} · batch de
                      production courante : {batchLabel}
                    </p>
                  </div>
                  {entry ? (
                    <span className="composition-console__disposition">
                      {entry.disposition === "included"
                        ? `Inclus · position ${position ?? "?"}`
                        : entry.disposition === "deferred"
                          ? "Reporté"
                          : "Exclu"}
                    </span>
                  ) : (
                    <span className="composition-console__disposition">
                      Non traité
                    </span>
                  )}
                </div>

                {entry?.disposition === "included" ? (
                  <dl className="composition-console__pin">
                    <div>
                      <dt>Run épinglé</dt>
                      <dd>{entry.production_run_id ?? "indisponible"}</dd>
                    </div>
                    <div>
                      <dt>Génération</dt>
                      <dd>{entry.pipeline_generation ?? "indisponible"}</dd>
                    </div>
                    <div>
                      <dt>Artifact</dt>
                      <dd>
                        {entry.artifact_id ?? "indisponible"} · v
                        {entry.artifact_version ?? "?"}
                      </dd>
                    </div>
                    <div>
                      <dt>Hash d’entrée</dt>
                      <dd>
                        <code>{hashLabel(entry.artifact_input_hash)}</code>
                      </dd>
                    </div>
                    <div>
                      <dt>Hash du document</dt>
                      <dd>
                        <code>{hashLabel(entry.document_sha256)}</code>
                      </dd>
                    </div>
                    <div>
                      <dt>Batch épinglé</dt>
                      <dd>
                        {entry.provenance_batch_id ?? "provenance absente"}
                      </dd>
                    </div>
                  </dl>
                ) : entry?.disposition === "excluded" ? (
                  <p>
                    Décision motivée :{" "}
                    <code>{entry.review_decision_id ?? "absente"}</code>
                  </p>
                ) : null}

                <p className="composition-console__artifact-status">
                  Artifact disponible :{" "}
                  {candidate.artifact?.status ?? "non disponible"}
                  {candidate.artifact
                    ? ` · run ${candidate.artifact.production_run_id ?? "inconnu"} · version ${candidate.artifact.version} · hash ${hashLabel(candidate.artifact.input_hash)} · batch ${candidate.artifactBatchId ?? "non renseigné"}`
                    : ""}
                </p>
                {entry?.disposition === "included" &&
                validationItem?.superseded_artifact_available ? (
                  <p className="composition-console__warning" role="status">
                    Une nouvelle version d’artifact est disponible. Le pin reste
                    inchangé jusqu’à votre action explicite.
                  </p>
                ) : null}
                {blockerDiagnostics.map((diagnostic, index) => (
                  <p
                    className="composition-console__diagnostic"
                    key={`${diagnostic.code}-${index}`}
                  >
                    <code>{diagnostic.code}</code>
                    {diagnostic.details
                      ? ` · ${JSON.stringify(diagnostic.details)}`
                      : ""}
                  </p>
                ))}

                {canEdit ? (
                  <div className="composition-console__actions">
                    <button
                      type="button"
                      className="button button--secondary"
                      disabled={!canInclude || currentPinMatches}
                      onClick={() =>
                        setDisposition(
                          subjectId,
                          "included",
                          candidate.artifact,
                          candidate.artifactRun ?? undefined,
                        )
                      }
                    >
                      {entry?.disposition === "included"
                        ? "Épingler la version disponible"
                        : "Inclure"}
                    </button>
                    <button
                      type="button"
                      className="button button--secondary"
                      disabled={mutationPending}
                      onClick={() => setDisposition(subjectId, "deferred")}
                    >
                      Reporter
                    </button>
                    <button
                      type="button"
                      className="button button--secondary"
                      disabled={mutationPending}
                      onClick={() => {
                        setExcludingSubjectId(
                          excludingSubjectId === subjectId ? null : subjectId,
                        );
                        setActionError(null);
                      }}
                    >
                      Exclure…
                    </button>
                    {entry?.disposition === "included" ? (
                      <>
                        <button
                          type="button"
                          className="button button--secondary"
                          aria-label={`Monter ${candidate.subject.title}`}
                          disabled={mutationPending || position === 1}
                          onClick={() => moveIncluded(subjectId, -1)}
                        >
                          Monter
                        </button>
                        <button
                          type="button"
                          className="button button--secondary"
                          aria-label={`Descendre ${candidate.subject.title}`}
                          disabled={
                            mutationPending || position === included.length
                          }
                          onClick={() => moveIncluded(subjectId, 1)}
                        >
                          Descendre
                        </button>
                      </>
                    ) : null}
                  </div>
                ) : null}
                {excludingSubjectId === subjectId && canEdit ? (
                  <div className="composition-console__exclude">
                    <label htmlFor={`exclude-reason-${subjectId}`}>
                      Motif obligatoire de l’exclusion
                    </label>
                    <textarea
                      id={`exclude-reason-${subjectId}`}
                      value={exclusionReasons[subjectId] ?? ""}
                      maxLength={500}
                      onChange={(event) =>
                        setExclusionReasons((current) => ({
                          ...current,
                          [subjectId]: event.target.value,
                        }))
                      }
                    />
                    <button
                      type="button"
                      className="button"
                      disabled={
                        !exclusionReasons[subjectId]?.trim() ||
                        updateComposition.isPending ||
                        isExclusionPending
                      }
                      onClick={() => void handleExclude(candidate)}
                    >
                      Confirmer l’exclusion motivée
                    </button>
                  </div>
                ) : null}
              </article>
            );
          })}
        </div>
      )}
      {productionBoardQuery.isError ? (
        <p className="error-message" role="alert">
          L’état de production par sujet est indisponible. Aucun artifact ne
          peut être ajouté tant que l’API ne confirme pas son run.
        </p>
      ) : null}

      <section
        className="composition-console__validation"
        aria-labelledby="composition-validation-title"
      >
        <h3 id="composition-validation-title">Validation avant acceptation</h3>
        <p role="status">
          {validation.can_accept
            ? "Aucun blocage de validation signalé par l’API."
            : "L’API signale des blocages. L’acceptation finale reste désactivée."}
        </p>
        {validation.diagnostics && validation.diagnostics.length > 0 ? (
          <ul className="composition-console__diagnostics">
            {validation.diagnostics.map((diagnostic, index) => (
              <li
                key={`${diagnostic.code}-${diagnostic.subject_id ?? "edition"}-${index}`}
              >
                <code>{diagnostic.code}</code>
                {diagnostic.subject_id
                  ? ` · sujet ${diagnostic.subject_id}`
                  : ""}
                {diagnostic.disposition ? ` · ${diagnostic.disposition}` : ""}
                {diagnostic.run_id ? ` · run ${diagnostic.run_id}` : ""}
                {diagnostic.details ? (
                  <details>
                    <summary>Détails structurés</summary>
                    <pre>{JSON.stringify(diagnostic.details)}</pre>
                  </details>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}
        {!readOnly ? (
          <>
            <button
              type="button"
              className="button button--secondary"
              disabled={mutationPending}
              onClick={() => setShowPreview((current) => !current)}
            >
              {showPreview
                ? "Masquer l’aperçu"
                : "Prévisualiser la composition (aperçu potentiellement partiel)"}
            </button>
            {showPreview ? (
              <EditionPreviewPanel editionId={editionId} compositionPreview />
            ) : null}
            <label className="composition-console__confirmation">
              <input
                type="checkbox"
                checked={confirmedVersion === currentVersion}
                disabled={!validation.can_accept || !composition}
                onChange={(event) =>
                  setConfirmedVersion(
                    event.target.checked ? currentVersion : null,
                  )
                }
              />
              Je confirme l’acceptation finale de la composition version{" "}
              {currentVersion}.
            </label>
            {accept.error ? (
              <p className="error-message" role="alert">
                {validationErrorText(accept.error)}
              </p>
            ) : null}
            <button
              type="button"
              className="button"
              disabled={!canAccept || accept.isPending}
              onClick={() => accept.mutate(currentVersion)}
            >
              {accept.isPending ? "Acceptation…" : "Accepter la composition"}
            </button>
          </>
        ) : (
          <p>Cette composition est en lecture seule.</p>
        )}
      </section>
    </section>
  );
}
