import {
  useMutation,
  useQuery,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useState } from "react";

import { attachReplacementSourceUrl } from "../../api/discovery";
import {
  getSubjectProduction,
  retryProductionStage,
  type ProductionStatus,
} from "../../api/production";
import {
  archiveManualSourceContent,
  getSubjectWorkbench,
  type CollectedSource,
  type SubjectWorkbenchResult,
} from "../../api/collection";
import { SubjectProduction } from "../../components/SubjectProduction";

const MANUAL_SOURCE_STATES = new Set([
  "blocked",
  "unavailable",
  "failed_retryable",
  "failed_terminal",
]);

export function PipelineTab({ subjectId }: { subjectId: string }) {
  const production = useQuery({
    queryKey: ["production", subjectId],
    queryFn: () => getSubjectProduction(subjectId),
  });
  const sourceFailure = isSourceCollectionFailure(production.data);
  const editionId = production.data?.edition_id;
  const workbench = useQuery({
    queryKey: ["subject-workbench", subjectId],
    queryFn: () => getSubjectWorkbench(subjectId),
    enabled: sourceFailure && Boolean(editionId),
  });

  return (
    <>
      <SubjectProduction subjectId={subjectId} />
      {sourceFailure && editionId ? (
        <SourceContentPanel
          subjectId={subjectId}
          editionId={editionId}
          workbench={workbench}
          refreshProduction={production.refetch}
        />
      ) : null}
    </>
  );
}

function isSourceCollectionFailure(
  production: ProductionStatus | null | undefined,
): boolean {
  if (!production || production.current_stage !== "sources") return false;
  if (production.status !== "failed" && production.status !== "needs_review") {
    return false;
  }
  const sourceStage = production.stages?.sources;
  return (
    sourceStage?.error_code === "source_collection_no_success" ||
    production.error_code === "source_collection_no_success"
  );
}

function SourceContentPanel({
  subjectId,
  editionId,
  workbench,
  refreshProduction,
}: {
  subjectId: string;
  editionId: string;
  workbench: UseQueryResult<SubjectWorkbenchResult, Error>;
  refreshProduction: () => Promise<unknown>;
}) {
  const [urls, setUrls] = useState<Record<string, string>>({});
  const [files, setFiles] = useState<Record<string, File | null>>({});
  const [contents, setContents] = useState<Record<string, string>>({});
  const [mimeTypes, setMimeTypes] = useState<Record<string, string>>({});
  const [replacedSourceIds, setReplacedSourceIds] = useState<Set<string>>(
    new Set(),
  );
  const [archivedSourceIds, setArchivedSourceIds] = useState<Set<string>>(
    new Set(),
  );
  const manualUpload = useMutation({
    mutationFn: (input: {
      source: CollectedSource;
      content: File | string;
      declaredMimeType: string;
    }) =>
      archiveManualSourceContent(subjectId, input.source.id, {
        content: input.content,
        declaredMimeType: input.declaredMimeType,
      }),
    onSuccess: (_result, input) => {
      setArchivedSourceIds((current) => new Set(current).add(input.source.id));
      void workbench.refetch();
    },
  });
  const replacement = useMutation({
    mutationFn: (input: { source: CollectedSource; url: string }) =>
      attachReplacementSourceUrl(
        editionId,
        subjectId,
        input.source.requested_url,
        input.url,
      ),
    onSuccess: (_result, input) => {
      setReplacedSourceIds((current) => new Set(current).add(input.source.id));
      void workbench.refetch();
    },
  });
  const restart = useMutation({
    mutationFn: () => retryProductionStage(subjectId, "sources"),
    onSuccess: () => {
      void refreshProduction();
      void workbench.refetch();
    },
  });

  if (workbench.isPending) {
    return (
      <section className="source-replacement-panel">
        <h2>Fournir le contenu d'une source</h2>
        <p role="status">Chargement des sources du sujet…</p>
      </section>
    );
  }
  if (workbench.isError) {
    return (
      <section className="source-replacement-panel">
        <h2>Fournir le contenu d'une source</h2>
        <p className="error-message" role="alert">
          Les sources du sujet sont inaccessibles : {String(workbench.error)}
        </p>
      </section>
    );
  }

  const sources = (workbench.data?.sources ?? []).filter((source) =>
    MANUAL_SOURCE_STATES.has(source.state),
  );
  const canRestart = archivedSourceIds.size > 0 || replacedSourceIds.size > 0;
  return (
    <section
      className="source-replacement-panel"
      aria-labelledby="source-replacement-heading"
    >
      <h2 id="source-replacement-heading">Fournir le contenu d'une source</h2>
      <p>
        Le collecteur n’a pas pu récupérer cette page. Enregistrez-la depuis
        votre navigateur (Ctrl+S, « page web complète » non nécessaire : le HTML
        seul suffit) et déposez le fichier ici. Le contenu déposé devient la
        preuve archivée : tous les indicateurs devront y être littéralement
        présents.
      </p>
      <ul className="source-replacement-list">
        {sources.map((source) => {
          const fileInputId = `source-file-${source.id}`;
          const contentInputId = `source-content-${source.id}`;
          const mimeInputId = `source-mime-${source.id}`;
          const urlInputId = `source-url-${source.id}`;
          const pending =
            manualUpload.isPending &&
            manualUpload.variables?.source.id === source.id;
          const replacementPending =
            replacement.isPending &&
            replacement.variables?.source.id === source.id;
          return (
            <li key={source.id} className="is-source-failure">
              <strong>{source.title}</strong>
              <span>État : {source.state}</span>
              <label htmlFor={urlInputId}>URL de la source</label>
              <input
                id={urlInputId}
                type="url"
                readOnly
                value={source.requested_url}
              />
              <form
                className="source-replacement-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  const file = files[source.id];
                  const text = contents[source.id] ?? "";
                  if (!file && !text.trim()) return;
                  manualUpload.mutate({
                    source,
                    content: file ?? text,
                    declaredMimeType:
                      file?.type || mimeTypes[source.id] || "text/html",
                  });
                }}
              >
                <label htmlFor={fileInputId}>Fichier à déposer</label>
                <input
                  id={fileInputId}
                  type="file"
                  accept=".html,.htm,.txt,.md,.pdf,.json"
                  onChange={(event) =>
                    setFiles((current) => ({
                      ...current,
                      [source.id]: event.currentTarget.files?.[0] ?? null,
                    }))
                  }
                />
                <label htmlFor={contentInputId}>Ou coller le contenu</label>
                <textarea
                  id={contentInputId}
                  rows={8}
                  value={contents[source.id] ?? ""}
                  onChange={(event) =>
                    setContents((current) => ({
                      ...current,
                      [source.id]: event.target.value,
                    }))
                  }
                />
                <label htmlFor={mimeInputId}>Type du contenu collé</label>
                <select
                  id={mimeInputId}
                  value={mimeTypes[source.id] ?? "text/html"}
                  onChange={(event) =>
                    setMimeTypes((current) => ({
                      ...current,
                      [source.id]: event.target.value,
                    }))
                  }
                >
                  <option value="text/html">text/html</option>
                  <option value="text/plain">text/plain</option>
                  <option value="application/json">application/json</option>
                  <option value="application/pdf">application/pdf</option>
                </select>
                <button type="submit" className="button" disabled={pending}>
                  {pending ? "Archivage…" : "Archiver ce contenu"}
                </button>
              </form>
              <form
                className="source-replacement-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  const url = (urls[source.id] ?? "").trim();
                  if (!url) return;
                  replacement.mutate({ source, url });
                }}
              >
                <label htmlFor={`replacement-url-${source.id}`}>
                  URL de remplacement
                </label>
                <input
                  id={`replacement-url-${source.id}`}
                  type="url"
                  required
                  value={urls[source.id] ?? ""}
                  onChange={(event) =>
                    setUrls((current) => ({
                      ...current,
                      [source.id]: event.target.value,
                    }))
                  }
                />
                <button
                  type="submit"
                  className="button"
                  disabled={replacementPending}
                >
                  {replacementPending ? "Remplacement…" : "Remplacer"}
                </button>
              </form>
            </li>
          );
        })}
      </ul>
      {manualUpload.error ? (
        <p className="error-message" role="alert">
          Le contenu n’a pas pu être archivé : {String(manualUpload.error)}
        </p>
      ) : null}
      {replacement.error ? (
        <p className="error-message" role="alert">
          L’URL de remplacement n’a pas pu être enregistrée :{" "}
          {String(replacement.error)}
        </p>
      ) : null}
      {canRestart ? (
        <div className="source-replacement-restart">
          <p>Le contenu ou la source de remplacement est enregistré.</p>
          <button
            type="button"
            className="button"
            onClick={() => restart.mutate()}
            disabled={restart.isPending}
          >
            {restart.isPending ? "Relance…" : "Relancer depuis Sources"}
          </button>
        </div>
      ) : null}
      {restart.error ? (
        <p className="error-message" role="alert">
          La relance n’a pas pu démarrer : {String(restart.error)}
        </p>
      ) : null}
    </section>
  );
}
