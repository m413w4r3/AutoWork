import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useState } from "react";

import {
  fetchDiscovery,
  launchDiscovery,
  markDiscoverySource,
  type SourceVerificationStatus,
} from "./api/discovery";
import {
  ApiError,
  createEdition,
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
        <label>
          Articles principaux ciblés
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
          Brèves ciblées
          <input
            name="target_briefs"
            type="number"
            min={0}
            max={100}
            defaultValue={6}
            required
          />
        </label>
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
  const queryClient = useQueryClient();
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
          <dt>Articles principaux</dt>
          <dd>{current.target_major_articles}</dd>
        </div>
        <div>
          <dt>Brèves</dt>
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
      <DiscoveryPanel editionId={current.id} />
      <EditorialBoard editionId={current.id} />
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
    </section>
  );
}

function DiscoveryPanel({ editionId }: { editionId: string }) {
  const queryClient = useQueryClient();
  const [jobId, setJobId] = useState<string | null>(null);
  const [axis, setAxis] = useState("initial");
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
    refetchInterval: jobId ? 2_000 : false,
  });
  const launch = useMutation({
    mutationFn: () => launchDiscovery(editionId, axis.trim() || "initial"),
    onSuccess: (result) => {
      setJobId(result.job_id);
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
  const candidates = discovery.data?.candidates ?? [];
  const batches = discovery.data?.batches ?? [];

  return (
    <section className="discovery-panel" aria-labelledby="discovery-heading">
      <div className="discovery-heading">
        <div>
          <p className="eyebrow">Découverte ponctuelle</p>
          <h2 id="discovery-heading">Sujets candidats</h2>
        </div>
        <button
          className="button"
          disabled={launch.isPending}
          onClick={() => launch.mutate()}
        >
          {launch.isPending ? "Lancement…" : "Rechercher les sujets"}
        </button>
      </div>
      <label className="axis-field">
        Axe de recherche
        <input
          value={axis}
          onChange={(event) => setAxis(event.target.value)}
          placeholder="initial ou axe complémentaire"
        />
      </label>
      <p className="verification-warning" role="note">
        Recherche effectuée depuis les citations visibles de ChatGPT. La liste
        des sources et leurs relations seront vérifiées lors de la collecte.
      </p>
      {launch.error ? (
        <ErrorMessage error={launch.error} fallback="Recherche impossible." />
      ) : null}
      {jobId ? <JobStatusCard jobId={jobId} /> : null}
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
            <p>{candidate.summary}</p>
            <p>
              <strong>Nouveauté :</strong> {candidate.novelty}
            </p>
            <p>
              <strong>Pertinence :</strong>{" "}
              {candidate.relevance_reasons.join(", ")}
            </p>
            <p>
              <strong>Matière probable :</strong>{" "}
              {candidate.likely_artifacts.join(", ") || "non signalée"}
            </p>
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
            <h4>Sources proposées</h4>
            <ul className="source-list">
              {candidate.sources.map((source) => (
                <li key={source.id}>
                  <a href={source.url} target="_blank" rel="noreferrer">
                    {source.title}
                  </a>
                  <span>
                    {source.role} · {source.verification_status}
                  </span>
                  <select
                    aria-label={`État de ${source.title}`}
                    value={source.verification_status}
                    disabled={markSource.isPending}
                    onChange={(event) =>
                      markSource.mutate({
                        sourceId: source.id,
                        status: event.target.value as SourceVerificationStatus,
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
            </ul>
          </article>
        ))}
      </div>
      {batches.map((batch) => (
        <details className="research-trace" key={batch.id}>
          <summary>Requêtes et citations — {batch.complementary_axis}</summary>
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
        </details>
      ))}
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
