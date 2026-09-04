import {
  useMutation,
  useQuery,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useState } from "react";

import { attachReplacementSourceUrl } from "../../api/discovery";
import {
  getSubjectProduction,
  restartProductionWithNewSources,
  type ProductionStatus,
} from "../../api/production";
import {
  getSubjectWorkbench,
  type CollectedSource,
  type SubjectWorkbenchResult,
} from "../../api/collection";
import { SubjectProduction } from "../../components/SubjectProduction";

const REPLACEMENT_SOURCE_STATES = new Set([
  "blocked",
  "unavailable",
  "failed_retryable",
  "failed_terminal",
]);
const HIGHLIGHTED_SOURCE_STATES = new Set([
  "blocked",
  "unavailable",
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
        <SourceReplacementPanel
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

function SourceReplacementPanel({
  editionId,
  subjectId,
  workbench,
  refreshProduction,
}: {
  subjectId: string;
  editionId: string;
  workbench: UseQueryResult<SubjectWorkbenchResult, Error>;
  refreshProduction: () => Promise<unknown>;
}) {
  const [urls, setUrls] = useState<Record<string, string>>({});
  const [replacedSourceIds, setReplacedSourceIds] = useState<Set<string>>(
    new Set(),
  );
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
    mutationFn: () => restartProductionWithNewSources(subjectId),
    onSuccess: () => {
      void refreshProduction();
      void workbench.refetch();
    },
  });

  if (workbench.isPending) {
    return (
      <section className="source-replacement-panel">
        <h2>Remplacer une source inaccessible</h2>
        <p role="status">Chargement des sources du sujet…</p>
      </section>
    );
  }
  if (workbench.isError) {
    return (
      <section className="source-replacement-panel">
        <h2>Remplacer une source inaccessible</h2>
        <p className="error-message" role="alert">
          Les sources du sujet sont inaccessibles : {String(workbench.error)}
        </p>
      </section>
    );
  }

  const sources = workbench.data?.sources ?? [];
  const canRestart = replacedSourceIds.size > 0;
  return (
    <section
      className="source-replacement-panel"
      aria-labelledby="source-replacement-heading"
    >
      <h2 id="source-replacement-heading">Remplacer une source inaccessible</h2>
      <p>
        Les sources restent attachées à ce sujet uniquement. Entrez une URL de
        remplacement pour chaque source qui n’a pas pu être archivée.
      </p>
      <ul className="source-replacement-list">
        {sources.map((source) => {
          const replaceable = REPLACEMENT_SOURCE_STATES.has(source.state);
          const highlighted = HIGHLIGHTED_SOURCE_STATES.has(source.state);
          const inputId = `replacement-url-${source.id}`;
          const pending =
            replacement.isPending &&
            replacement.variables?.source.id === source.id;
          return (
            <li
              key={source.id}
              className={highlighted ? "is-source-failure" : undefined}
            >
              <strong>{source.title}</strong>
              <span>État : {source.state}</span>
              <a href={source.requested_url}>{source.requested_url}</a>
              {replaceable ? (
                <form
                  className="source-replacement-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    const url = (urls[source.id] ?? "").trim();
                    if (!url) return;
                    replacement.mutate({ source, url });
                  }}
                >
                  <label htmlFor={inputId}>Nouvelle URL</label>
                  <input
                    id={inputId}
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
                  <button type="submit" className="button" disabled={pending}>
                    {pending ? "Remplacement…" : "Remplacer"}
                  </button>
                </form>
              ) : null}
            </li>
          );
        })}
      </ul>
      {replacement.error ? (
        <p className="error-message" role="alert">
          Le remplacement n’a pas pu être enregistré :{" "}
          {String(replacement.error)}
        </p>
      ) : null}
      {canRestart ? (
        <div className="source-replacement-restart">
          <p>La nouvelle URL est enregistrée dans la découverte.</p>
          <button
            type="button"
            className="button"
            onClick={() => restart.mutate()}
            disabled={restart.isPending}
          >
            {restart.isPending ? "Relance…" : "Relancer la production"}
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
