import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/editions";
import {
  acquireSourceAddition,
  applyTargetedEditorialRevision,
  getTargetedEditorialRevisionLineage,
  previewTargetedEditorialRevision,
  recordSourceAdditionCandidate,
  selectSourceAddition,
  type ArtifactResponse,
  type SourceAdditionExtractionIntent,
  type TargetedEditorialRevisionLineage,
  type TargetedEditorialRevisionMode,
  type TargetedEditorialRevisionOperation,
  type TargetedEditorialRevisionPreview,
  type TargetedEditorialRevisionRequest,
  type TargetedEditorialRevisionTargetOption,
} from "../api/production";

const OPERATION_LABELS: Record<TargetedEditorialRevisionOperation, string> = {
  rewrite_title: "Réécrire le titre",
  rewrite_lead: "Réécrire le chapeau",
  rewrite_paragraph: "Réécrire le paragraphe",
  insert_paragraph: "Insérer un paragraphe",
  delete_paragraph: "Supprimer le paragraphe",
  rewrite_section: "Réécrire la section",
  insert_section: "Créer une section",
  delete_section: "Supprimer la section",
  merge_sections: "Fusionner les sections",
  split_section: "Scinder la section",
  rename_section: "Renommer la section",
  create_table: "Créer un tableau",
  rewrite_table: "Réécrire le tableau",
  delete_table: "Supprimer le tableau",
  create_diagram: "Créer un diagramme",
  rewrite_diagram: "Réécrire le diagramme",
  delete_diagram: "Supprimer le diagramme",
  create_chart: "Créer un graphique",
  rewrite_chart: "Réécrire le graphique",
  delete_chart: "Supprimer le graphique",
  create_source_figure: "Créer une figure source",
  rewrite_source_figure: "Réécrire la figure source",
  replace_source_figure: "Remplacer la figure source",
  delete_source_figure: "Supprimer la figure source",
};

function refusalMessage(refusal: {
  code: string;
  reason: string;
  recommended_restart_stage: string | null;
}): string {
  const restart = refusal.recommended_restart_stage
    ? ` Reprise recommandée : ${refusal.recommended_restart_stage}.`
    : "";
  return `${refusal.code} — ${refusal.reason}${restart}`;
}

function apiErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return String(error);
  if (error.status === 409) {
    return `Conflit de version (${error.code}) : rechargez l’artifact avant de réessayer. ${error.message}`;
  }
  const reasonCode = error.details?.reason_code;
  if (
    error.code === "revision_not_applicable" ||
    reasonCode === "revision_not_applicable"
  ) {
    const reason = error.details?.reason;
    const stage = error.details?.recommended_restart_stage;
    return `Refus typé revision_not_applicable : ${typeof reason === "string" ? reason : error.message}${typeof stage === "string" ? ` Reprise recommandée : ${stage}.` : ""}`;
  }
  return `${error.code}${typeof reasonCode === "string" ? ` (${reasonCode})` : ""} : ${error.message}`;
}

function RevisionLineage({
  lineage,
}: {
  lineage: TargetedEditorialRevisionLineage;
}) {
  const provenance = Object.entries(lineage.paragraph_provenance);
  const elementProvenance = Object.entries(lineage.element_provenance);
  return (
    <section
      className="targeted-revision-lineage"
      aria-label="Lineage des révisions"
    >
      <h4>Historique et provenance</h4>
      {lineage.revisions.length === 0 ? (
        <p>Aucune révision ciblée enregistrée pour cet artifact.</p>
      ) : (
        <ol>
          {lineage.revisions.map((revision) => (
            <li key={revision.revision_id}>
              <p>
                {revision.operation} · {revision.target_path} ·{" "}
                {revision.created_at}
              </p>
              <p>Instruction : {revision.request}</p>
              <p>Artifact créé : {revision.result_artifact_id}</p>
              <p>
                Mode : {revision.mode} · acteur : {revision.actor}
              </p>
              <p>
                Preuves : {revision.evidence_handles.join(", ") || "Aucune"}
              </p>
            </li>
          ))}
        </ol>
      )}
      <details>
        <summary>Provenance par paragraphe ({provenance.length})</summary>
        <pre>{JSON.stringify(Object.fromEntries(provenance), null, 2)}</pre>
      </details>
      <details>
        <summary>Provenance des éléments ({elementProvenance.length})</summary>
        <pre>
          {JSON.stringify(Object.fromEntries(elementProvenance), null, 2)}
        </pre>
      </details>
    </section>
  );
}

function RevisionPreview({
  preview,
}: {
  preview: TargetedEditorialRevisionPreview;
}) {
  return (
    <section className="targeted-revision-impact" aria-label="Preview d’impact">
      <h4>Preview d’impact</h4>
      <dl>
        <dt>Artifact de base</dt>
        <dd>
          {preview.base_artifact_id} · version {preview.base_version}
        </dd>
        <dt>Fences</dt>
        <dd>
          input {preview.base_input_hash} · SHA-256{" "}
          {preview.base_canonical_sha256}
        </dd>
        <dt>Zone visée</dt>
        <dd>
          {preview.target_path} · hash avant{" "}
          {preview.before_hash ?? "indisponible"}
        </dd>
        <dt>Extension de sources</dt>
        <dd>{preview.source_extension ? "Oui" : "Non"}</dd>
        <dt>Appels modèle attendus</dt>
        <dd>{preview.expected_model_calls}</dd>
        <dt>Rebasage enrichissement</dt>
        <dd>{preview.enrichment_rebase}</dd>
      </dl>
      <div>
        <p>
          Stages conservés : {preview.stages_preserved.join(", ") || "Aucun"}
        </p>
        <p>
          Stages invalidés : {preview.stages_invalidated.join(", ") || "Aucun"}
        </p>
        <p>
          Rebasage conditionnel :{" "}
          {preview.stages_conditionally_rebased.join(", ") || "Aucun"}
        </p>
        <p>Stages rebasés : {preview.stages_rebased.join(", ") || "Aucun"}</p>
      </div>
      {preview.expected_model_calls_by_stage ? (
        <details>
          <summary>Appels modèle par stage</summary>
          <pre>
            {JSON.stringify(preview.expected_model_calls_by_stage, null, 2)}
          </pre>
        </details>
      ) : null}
      {preview.source_additions.length > 0 ? (
        <details>
          <summary>Sources ajoutées prévues</summary>
          <pre>{JSON.stringify(preview.source_additions, null, 2)}</pre>
        </details>
      ) : null}
      {preview.source_addition_blockers.length > 0 ? (
        <div className="error-message" role="alert">
          <h5>Bloqueurs de source</h5>
          <ul>
            {preview.source_addition_blockers.map((blocker) => (
              <li key={`${blocker.source_url}-${blocker.repair_key}`}>
                {blocker.source_url} · {blocker.criticality} · {blocker.reason}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {preview.refusal ? (
        <p className="error-message" role="alert">
          {refusalMessage(preview.refusal)}
        </p>
      ) : null}
    </section>
  );
}

export function TargetedEditorialRevisionPanel({
  subjectId,
  stage,
  artifact,
}: {
  subjectId: string;
  stage: "synthesis" | "editorial_enrichment";
  artifact: ArtifactResponse;
}) {
  const queryClient = useQueryClient();
  const targets: TargetedEditorialRevisionTargetOption[] = useMemo(
    () => artifact.revision_targets ?? [],
    [artifact.revision_targets],
  );
  const [targetId, setTargetId] = useState(targets[0]?.id ?? "");
  const [operation, setOperation] = useState<
    TargetedEditorialRevisionOperation | ""
  >(targets[0]?.operations[0] ?? "");
  const [instruction, setInstruction] = useState("");
  const [mode, setMode] = useState<TargetedEditorialRevisionMode>(
    "existing_evidence_only",
  );
  const [sourceUrl, setSourceUrl] = useState("");
  const [sourceExtractionIntent, setSourceExtractionIntent] =
    useState<SourceAdditionExtractionIntent>("indicators_only");
  const [sourceCriterion, setSourceCriterion] = useState(
    "Source fournie par l’opérateur",
  );
  const [sourceRequestId, setSourceRequestId] = useState<string | null>(null);
  const [previewRequest, setPreviewRequest] =
    useState<TargetedEditorialRevisionRequest | null>(null);

  const selectedTarget = targets.find((target) => target.id === targetId);
  useEffect(() => {
    if (!selectedTarget) {
      setTargetId(targets[0]?.id ?? "");
      setOperation(targets[0]?.operations[0] ?? "");
    }
  }, [selectedTarget, targets]);

  const currentLineageQuery = useQuery({
    queryKey: ["targeted-revision-lineage", artifact.artifact_id],
    queryFn: () => getTargetedEditorialRevisionLineage(artifact.artifact_id),
    retry: false,
  });
  const previewMutation = useMutation({
    mutationFn: (body: TargetedEditorialRevisionRequest) =>
      previewTargetedEditorialRevision(subjectId, body),
    onSuccess: (_preview, body) => setPreviewRequest(body),
  });
  const sourceMutation = useMutation({
    mutationFn: async (requestId: string) => {
      if (!artifact.production_run_id)
        throw new Error("L’identifiant du run est indisponible.");
      const body = {
        production_run_id: artifact.production_run_id,
        request_id: requestId,
        url: sourceUrl.trim(),
        extraction_intent: sourceExtractionIntent,
      };
      await recordSourceAdditionCandidate(subjectId, {
        ...body,
        origin: "operator_url",
      });
      await selectSourceAddition(subjectId, {
        ...body,
        criterion: sourceCriterion.trim(),
      });
      return acquireSourceAddition(subjectId, body);
    },
  });
  const applyMutation = useMutation({
    mutationFn: () => {
      const preview = previewMutation.data;
      if (!preview || !previewRequest)
        throw new Error("Le preview est indisponible.");
      const fencedBody: TargetedEditorialRevisionRequest = {
        ...previewRequest,
        base_artifact_id: preview.base_artifact_id,
        base_version: preview.base_version,
        base_input_hash: preview.base_input_hash,
        base_canonical_sha256: preview.base_canonical_sha256,
        before_hash: preview.before_hash,
        source_additions_hash: preview.source_additions_hash,
      };
      return applyTargetedEditorialRevision(subjectId, fencedBody);
    },
    onSuccess: async (result) => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["production-artifact", subjectId, stage],
        }),
        queryClient.invalidateQueries({
          queryKey: ["production-artifact", subjectId, "publication"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["targeted-revision-lineage", artifact.artifact_id],
        }),
      ]);
      if (result.artifact_id) {
        await queryClient.invalidateQueries({
          queryKey: ["targeted-revision-lineage", result.artifact_id],
        });
      }
    },
  });
  const resultLineageQuery = useQuery({
    queryKey: ["targeted-revision-lineage", applyMutation.data?.artifact_id],
    queryFn: () => {
      const artifactId = applyMutation.data?.artifact_id;
      if (!artifactId) throw new Error("Aucun artifact de résultat.");
      return getTargetedEditorialRevisionLineage(artifactId);
    },
    enabled: Boolean(applyMutation.data?.artifact_id),
    retry: false,
  });

  function clearPreview(): void {
    previewMutation.reset();
    applyMutation.reset();
    setPreviewRequest(null);
  }

  function submitPreview(): void {
    if (!selectedTarget || !operation || artifact.canonical_sha256 === null)
      return;
    const body: TargetedEditorialRevisionRequest = {
      base_artifact_id: artifact.artifact_id,
      base_version: artifact.version,
      base_input_hash: artifact.input_hash,
      base_canonical_sha256: artifact.canonical_sha256,
      target: selectedTarget.target,
      operation,
      instruction: instruction.trim(),
      mode,
      ...(mode === "allow_source_extension" && sourceRequestId
        ? { source_addition_request_id: sourceRequestId }
        : {}),
    };
    applyMutation.reset();
    previewMutation.mutate(body);
  }

  const preview = previewMutation.data;
  const result = applyMutation.data;
  const sourceResult = sourceMutation.data;

  return (
    <section className="targeted-revision-panel" aria-label="Révision ciblée">
      <h3>Révision ciblée</h3>
      <p>
        La révision est préparée sur l’artifact courant et appliquée après son
        preview.
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          submitPreview();
        }}
      >
        <label>
          Cible
          <select
            aria-label="Cible de révision"
            value={targetId}
            onChange={(event) => {
              const next = targets.find(
                (item) => item.id === event.target.value,
              );
              setTargetId(event.target.value);
              setOperation(next?.operations[0] ?? "");
              clearPreview();
            }}
          >
            {targets.map((target) => (
              <option key={target.id} value={target.id}>
                {target.label}
              </option>
            ))}
          </select>
        </label>
        {selectedTarget?.summary ? (
          <p>Zone actuelle : {selectedTarget.summary}</p>
        ) : null}
        <label>
          Opération
          <select
            aria-label="Opération de révision"
            value={operation}
            onChange={(event) => {
              setOperation(
                event.target.value as TargetedEditorialRevisionOperation,
              );
              clearPreview();
            }}
          >
            {selectedTarget?.operations.map((item) => (
              <option key={item} value={item}>
                {OPERATION_LABELS[item]}
              </option>
            ))}
          </select>
        </label>
        <label>
          Instruction (≤ 2000 caractères)
          <textarea
            aria-label="Instruction de révision"
            maxLength={2000}
            value={instruction}
            onChange={(event) => {
              setInstruction(event.target.value);
              clearPreview();
            }}
          />
        </label>
        <p>{instruction.length}/2000</p>
        <fieldset>
          <legend>Mode de preuve</legend>
          <label>
            <input
              checked={mode === "existing_evidence_only"}
              name="targeted-revision-mode"
              type="radio"
              value="existing_evidence_only"
              onChange={() => {
                setMode("existing_evidence_only");
                clearPreview();
              }}
            />
            Preuves existantes uniquement
          </label>
          {stage === "synthesis" ? (
            <label>
              <input
                checked={mode === "allow_source_extension"}
                name="targeted-revision-mode"
                type="radio"
                value="allow_source_extension"
                onChange={() => {
                  setMode("allow_source_extension");
                  clearPreview();
                }}
              />
              Autoriser l’extension de sources
            </label>
          ) : null}
        </fieldset>
        {mode === "allow_source_extension" ? (
          <div className="targeted-revision-source-extension">
            <label>
              URL fournie par l’opérateur
              <input
                aria-label="URL de la source opérateur"
                type="url"
                value={sourceUrl}
                onChange={(event) => {
                  setSourceUrl(event.target.value);
                  setSourceRequestId(null);
                  sourceMutation.reset();
                  clearPreview();
                }}
              />
            </label>
            <label>
              Intention d’extraction
              <select
                aria-label="Intention d’extraction de la source"
                value={sourceExtractionIntent}
                onChange={(event) => {
                  setSourceExtractionIntent(
                    event.target.value as SourceAdditionExtractionIntent,
                  );
                  setSourceRequestId(null);
                  sourceMutation.reset();
                  clearPreview();
                }}
              >
                <option value="indicators_only">Indicateurs uniquement</option>
                <option value="descriptive_facts">Faits descriptifs</option>
              </select>
            </label>
            <label>
              Motif de sélection
              <input
                aria-label="Motif de sélection de la source"
                value={sourceCriterion}
                onChange={(event) => setSourceCriterion(event.target.value)}
              />
            </label>
            <button
              className="button button--secondary"
              disabled={
                sourceMutation.isPending ||
                !sourceUrl.trim() ||
                !sourceCriterion.trim()
              }
              type="button"
              onClick={() => {
                const requestId =
                  sourceRequestId ?? globalThis.crypto.randomUUID();
                setSourceRequestId(requestId);
                clearPreview();
                sourceMutation.mutate(requestId);
              }}
            >
              {sourceMutation.isPending
                ? "Archivage et extraction…"
                : "Archiver et préparer la source"}
            </button>
            <p>
              Si l’accès est bloqué, l’API expose l’archivage manuel du contenu
              opérateur avant le preview.
            </p>
            {sourceResult ? (
              <p role="status">
                État source ·{" "}
                {typeof sourceResult.addition.state === "string"
                  ? sourceResult.addition.state
                  : "état retourné par l’API"}
                {sourceResult.refusal
                  ? ` · ${sourceResult.refusal.code} : ${sourceResult.refusal.reason}`
                  : ""}
              </p>
            ) : null}
            {sourceMutation.isError ? (
              <p className="error-message" role="alert">
                {apiErrorMessage(sourceMutation.error)}
              </p>
            ) : null}
          </div>
        ) : null}
        <button
          className="button button--secondary"
          disabled={
            previewMutation.isPending ||
            !selectedTarget ||
            !operation ||
            !instruction.trim() ||
            artifact.canonical_sha256 === null ||
            (mode === "allow_source_extension" && !sourceRequestId)
          }
          type="submit"
        >
          {previewMutation.isPending
            ? "Calcul du preview…"
            : "Prévisualiser l’impact"}
        </button>
      </form>
      {previewMutation.isError ? (
        <p className="error-message" role="alert">
          {apiErrorMessage(previewMutation.error)}
        </p>
      ) : null}
      {preview ? <RevisionPreview preview={preview} /> : null}
      {preview &&
      !preview.refusal &&
      preview.source_addition_blockers.length === 0 &&
      !(preview.source_extension && preview.source_addition_ready === false) ? (
        <button
          className="button button--primary"
          disabled={applyMutation.isPending}
          type="button"
          onClick={() => applyMutation.mutate()}
        >
          {applyMutation.isPending
            ? "Application…"
            : "Appliquer cette révision"}
        </button>
      ) : null}
      {applyMutation.isError ? (
        <p className="error-message" role="alert">
          {apiErrorMessage(applyMutation.error)}
        </p>
      ) : null}
      {result?.refusal ? (
        <p className="error-message" role="alert">
          {refusalMessage(result.refusal)}
        </p>
      ) : null}
      {result?.artifact_id ? (
        <section
          className="targeted-revision-result"
          aria-label="Résultat de la révision"
        >
          <h4>Révision appliquée · version {result.artifact_version}</h4>
          <p>Nouvel artifact : {result.artifact_id}</p>
          {resultLineageQuery.data ? (
            <RevisionLineage lineage={resultLineageQuery.data} />
          ) : null}
          {resultLineageQuery.isLoading ? (
            <p>Chargement du lineage du résultat…</p>
          ) : null}
          {resultLineageQuery.isError ? (
            <p className="error-message" role="alert">
              {apiErrorMessage(resultLineageQuery.error)}
            </p>
          ) : null}
        </section>
      ) : null}
      <details className="targeted-revision-history">
        <summary>Historique du lineage de l’artifact courant</summary>
        {currentLineageQuery.data ? (
          <RevisionLineage lineage={currentLineageQuery.data} />
        ) : null}
        {currentLineageQuery.isLoading ? (
          <p>Chargement de l’historique…</p>
        ) : null}
        {currentLineageQuery.isError ? (
          <p className="error-message" role="alert">
            {apiErrorMessage(currentLineageQuery.error)}
          </p>
        ) : null}
      </details>
    </section>
  );
}
