import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useState } from "react";

import {
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
import { EditorialBoard } from "./components/EditorialBoard";
import { SubjectWorkbench } from "./components/SubjectWorkbench";
import { ProductionArtifactView } from "./components/ProductionArtifactView";
import { ErrorMessage } from "./components/ErrorMessage";
import { DiscoveryPanel } from "./features/discovery/DiscoveryPanel";
import { discoveryJobStorageKey } from "./features/discovery/discoveryStorage";

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
  const artifact = pathname.match(
    /^\/subjects\/([^/]+)\/production\/artifacts\/(references|extraction|synthesis|brief)$/,
  );
  return (
    <main>
      <header className="app-header">
        <Link to="/editions">CTI Bulletin</Link>
        <span>Utilisateur local : dev-analyst</span>
      </header>
      {artifact ? (
        <ProductionArtifactView
          subjectId={artifact[1]!}
          stage={
            artifact[2] as "references" | "extraction" | "synthesis" | "brief"
          }
          onClose={() => window.history.back()}
        />
      ) : subject ? (
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
