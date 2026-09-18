import {
  useQueries,
  useQuery,
  type UseQueryResult,
} from "@tanstack/react-query";

import { listSubjects, type Edition, type Subject } from "../../api/editions";
import type { ProductionStatus, StageStatus } from "../../api/production";
import { STAGE_LABELS } from "../production/productionLabels";
import { subjectProductionQuery } from "../production/subjectProductionQuery";
import { TlpBadge } from "../editions/editionPresentation";
import { Link } from "../../routing";

/**
 * Columns of the dashboard: a projection of the current ProductionRun, never a
 * property of the Subject. `sources` is left out on purpose — it would add a
 * column without telling the reader anything actionable.
 */
const STAGE_COLUMNS = [
  "references",
  "extraction",
  "synthesis",
  "assembly",
] as const;

type StageColumn = (typeof STAGE_COLUMNS)[number];
type ProductionQuery = UseQueryResult<ProductionStatus | null>;

/** Display grouping of the existing statuses; the domain values are untouched. */
type ProductionBucket =
  "notStarted" | "running" | "attention" | "ready" | "cancelled";

const NOT_STARTED = "Non démarrée";

const STAGE_STATUS_LABELS: Record<StageStatus["status"], string> = {
  pending: NOT_STARTED,
  running: "En cours",
  succeeded: "Terminée",
  needs_review: "Attention",
  failed: "Échec",
  cancelled: "Annulée",
};

const BUCKET_LABELS: Record<ProductionBucket, string> = {
  notStarted: "Production non démarrée",
  running: "En cours",
  attention: "Attention requise",
  ready: "Prêts",
  cancelled: "Annulées",
};

function productionBucket(
  production: ProductionStatus | null,
): ProductionBucket {
  if (!production) return "notStarted";
  switch (production.status) {
    case "queued":
    case "running":
      return "running";
    case "needs_review":
    case "failed":
      return "attention";
    case "ready":
      return "ready";
    case "cancelled":
      return "cancelled";
  }
}

function productionLabel(production: ProductionStatus | null): string {
  if (!production) return NOT_STARTED;
  if (production.status === "needs_review") return "Attention";
  if (production.status === "failed") return "Échec";
  if (production.status === "ready") return "Prête";
  if (production.status === "cancelled") return "Annulée";
  return "En cours";
}

function stageDiagnostic(stage: StageStatus | undefined): string {
  return [stage?.error_code, stage?.error_message]
    .filter((value): value is string => Boolean(value))
    .join(" — ");
}

function productionDiagnostics(production: ProductionStatus): string[] {
  const diagnostics: string[] = [];
  // Le diagnostic global reprend à défaut les métadonnées du stage courant :
  // lui seul peut donc produire un doublon exact.
  let duplicatedStage: ProductionStatus["current_stage"] | null = null;

  if (production.status === "failed" || production.status === "needs_review") {
    const currentStage = production.stages[production.current_stage];
    const code = production.error_code ?? currentStage?.error_code;
    const message = production.error_message ?? currentStage?.error_message;
    const diagnostic = [code, message]
      .filter((value): value is string => Boolean(value))
      .join(" — ");
    diagnostics.push(diagnostic || "Diagnostic non communiqué.");
    if (diagnostic && diagnostic === stageDiagnostic(currentStage)) {
      duplicatedStage = production.current_stage;
    }
  }

  for (const stage of STAGE_COLUMNS) {
    if (stage === duplicatedStage) continue;
    const diagnostic = stageDiagnostic(production.stages[stage]);
    if (!diagnostic) continue;
    diagnostics.push(`${STAGE_LABELS[stage]} : ${diagnostic}`);
  }

  return diagnostics;
}

/**
 * A row only claims "non démarrée" for an answered `null`: while the request is
 * in flight, or once it failed, the dashboard says so instead of inventing an
 * operational state.
 */
type RowProjection =
  | { kind: "loading" }
  | { kind: "unavailable" }
  | { kind: "known"; production: ProductionStatus | null };

function rowProjection(query: ProductionQuery): RowProjection {
  if (query.isError) return { kind: "unavailable" };
  if (query.isSuccess) return { kind: "known", production: query.data };
  return { kind: "loading" };
}

function overallLabel(projection: RowProjection): string {
  if (projection.kind === "loading") return "Chargement…";
  if (projection.kind === "unavailable") return "Indisponible";
  return productionLabel(projection.production);
}

function currentStageLabel(projection: RowProjection): string {
  if (projection.kind !== "known") return "—";
  const production = projection.production;
  return production ? STAGE_LABELS[production.current_stage] : NOT_STARTED;
}

function stageLabel(projection: RowProjection, stage: StageColumn): string {
  if (projection.kind === "loading") return "Chargement…";
  if (projection.kind === "unavailable") return "Indisponible";
  const stageStatus = projection.production?.stages[stage];
  return stageStatus ? STAGE_STATUS_LABELS[stageStatus.status] : NOT_STARTED;
}

function Summary({
  subjectCount,
  productions,
}: {
  subjectCount: number;
  productions: ProductionQuery[];
}) {
  const counts: Record<ProductionBucket, number> = {
    notStarted: 0,
    running: 0,
    attention: 0,
    ready: 0,
    cancelled: 0,
  };
  for (const query of productions) {
    // Une requête en vol ou en échec ne compte dans aucun groupe : la ligne
    // correspondante l'annonce déjà, et deviner ici fausserait le total.
    if (!query.isSuccess) continue;
    counts[productionBucket(query.data)] += 1;
  }

  return (
    <dl
      className="edition-dashboard__summary"
      aria-label="Résumé de production"
    >
      <div className="edition-dashboard__summary-card">
        <dt>Subjects</dt>
        <dd>{subjectCount}</dd>
      </div>
      {(Object.keys(BUCKET_LABELS) as ProductionBucket[]).map((bucket) => (
        <div className="edition-dashboard__summary-card" key={bucket}>
          <dt>{BUCKET_LABELS[bucket]}</dt>
          <dd>{counts[bucket]}</dd>
        </div>
      ))}
    </dl>
  );
}

function SubjectRow({
  subject,
  productionQuery,
}: {
  subject: Subject;
  productionQuery: ProductionQuery;
}) {
  const projection = rowProjection(productionQuery);
  const diagnostics =
    projection.kind === "known" && projection.production
      ? productionDiagnostics(projection.production)
      : [];

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
        <span className="edition-dashboard__status">
          {overallLabel(projection)}
        </span>
        {projection.kind === "unavailable" ? (
          <p className="edition-dashboard__row-error" role="alert">
            État de production indisponible
          </p>
        ) : null}
        {diagnostics.map((diagnostic) => (
          <p className="edition-dashboard__row-error" key={diagnostic}>
            {diagnostic}
          </p>
        ))}
      </td>
      <td>{currentStageLabel(projection)}</td>
      {STAGE_COLUMNS.map((stage) => (
        <td key={stage}>{stageLabel(projection, stage)}</td>
      ))}
    </tr>
  );
}

export function EditionDashboard({ edition }: { edition: Edition }) {
  const subjectsQuery = useQuery({
    queryKey: ["edition-subjects", edition.id],
    queryFn: () => listSubjects(edition.id),
  });
  // useQueries doit recevoir une liste stable même si l’API renvoie un
  // payload non conforme. Cette valeur ne devient jamais un état métier.
  const subjects = Array.isArray(subjectsQuery.data) ? subjectsQuery.data : [];
  const productionQueries = useQueries({
    queries: subjects.map((subject) => subjectProductionQuery(subject.id)),
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

  if (!Array.isArray(subjectsQuery.data)) {
    return (
      <p role="alert" className="error-message">
        Les sujets de cette édition ont renvoyé des données invalides.
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
          <h2 id="edition-dashboard-title">Vue d’ensemble des sujets</h2>
        </div>
        <p>{edition.country}</p>
      </div>
      <Summary subjectCount={subjects.length} productions={productionQueries} />
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
        // Le tableau déborde horizontalement sur petit écran : le conteneur
        // doit donc être atteignable et défilable au clavier seul.
        <div
          className="edition-dashboard__table-wrapper"
          role="region"
          aria-label="Sujets et état de production"
          tabIndex={0}
        >
          <table className="edition-dashboard__table">
            <caption className="sr-only">Sujets et état de production</caption>
            <thead>
              <tr>
                <th scope="col">Sujet</th>
                <th scope="col">TLP</th>
                <th scope="col">Production</th>
                <th scope="col">Étape en cours</th>
                {STAGE_COLUMNS.map((stage) => (
                  <th scope="col" key={stage}>
                    {STAGE_LABELS[stage]}
                  </th>
                ))}
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
