import {
  useQueries,
  useQuery,
  type Query,
  type UseQueryResult,
} from "@tanstack/react-query";

import { listSubjects, type Edition, type Subject } from "../../api/editions";
import {
  getSubjectProduction,
  shouldPollProduction,
  type ProductionStatus,
  type StageStatus,
} from "../../api/production";
import { TlpBadge } from "../editions/editionPresentation";
import { Link } from "../../routing";

const stageKeys = [
  "references",
  "extraction",
  "synthesis",
  "assembly",
] as const;
type ProductionQuery = UseQueryResult<ProductionStatus | null>;

const stageStatusLabels: Record<StageStatus["status"], string> = {
  pending: "Non démarrée",
  running: "En cours",
  succeeded: "Terminée",
  needs_review: "Attention",
  failed: "Échec",
  cancelled: "Annulée",
};

function productionLabel(production: ProductionStatus | null): string {
  if (!production) return "Non démarrée";
  if (production.status === "queued" || production.status === "running") {
    return "En cours";
  }
  if (production.status === "needs_review") return "Attention";
  if (production.status === "failed") return "Échec";
  if (production.status === "ready") return "Prête";
  return "Annulée";
}

function stageLabel(stage: ProductionStatus["current_stage"]): string {
  return {
    sources: "Sources",
    references: "Références",
    extraction: "Extraction",
    synthesis: "Synthèse",
    assembly: "Assemblage",
  }[stage];
}

function stageStatusLabel(stage: StageStatus | undefined): string {
  return stage ? stageStatusLabels[stage.status] : "Non démarrée";
}

function productionDiagnostic(production: ProductionStatus): string | null {
  if (production.status !== "failed" && production.status !== "needs_review") {
    return null;
  }
  const currentStage = production.stages[production.current_stage];
  const code = production.error_code ?? currentStage?.error_code;
  const message = production.error_message ?? currentStage?.error_message;
  return (
    [code, message]
      .filter((value): value is string => Boolean(value))
      .join(" — ") || "Diagnostic non communiqué."
  );
}

function Summary({
  subjects,
  productions,
}: {
  subjects: Subject[];
  productions: ProductionQuery[];
}) {
  const counts = productions.reduce(
    (summary, query) => {
      const production = query.data;
      if (!production) {
        if (query.isSuccess) summary.notStarted += 1;
        return summary;
      }
      if (production.status === "queued" || production.status === "running") {
        summary.running += 1;
      } else if (
        production.status === "needs_review" ||
        production.status === "failed"
      ) {
        summary.attention += 1;
      } else if (production.status === "ready") {
        summary.ready += 1;
      }
      return summary;
    },
    { notStarted: 0, running: 0, attention: 0, ready: 0 },
  );

  const items = [
    ["Subjects", subjects.length],
    ["Production non démarrée", counts.notStarted],
    ["En cours", counts.running],
    ["Attention requise", counts.attention],
    ["Prêts", counts.ready],
  ] as const;

  return (
    <dl
      className="edition-dashboard__summary"
      aria-label="Résumé de production"
    >
      {items.map(([label, count]) => (
        <div className="edition-dashboard__summary-card" key={label}>
          <dt>{label}</dt>
          <dd>{count}</dd>
        </div>
      ))}
    </dl>
  );
}

function StageCell({
  production,
  stage,
}: {
  production: ProductionStatus | null | undefined;
  stage: (typeof stageKeys)[number];
}) {
  return (
    <td>
      {production ? stageStatusLabel(production.stages[stage]) : "Non démarrée"}
    </td>
  );
}

function SubjectRow({
  subject,
  productionQuery,
}: {
  subject: Subject;
  productionQuery: ProductionQuery;
}) {
  const production = productionQuery.data;
  const diagnostic = production ? productionDiagnostic(production) : null;
  const overall = productionQuery.isError
    ? "Indisponible"
    : productionQuery.isPending
      ? "Chargement…"
      : productionLabel(production ?? null);

  return (
    <tr>
      <th scope="row">
        <strong>{subject.title}</strong>
        <Link to={`/subjects/${subject.id}`}>Ouvrir le sujet</Link>
      </th>
      <td>
        <TlpBadge tlp={subject.tlp} />
      </td>
      <td>
        <span className="edition-dashboard__status">{overall}</span>
        {productionQuery.isError ? (
          <p className="edition-dashboard__row-error" role="alert">
            État de production indisponible
          </p>
        ) : null}
        {diagnostic ? (
          <p className="edition-dashboard__row-error">{diagnostic}</p>
        ) : null}
      </td>
      <td>
        {productionQuery.isError || productionQuery.isPending
          ? "—"
          : production
            ? stageLabel(production.current_stage)
            : "Non démarrée"}
      </td>
      {stageKeys.map((stage) => (
        <StageCell key={stage} production={production} stage={stage} />
      ))}
    </tr>
  );
}

export function EditionDashboard({ edition }: { edition: Edition }) {
  const subjectsQuery = useQuery({
    queryKey: ["edition-subjects", edition.id],
    queryFn: () => listSubjects(edition.id),
  });
  // Une réponse non conforme au contrat liste ne doit pas faire tomber
  // l’arbre React : elle se projette comme une édition sans sujet.
  const subjects = Array.isArray(subjectsQuery.data) ? subjectsQuery.data : [];
  const productionQueries = useQueries({
    queries: subjects.map((subject) => ({
      queryKey: ["production", subject.id],
      queryFn: (): Promise<ProductionStatus | null> =>
        getSubjectProduction(subject.id),
      refetchInterval: (query: Query<ProductionStatus | null>) =>
        shouldPollProduction(query.state.data?.status) ? 1000 : false,
    })),
  });

  if (subjectsQuery.isPending) {
    return <p role="status">Chargement des sujets…</p>;
  }

  if (subjectsQuery.isError) {
    return (
      <p role="alert" className="error-message">
        Les sujets de cette édition sont indisponibles.
      </p>
    );
  }

  return (
    <section
      className="edition-dashboard"
      aria-labelledby="edition-dashboard-title"
    >
      <div className="edition-dashboard__heading">
        <div>
          <p className="eyebrow">Édition</p>
          <h2 id="edition-dashboard-title">Dashboard Edition</h2>
        </div>
        <p>{edition.country}</p>
      </div>
      <Summary subjects={subjects} productions={productionQueries} />
      {subjects.length === 0 ? (
        <div className="empty-state edition-dashboard__empty">
          <p>Aucun sujet n'a encore été sélectionné pour cette édition.</p>
          {edition.state === "open" ? (
            <nav aria-label="Actions de sélection des sujets">
              <Link to={`/editions/${edition.id}/discovery`}>
                Découvrir des sujets
              </Link>
              <Link to={`/editions/${edition.id}/selection`}>
                Sélectionner les sujets
              </Link>
            </nav>
          ) : null}
        </div>
      ) : (
        <div className="edition-dashboard__table-wrapper">
          <table className="edition-dashboard__table">
            <caption className="sr-only">Sujets et état de production</caption>
            <thead>
              <tr>
                <th scope="col">Sujet</th>
                <th scope="col">TLP</th>
                <th scope="col">Production</th>
                <th scope="col">Étape en cours</th>
                <th scope="col">Références</th>
                <th scope="col">Extraction</th>
                <th scope="col">Synthèse</th>
                <th scope="col">Assemblage</th>
              </tr>
            </thead>
            <tbody>
              {subjects.map((subject, index) => (
                <SubjectRow
                  key={subject.id}
                  subject={subject}
                  productionQuery={productionQueries[index]!}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
