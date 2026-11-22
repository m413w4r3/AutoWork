import {
  isWorkshopApiError,
  useBootstrapInventory,
  useBootstrapPlan,
  useBootstrapSnapshot,
  useContributions,
  useRunBootstrap,
  type BootstrapCoverageEntryV1,
  type BootstrapDownloadOutcomeV1,
  type BootstrapIocV1,
  type BootstrapPlanV1,
  type BootstrapQuarantinedSampleV1,
  type BootstrapSnapshotIocV1,
  type BootstrapSnapshotOperationV1,
  type BootstrapSnapshotV1,
  type IOCInventoryV1,
  type SampleOrigin,
  type SampleQualification,
  type SampleState,
  type WorkContributionCategory,
  type WorkContributionView,
} from "../api";
import {
  CONTRIBUTION_CATEGORY_LABELS,
  CONTRIBUTION_IMPORT_LABELS,
  COVERAGE_KIND_LABELS,
  DOWNLOAD_STATUS_LABELS,
  INVOCATION_STATUS_LABELS,
  IOC_KIND_LABELS,
  IOC_ORIGIN_LABELS,
  PLAN_STATE_LABELS,
  PLAN_STATE_REASONS,
  REASON_LABELS,
  ROUTE_STATUS_LABELS,
  SAMPLE_ORIGIN_LABELS,
  SAMPLE_QUALIFICATION_LABELS,
  SAMPLE_STATE_LABELS,
  VT_TOOL_LABELS,
  formatTimestamp,
  labelOf,
  operationOutcomeLabel,
  workshopErrorText,
} from "./samplesLabels";
import "./samples.css";

const CHIP_KINDS: Record<WorkContributionCategory, string> = {
  work_contribution: "Note de travail — non probante",
  observation: "Observation — référence canonique",
  finding: "Finding — vérification humaine requise",
};

function ErrorNotice({ label, error }: { label: string; error: unknown }) {
  return (
    <p className="error-message" role="alert">
      {workshopErrorText(error, label)}
    </p>
  );
}

interface SampleRow {
  key: string;
  sha256: string;
  ioc: BootstrapIocV1;
  origin: SampleOrigin | null;
  qualification: SampleQualification | null;
  state: SampleState | null;
  associationId: string | null;
  version: number | null;
  download: BootstrapDownloadOutcomeV1 | null;
}

function buildSampleRows(
  inventory: IOCInventoryV1,
  snapshot: BootstrapSnapshotV1 | undefined,
): SampleRow[] {
  const snapshotByIoc = new Map<string, BootstrapSnapshotIocV1>();
  for (const ioc of snapshot?.iocs ?? []) snapshotByIoc.set(ioc.ioc_id, ioc);
  const rows: SampleRow[] = [];
  const seen = new Set<string>();
  for (const ioc of [...inventory.seeds, ...inventory.discovered]) {
    const download = snapshotByIoc.get(ioc.ioc_id)?.download ?? null;
    const quarantinedByAssociation = new Map<
      string,
      BootstrapQuarantinedSampleV1
    >();
    for (const sample of ioc.quarantined) {
      if (sample.association_id !== null) {
        quarantinedByAssociation.set(sample.association_id, sample);
      }
    }
    for (const association of ioc.associations) {
      const key = `${ioc.ioc_id}:${association.association_id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const quarantined = quarantinedByAssociation.get(
        association.association_id,
      );
      rows.push({
        key,
        sha256: association.sample_sha256,
        ioc,
        origin: association.origin,
        qualification: association.qualification,
        state: quarantined?.state ?? null,
        associationId: association.association_id,
        version: association.version,
        download,
      });
    }
    for (const sample of ioc.quarantined) {
      if (
        sample.association_id !== null &&
        ioc.associations.some(
          (association) => association.association_id === sample.association_id,
        )
      ) {
        continue;
      }
      const key = `${ioc.ioc_id}:quarantine:${sample.sample_id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push({
        key,
        sha256: sample.sha256,
        ioc,
        origin: null,
        qualification: sample.qualification,
        state: sample.state,
        associationId: sample.association_id,
        version: null,
        download,
      });
    }
  }
  return rows;
}

function associationQualifications(
  inventory: IOCInventoryV1 | undefined,
): Map<string, SampleQualification> {
  const map = new Map<string, SampleQualification>();
  if (inventory === undefined) return map;
  for (const ioc of [...inventory.seeds, ...inventory.discovered]) {
    for (const association of ioc.associations) {
      map.set(association.association_id, association.qualification);
    }
  }
  return map;
}

function IocTable({
  title,
  origin,
  iocs,
}: {
  title: string;
  origin: string;
  iocs: BootstrapIocV1[];
}) {
  return (
    <div className="samples-results__subsection">
      <h3>{title}</h3>
      <p className="samples-results__meta">{origin}</p>
      {iocs.length === 0 ? (
        <p>Aucun IOC dans cette catégorie.</p>
      ) : (
        <div className="table-scroll">
          <table>
            <caption className="samples-results__caption">
              {title} — {iocs.length} entrée(s)
            </caption>
            <thead>
              <tr>
                <th scope="col">Type</th>
                <th scope="col">Valeur</th>
                <th scope="col">Alias de hachage</th>
                <th scope="col">Sources / claims</th>
                <th scope="col">Échantillons associés</th>
                <th scope="col">Champs absents / limites</th>
              </tr>
            </thead>
            <tbody>
              {iocs.map((ioc) => (
                <tr key={ioc.ioc_id}>
                  <td>{IOC_KIND_LABELS[ioc.kind]}</td>
                  <td>
                    <code>{ioc.value}</code>
                    {ioc.hash_type === null ? null : (
                      <span className="samples-results__meta">
                        {" "}
                        · hash {ioc.hash_type}
                      </span>
                    )}
                  </td>
                  <td>
                    {ioc.hashes.length === 0 ? (
                      "—"
                    ) : (
                      <ul className="samples-results__list">
                        {ioc.hashes.map((hash) => (
                          <li key={`${hash.hash_type}:${hash.value}`}>
                            <code>{hash.value}</code> ({hash.hash_type})
                          </li>
                        ))}
                      </ul>
                    )}
                  </td>
                  <td>
                    {ioc.sources.length} source(s) · {ioc.claims.length}{" "}
                    claim(s)
                  </td>
                  <td>
                    {ioc.associations.length} association(s) ·{" "}
                    {ioc.quarantined.length} en quarantaine
                  </td>
                  <td>
                    {ioc.limitations.length === 0 ? (
                      "Aucune limite déclarée"
                    ) : (
                      <ul className="samples-results__list">
                        {ioc.limitations.map((limitation) => (
                          <li key={limitation}>{limitation}</li>
                        ))}
                      </ul>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function InventorySection({ inventory }: { inventory: IOCInventoryV1 }) {
  return (
    <section className="samples-results__section" aria-label="Inventaire IOC">
      <h2>Inventaire IOC</h2>
      <p>
        Généré le{" "}
        <time dateTime={inventory.generated_at}>
          {formatTimestamp(inventory.generated_at)}
        </time>
        . Les seeds proviennent de l’article étudié ; les découverts sont des
        pivots à qualifier.
      </p>
      {inventory.limits.truncated ? (
        <p className="samples-results__warn" role="status">
          Inventaire tronqué : {inventory.limits.iocs_considered} IOC
          considérés, {inventory.limits.max_iocs} maximum.
        </p>
      ) : null}
      <IocTable
        title="Seeds — déjà dans l’article"
        origin={IOC_ORIGIN_LABELS.seed}
        iocs={inventory.seeds}
      />
      <IocTable
        title="Découverts — pivots"
        origin={IOC_ORIGIN_LABELS.discovered}
        iocs={inventory.discovered}
      />
      {inventory.limitations.length > 0 ? (
        <div>
          <h3>Limites de l’inventaire</h3>
          <ul className="samples-results__list">
            {inventory.limitations.map((limitation) => (
              <li key={limitation}>{limitation}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

function AcquisitionSection({
  inventory,
  snapshot,
}: {
  inventory: IOCInventoryV1;
  snapshot: BootstrapSnapshotV1 | undefined;
}) {
  const rows = buildSampleRows(inventory, snapshot);
  return (
    <section
      className="samples-results__section"
      aria-label="Acquisition des échantillons"
    >
      <h2>Statut d’acquisition par échantillon</h2>
      <p className="samples-results__meta">
        Statuts canoniques existants uniquement ; une qualification « candidat »
        reste soumise à une décision G3 humaine.
      </p>
      {rows.length === 0 ? (
        <p>Aucun échantillon associé aux IOC de cette enquête.</p>
      ) : (
        <div className="table-scroll">
          <table>
            <caption className="samples-results__caption">
              {rows.length} échantillon(s) associé(s)
            </caption>
            <thead>
              <tr>
                <th scope="col">SHA-256</th>
                <th scope="col">IOC</th>
                <th scope="col">Origine</th>
                <th scope="col">Qualification</th>
                <th scope="col">Quarantaine</th>
                <th scope="col">Téléchargement</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.key}>
                  <td>
                    <code>{row.sha256}</code>
                  </td>
                  <td>
                    {IOC_KIND_LABELS[row.ioc.kind]} ·{" "}
                    <code>{row.ioc.value}</code>
                  </td>
                  <td>
                    {row.origin === null
                      ? "—"
                      : SAMPLE_ORIGIN_LABELS[row.origin]}
                  </td>
                  <td>
                    {row.qualification === null ? (
                      "—"
                    ) : (
                      <span className="badge">
                        {SAMPLE_QUALIFICATION_LABELS[row.qualification]}
                      </span>
                    )}
                  </td>
                  <td>
                    {row.state === null ? "—" : SAMPLE_STATE_LABELS[row.state]}
                  </td>
                  <td>
                    {row.download === null ? (
                      "Non tenté"
                    ) : (
                      <>
                        {labelOf(DOWNLOAD_STATUS_LABELS, row.download.status)}
                        {row.download.hash_verified_quarantine
                          ? " · SHA-256 vérifié"
                          : " · hash non vérifié"}
                        {row.download.reason === null
                          ? null
                          : ` · ${labelOf(REASON_LABELS, row.download.reason)}`}
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function OperationRow({
  operation,
}: {
  operation: BootstrapSnapshotOperationV1;
}) {
  const absent: string[] = [...operation.limitations];
  if (operation.status === "empty") absent.push("Aucun champ observé.");
  if (operation.detail_code !== null)
    absent.push(`Code de détail : ${operation.detail_code}.`);
  if (operation.error_code !== null)
    absent.push(`Code d’erreur : ${operation.error_code}.`);
  return (
    <tr>
      <td>{labelOf(VT_TOOL_LABELS, operation.tool_name)}</td>
      <td>
        <span className="badge">
          {INVOCATION_STATUS_LABELS[operation.status]}
        </span>
        <div className="samples-results__meta">
          {operationOutcomeLabel(operation.status)}
        </div>
      </td>
      <td>
        {operation.reason === null
          ? "—"
          : labelOf(REASON_LABELS, operation.reason)}
      </td>
      <td>{operation.pages === null ? "—" : operation.pages}</td>
      <td>
        {operation.observed_count === null ? "—" : operation.observed_count}
      </td>
      <td>
        {operation.pagination_complete === null
          ? "Inconnue"
          : operation.pagination_complete
            ? "Complète"
            : `Incomplète — curseur ${operation.next_cursor ?? "inconnu"}`}
      </td>
      <td>
        {operation.provider_routes.length === 0
          ? "—"
          : operation.provider_routes.join(", ")}
      </td>
      <td>{operation.observation_refs.length} référence(s)</td>
      <td>
        {absent.length === 0 ? (
          "Aucune limite déclarée"
        ) : (
          <ul className="samples-results__list">
            {absent.map((entry) => (
              <li key={entry}>{entry}</li>
            ))}
          </ul>
        )}
      </td>
    </tr>
  );
}

function CoverageList({ coverage }: { coverage: BootstrapCoverageEntryV1[] }) {
  if (coverage.length === 0) {
    return <p>Aucune limite de couverture déclarée.</p>;
  }
  return (
    <ul className="samples-results__list">
      {coverage.map((entry) => (
        <li key={`${entry.kind}:${entry.target}:${entry.detail}`}>
          <span className="badge">{COVERAGE_KIND_LABELS[entry.kind]}</span>{" "}
          <code>{entry.target}</code> — {entry.detail}
          {entry.blocking ? " (bloquant)" : ""}
        </li>
      ))}
    </ul>
  );
}

function VtReportsSection({
  snapshot,
  plan,
}: {
  snapshot: BootstrapSnapshotV1 | undefined;
  plan: BootstrapPlanV1 | undefined;
}) {
  if (snapshot === undefined) {
    return (
      <section className="samples-results__section" aria-label="Rapports VT">
        <h2>Rapports VT</h2>
        <p>
          Aucun rapport VT enregistré pour l’instant. Les rapports apparaîtront
          après une collecte autorisée par l’enveloppe G2.
        </p>
        {plan !== undefined && plan.operations.length > 0 ? (
          <>
            <h3>Opérations planifiées</h3>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th scope="col">IOC</th>
                    <th scope="col">Outil</th>
                    <th scope="col">Statut</th>
                    <th scope="col">Raison</th>
                    <th scope="col">Route</th>
                    <th scope="col">Unités VT réservées</th>
                    <th scope="col">Limites</th>
                  </tr>
                </thead>
                <tbody>
                  {plan.operations.map((operation) => (
                    <tr key={operation.operation_key}>
                      <td>
                        <code>{operation.ioc_id}</code>
                      </td>
                      <td>{labelOf(VT_TOOL_LABELS, operation.tool_name)}</td>
                      <td>{INVOCATION_STATUS_LABELS[operation.status]}</td>
                      <td>
                        {operation.reason === null
                          ? "—"
                          : labelOf(REASON_LABELS, operation.reason)}
                      </td>
                      <td>{ROUTE_STATUS_LABELS[operation.route_status]}</td>
                      <td>{operation.reserved_vt_units}</td>
                      <td>
                        {operation.limitations.length === 0
                          ? "—"
                          : operation.limitations.join(" · ")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : null}
        {plan !== undefined ? (
          <>
            <h3>Couverture</h3>
            <CoverageList coverage={plan.coverage} />
          </>
        ) : null}
      </section>
    );
  }
  return (
    <section className="samples-results__section" aria-label="Rapports VT">
      <h2>Rapports VT</h2>
      <p>
        Collecte du{" "}
        <time dateTime={snapshot.generated_at}>
          {formatTimestamp(snapshot.generated_at)}
        </time>{" "}
        · état du plan : {PLAN_STATE_LABELS[snapshot.plan_state]}
        {snapshot.envelope_version === null
          ? ""
          : ` · enveloppe G2 v${snapshot.envelope_version}`}
        .
      </p>
      <p className="samples-results__meta">
        {snapshot.limits.iocs_included}/{snapshot.limits.max_iocs} IOC inclus
        {snapshot.limits.inventory_truncated
          ? " (inventaire tronqué)"
          : ""} · {snapshot.limits.operations_recorded}/
        {snapshot.limits.max_operations} opérations enregistrées
        {snapshot.limits.operations_truncated ? " (tronquées)" : ""}
        {snapshot.limits.stopped_reason === null
          ? ""
          : ` · arrêt : ${labelOf(
              REASON_LABELS,
              snapshot.limits.stopped_reason,
            )}`}
        .
      </p>
      {snapshot.iocs.length === 0 ? (
        <p>Aucun IOC n’a été collecté.</p>
      ) : (
        snapshot.iocs.map((ioc) => (
          <article
            className="samples-results__subsection"
            key={ioc.ioc_id}
            aria-label={`Rapports ${ioc.value}`}
          >
            <h3>
              {IOC_KIND_LABELS[ioc.kind]} · <code>{ioc.value}</code>
            </h3>
            <p className="samples-results__badges">
              <span className="badge">{IOC_ORIGIN_LABELS[ioc.origin]}</span>
              <span className="badge">
                {INVOCATION_STATUS_LABELS[ioc.status]}
              </span>
              {ioc.reason === null ? null : (
                <span className="badge">
                  {labelOf(REASON_LABELS, ioc.reason)}
                </span>
              )}
            </p>
            <p className="samples-results__meta">
              {operationOutcomeLabel(ioc.status)}
            </p>
            <div className="table-scroll">
              <table>
                <caption className="samples-results__caption">
                  Opérations VT pour {ioc.value}
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Rapport</th>
                    <th scope="col">Statut / lecture</th>
                    <th scope="col">Raison</th>
                    <th scope="col">Pages</th>
                    <th scope="col">Champs observés</th>
                    <th scope="col">Pagination</th>
                    <th scope="col">Routes</th>
                    <th scope="col">Observations</th>
                    <th scope="col">Champs absents / limites</th>
                  </tr>
                </thead>
                <tbody>
                  {ioc.operations.map((operation) => (
                    <OperationRow
                      key={operation.operation_key}
                      operation={operation}
                    />
                  ))}
                </tbody>
              </table>
            </div>
            {ioc.download === null ? null : (
              <p>
                Téléchargement :{" "}
                {labelOf(DOWNLOAD_STATUS_LABELS, ioc.download.status)}
                {ioc.download.hash_verified_quarantine
                  ? " · SHA-256 vérifié en quarantaine"
                  : " · hash non vérifié"}
                {ioc.download.reason === null
                  ? ""
                  : ` · ${labelOf(REASON_LABELS, ioc.download.reason)}`}
              </p>
            )}
          </article>
        ))
      )}
      <h3>Couverture</h3>
      <CoverageList coverage={snapshot.coverage} />
      {snapshot.limitations.length > 0 ? (
        <>
          <h3>Limites de la collecte</h3>
          <ul className="samples-results__list">
            {snapshot.limitations.map((limitation) => (
              <li key={limitation}>{limitation}</li>
            ))}
          </ul>
        </>
      ) : null}
    </section>
  );
}

function ContributionsSection({
  contributions,
  inventory,
}: {
  contributions: WorkContributionView[];
  inventory: IOCInventoryV1 | undefined;
}) {
  const qualifications = associationQualifications(inventory);
  const hasFinding = contributions.some(
    (contribution) => contribution.category === "finding",
  );
  const hasSampleAssociation = contributions.some(
    (contribution) => contribution.sample_association_id !== null,
  );
  return (
    <section
      className="samples-results__section"
      aria-label="Contributions importées"
    >
      <h2>Contributions importées</h2>
      {contributions.length === 0 ? (
        <p>Aucune contribution importée dans cette enquête.</p>
      ) : (
        <div className="table-scroll">
          <table>
            <caption className="samples-results__caption">
              {contributions.length} contribution(s)
            </caption>
            <thead>
              <tr>
                <th scope="col">Catégorie</th>
                <th scope="col">Fichier source</th>
                <th scope="col">Import</th>
                <th scope="col">Statut probatoire</th>
                <th scope="col">Références</th>
                <th scope="col">Tâche</th>
                <th scope="col">Observé le</th>
              </tr>
            </thead>
            <tbody>
              {contributions.map((contribution) => {
                const qualification =
                  contribution.sample_association_id === null
                    ? undefined
                    : qualifications.get(contribution.sample_association_id);
                return (
                  <tr key={contribution.id}>
                    <td>
                      {CONTRIBUTION_CATEGORY_LABELS[contribution.category]}
                      <div className="samples-results__meta">
                        {CHIP_KINDS[contribution.category]}
                      </div>
                    </td>
                    <td>
                      <code>{contribution.file_relpath}</code>
                    </td>
                    <td>
                      {CONTRIBUTION_IMPORT_LABELS[contribution.import_status]}
                      {contribution.validation_error_code === null ? null : (
                        <div className="samples-results__warn">
                          {contribution.validation_error_code}
                        </div>
                      )}
                    </td>
                    <td>
                      {contribution.citable_as_evidence
                        ? "Citable comme observation canonique"
                        : "Non probant — aucune référence canonique"}
                    </td>
                    <td>
                      {contribution.observation_id === null &&
                      contribution.finding_id === null &&
                      contribution.sample_association_id === null ? (
                        "—"
                      ) : (
                        <ul className="samples-results__list">
                          {contribution.observation_id === null ? null : (
                            <li>observation {contribution.observation_id}</li>
                          )}
                          {contribution.finding_id === null ? null : (
                            <li>finding {contribution.finding_id}</li>
                          )}
                          {contribution.sample_association_id ===
                          null ? null : (
                            <li>
                              échantillon {contribution.sample_association_id}
                              {qualification === undefined
                                ? " — qualification non exposée, décision G3 humaine requise"
                                : ` — ${
                                    SAMPLE_QUALIFICATION_LABELS[qualification]
                                  }`}
                            </li>
                          )}
                        </ul>
                      )}
                    </td>
                    <td>
                      {contribution.task_id === null ? (
                        "—"
                      ) : (
                        <code>{contribution.task_id}</code>
                      )}
                    </td>
                    <td>
                      <time dateTime={contribution.observed_at}>
                        {formatTimestamp(contribution.observed_at)}
                      </time>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {hasFinding ? (
        <p className="samples-results__note">
          Un finding importé n’est jamais automatiquement étayé : son évaluation
          reste une décision serveur et humaine.
        </p>
      ) : null}
      {hasSampleAssociation ? (
        <p className="samples-results__note">
          Un nouvel échantillon reste candidat jusqu’à une confirmation G3
          humaine ; aucune automatisation ne le promeut.
        </p>
      ) : null}
    </section>
  );
}

export function SamplesResultsView({
  investigationId,
}: {
  investigationId: string;
}) {
  const inventory = useBootstrapInventory(investigationId);
  const plan = useBootstrapPlan(investigationId);
  const snapshot = useBootstrapSnapshot(investigationId);
  const contributions = useContributions(investigationId);
  const run = useRunBootstrap(investigationId);

  const snapshotMissing =
    snapshot.error !== null &&
    isWorkshopApiError(snapshot.error) &&
    snapshot.error.code === "bootstrap_snapshot_not_found";
  const snapshotData = snapshotMissing ? undefined : snapshot.data;
  const planData = plan.data;
  const planReady = planData?.state === "ready";

  return (
    <div className="samples-results" aria-label="Échantillons et résultats">
      <section className="samples-results__section" aria-label="Collecte VT">
        <h2>Collecte VT</h2>
        {plan.isPending ? (
          <p role="status">Chargement du plan de collecte…</p>
        ) : null}
        {plan.error !== null ? (
          <ErrorNotice
            label="Le plan de collecte n’a pas pu être lu."
            error={plan.error}
          />
        ) : null}
        {planReady ? (
          <div className="samples-results__action">
            <button
              className="button"
              type="button"
              disabled={run.isPending}
              onClick={() => run.mutate()}
            >
              {run.isPending
                ? "Collecte VT en cours…"
                : snapshotData === undefined
                  ? "Lancer la collecte VT"
                  : "Relancer la collecte VT"}
            </button>
            <span className="samples-results__meta">
              Collecte bornée par l’enveloppe G2 approuvée ; aucune resaisie par
              hash n’est nécessaire.
            </span>
          </div>
        ) : null}
        {planData !== undefined && planData.state !== "ready" ? (
          <p className="samples-results__blocked" role="status">
            {PLAN_STATE_REASONS[planData.state]}
          </p>
        ) : null}
        {run.isPending ? (
          <p role="status">
            Collecte VT en cours — opération longue, les résultats partiels
            restent visibles ci-dessous.
          </p>
        ) : null}
        {run.error !== null ? (
          <ErrorNotice label="La collecte VT a échoué." error={run.error} />
        ) : null}
      </section>

      {inventory.isPending ? (
        <p role="status">Chargement de l’inventaire IOC…</p>
      ) : null}
      {inventory.error !== null ? (
        <ErrorNotice
          label="L’inventaire IOC n’a pas pu être lu."
          error={inventory.error}
        />
      ) : null}
      {inventory.data === undefined ? null : (
        <>
          <InventorySection inventory={inventory.data} />
          <AcquisitionSection
            inventory={inventory.data}
            snapshot={snapshotData}
          />
        </>
      )}

      {snapshot.isPending ? (
        <p role="status">Chargement des rapports VT…</p>
      ) : null}
      {snapshot.error !== null && !snapshotMissing ? (
        <ErrorNotice
          label="Les rapports VT n’ont pas pu être lus."
          error={snapshot.error}
        />
      ) : null}
      {snapshot.isPending ||
      (snapshot.error !== null && !snapshotMissing) ? null : (
        <VtReportsSection snapshot={snapshotData} plan={planData} />
      )}

      {contributions.isPending ? (
        <p role="status">Chargement des contributions…</p>
      ) : null}
      {contributions.error !== null ? (
        <ErrorNotice
          label="Les contributions n’ont pas pu être lues."
          error={contributions.error}
        />
      ) : null}
      {contributions.data === undefined ? null : (
        <ContributionsSection
          contributions={contributions.data}
          inventory={inventory.data}
        />
      )}
    </div>
  );
}
