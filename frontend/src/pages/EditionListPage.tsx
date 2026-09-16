import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { type EditionStatus, listEditions } from "../api/editions";
import { ErrorMessage } from "../components/ErrorMessage";
import {
  StatusBadge,
  TlpBadge,
  formatPeriod,
  statusLabels,
} from "../features/editions/editionPresentation";
import { Link, navigate } from "../routing";

type EditionFilters = {
  countryCode: string;
  period: string;
  state: EditionStatus | "";
};

function isEditionState(value: string): value is EditionStatus {
  return Object.prototype.hasOwnProperty.call(statusLabels, value);
}

function readEditionFilters(search: string): EditionFilters {
  const parameters = new URLSearchParams(search);
  const countryCode = parameters.get("country_code") ?? "";
  const period = parameters.get("period") ?? "";
  const state = parameters.get("state") ?? "";

  return {
    countryCode: /^[A-Za-z]{2}$/.test(countryCode)
      ? countryCode.toUpperCase()
      : "",
    period: /^\d{4}-(0[1-9]|1[0-2])$/.test(period) ? period : "",
    state: isEditionState(state) ? state : "",
  };
}

function replaceEditionFilterQuery(filters: EditionFilters) {
  const parameters = new URLSearchParams(window.location.search);
  const filterParameters: Array<[string, string]> = [
    ["country_code", filters.countryCode],
    ["period", filters.period],
    ["state", filters.state],
  ];
  for (const [name, value] of filterParameters) {
    if (value) {
      parameters.set(name, value);
    } else {
      parameters.delete(name);
    }
  }
  const query = parameters.toString();
  const updatedPath = `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`;
  window.history.replaceState(window.history.state, "", updatedPath);
}

export function EditionListPage() {
  const [filters, setFilters] = useState<EditionFilters>(() =>
    readEditionFilters(window.location.search),
  );
  const { countryCode, period, state } = filters;

  useEffect(() => {
    const updateFiltersFromLocation = () => {
      setFilters(readEditionFilters(window.location.search));
    };
    window.addEventListener("popstate", updateFiltersFromLocation);
    return () =>
      window.removeEventListener("popstate", updateFiltersFromLocation);
  }, []);

  const updateFilters = (nextFilters: Partial<EditionFilters>) => {
    const next = { ...filters, ...nextFilters };
    setFilters(next);
    replaceEditionFilterQuery(next);
  };

  const editions = useQuery({
    queryKey: ["editions", countryCode, period, state],
    queryFn: () => listEditions({ countryCode, period, state }),
  });

  return (
    <>
      <section className="page-heading">
        <div>
          <p className="eyebrow">Pilotage mensuel</p>
          <h1>Éditions</h1>
          <p>Créez et consultez les éditions mensuelles.</p>
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
              updateFilters({
                countryCode: event.target.value.toUpperCase(),
              })
            }
          />
        </label>
        <label>
          Période
          <input
            type="month"
            value={period}
            onChange={(event) => updateFilters({ period: event.target.value })}
          />
        </label>
        <label>
          État
          <select
            value={state}
            onChange={(event) =>
              updateFilters({
                state: event.target.value as EditionStatus | "",
              })
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
                <StatusBadge state={edition.state} />
                <TlpBadge tlp={edition.tlp} />
              </div>
              <h2>{edition.country}</h2>
              <p>{formatPeriod(edition.period_start)}</p>
              <Link to={`/editions/${edition.id}`}>Ouvrir l’édition</Link>
            </article>
          ))}
        </section>
      ) : null}
    </>
  );
}
