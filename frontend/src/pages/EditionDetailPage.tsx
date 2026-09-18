import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { archiveEdition, type Edition, getEdition } from "../api/editions";
import { ErrorMessage } from "../components/ErrorMessage";
import {
  StatusBadge,
  TlpBadge,
  formatPeriod,
} from "../features/editions/editionPresentation";
import {
  EditionWorkspace,
  type EditionTool,
} from "../features/edition-tools/EditionWorkspace";
import { Link } from "../routing";

export function EditionDetailPage({
  editionId,
  tool,
}: {
  editionId: string;
  tool?: EditionTool;
}) {
  const queryClient = useQueryClient();
  const edition = useQuery({
    queryKey: ["edition", editionId],
    queryFn: () => getEdition(editionId),
  });
  const archive = useMutation({
    mutationFn: (current: Edition) => archiveEdition(current),
    onSuccess: (updated) => {
      queryClient.setQueryData(["edition", editionId], updated);
      void queryClient.invalidateQueries({ queryKey: ["editions"] });
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
            <StatusBadge state={current.state} />
            <TlpBadge tlp={current.tlp} />
          </div>
        </div>
        <p>Version {current.version}</p>
      </div>
      {/* L’état OPEN / ARCHIVED est déjà porté par le badge ci-dessus. */}
      <dl className="edition-facts">
        <div>
          <dt>Code pays</dt>
          <dd>{current.country_code}</dd>
        </div>
        <div>
          <dt>Langues</dt>
          <dd>{current.languages.join(", ")}</dd>
        </div>
        <div>
          <dt>Période</dt>
          <dd>
            {current.period_start} → {current.period_end}
          </dd>
        </div>
      </dl>
      <EditionWorkspace edition={current} current={tool ?? "overview"} />
      <section className="danger-zone" aria-labelledby="edition-archive">
        <h2 id="edition-archive">Actions secondaires</h2>
        {archive.error ? (
          <ErrorMessage
            error={archive.error}
            fallback="Archivage impossible."
          />
        ) : null}
        {current.state === "open" ? (
          <button
            className="button button--secondary"
            disabled={archive.isPending}
            onClick={() => archive.mutate(current)}
          >
            {archive.isPending ? "Archivage…" : "Archiver l’édition"}
          </button>
        ) : null}
      </section>
    </section>
  );
}
