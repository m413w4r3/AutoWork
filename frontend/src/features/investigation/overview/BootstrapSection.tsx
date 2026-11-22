/**
 * Vue d’ensemble — inventaire IOC et snapshot de bootstrap.
 *
 * Les lectures sont bornées : l’inventaire complet et le snapshot restent
 * canoniques côté serveur, l’écran n’en affiche qu’un extrait daté avec les
 * limites déclarées par l’API.
 */

import { toWorkshopApiError } from "../api/client";
import {
  useBootstrapInventory,
  useBootstrapSnapshot,
  type BootstrapIocV1,
  type BootstrapSnapshotIocV1,
  type BootstrapSnapshotV1,
  type IOCInventoryV1,
} from "../api/bootstrap";
import {
  WorkshopLoadingState,
  WorkshopPartialState,
  WorkshopStateBanner,
  WorkshopTypedErrorState,
} from "../shell/WorkshopStates";
import {
  DOWNLOAD_STATUS_LABELS,
  INVOCATION_STATUS_LABELS,
  labelOf,
} from "../samples/samplesLabels";
import {
  COVERAGE_KIND_LABELS,
  IOC_KIND_LABELS,
  PLAN_STATE_LABELS,
  REASON_LABELS,
  bounded,
  formatCount,
} from "./overviewPresentation";

const MAX_ROWS = 10;
const MAX_LIMITATIONS = 10;

export function BootstrapSection({
  investigationId,
}: {
  investigationId: string;
}) {
  const inventoryQuery = useBootstrapInventory(investigationId);
  const snapshotQuery = useBootstrapSnapshot(investigationId);

  return (
    <section
      className="job-card workshop-bootstrap"
      aria-labelledby="overview-bootstrap-heading"
    >
      <h2 id="overview-bootstrap-heading">Bootstrap et inventaire IOC</h2>
      <InventoryPanel query={inventoryQuery} />
      <SnapshotPanel query={snapshotQuery} />
    </section>
  );
}

function InventoryPanel({
  query,
}: {
  query: ReturnType<typeof useBootstrapInventory>;
}) {
  if (query.isPending)
    return <WorkshopLoadingState label="Chargement de l’inventaire IOC…" />;
  if (query.isError)
    return (
      <WorkshopTypedErrorState
        error={toWorkshopApiError(query.error)}
        title="Inventaire IOC indisponible"
        onRetry={() => void query.refetch()}
      />
    );
  if (!query.data) return null;
  return <InventoryDetails inventory={query.data} />;
}

function InventoryDetails({ inventory }: { inventory: IOCInventoryV1 }) {
  const seeds = bounded(inventory.seeds, MAX_ROWS);
  const discovered = bounded(inventory.discovered, MAX_ROWS);
  return (
    <section aria-labelledby="overview-inventory-heading">
      <h3 id="overview-inventory-heading">Inventaire IOC</h3>
      <dl className="workshop-meta">
        <dt>Schéma</dt>
        <dd>{inventory.schema_version}</dd>
        <dt>Généré le</dt>
        <dd>
          <time dateTime={inventory.generated_at}>
            {inventory.generated_at}
          </time>
        </dd>
        <dt>Synthèse épinglée</dt>
        <dd>
          <code>{inventory.pinned_synthesis_sha256}</code>
        </dd>
        <dt>Workspace lié</dt>
        <dd>{inventory.workspace_binding_id ?? "Aucun"}</dd>
        <dt>Limites</dt>
        <dd>
          {formatCount(inventory.limits.iocs_considered)} IOC considérés sur{" "}
          {formatCount(inventory.limits.max_iocs)} ·{" "}
          {inventory.limits.truncated
            ? "inventaire tronqué"
            : "inventaire complet"}
        </dd>
      </dl>
      <InventoryIocList
        title={`Graines (${formatCount(inventory.seeds.length)})`}
        items={seeds.visible}
        hidden={seeds.hidden}
      />
      <InventoryIocList
        title={`Découverts (${formatCount(inventory.discovered.length)})`}
        items={discovered.visible}
        hidden={discovered.hidden}
      />
      <Limitations items={inventory.limitations} />
    </section>
  );
}

function InventoryIocList({
  title,
  items,
  hidden,
}: {
  title: string;
  items: readonly BootstrapIocV1[];
  hidden: number;
}) {
  if (items.length === 0) {
    return (
      <section aria-label={title}>
        <h4>{title}</h4>
        <p>Aucun IOC dans cette catégorie.</p>
      </section>
    );
  }
  return (
    <section aria-label={title}>
      <h4>{title}</h4>
      <ul>
        {items.map((ioc) => (
          <li key={ioc.ioc_id}>
            <p>
              {IOC_KIND_LABELS[ioc.kind]} · <code>{ioc.value}</code> ·{" "}
              {ioc.origin === "seed" ? "graine" : "découvert"}
              {ioc.hash_type ? ` · type ${ioc.hash_type}` : ""}
            </p>
            <p>
              {formatCount(ioc.associations.length)} association(s) ·{" "}
              {formatCount(ioc.quarantined.length)} quarantaine(s) ·{" "}
              {formatCount(ioc.hashes.length)} hash alias ·{" "}
              {formatCount(ioc.sources.length)} source(s) ·{" "}
              {formatCount(ioc.claims.length)} affirmation(s)
            </p>
          </li>
        ))}
      </ul>
      {hidden > 0 ? (
        <p role="note">
          {formatCount(hidden)} autre(s) IOC non affiché(s) dans cette vue
          bornée ; l’inventaire canonique reste complet côté serveur.
        </p>
      ) : null}
    </section>
  );
}

function SnapshotPanel({
  query,
}: {
  query: ReturnType<typeof useBootstrapSnapshot>;
}) {
  if (query.isPending)
    return <WorkshopLoadingState label="Chargement du snapshot bootstrap…" />;
  if (query.isError) {
    const error = toWorkshopApiError(query.error);
    if (error.code === "bootstrap_snapshot_not_found") {
      return (
        <WorkshopStateBanner tone="info" title="Aucun snapshot bootstrap">
          <p>
            Le lot de lectures VT bornées n’a pas encore été exécuté pour cette
            enquête. Aucun résultat n’est simulé.
          </p>
        </WorkshopStateBanner>
      );
    }
    return (
      <WorkshopTypedErrorState
        error={error}
        title="Snapshot bootstrap indisponible"
        onRetry={() => void query.refetch()}
      />
    );
  }
  if (!query.data) return null;
  return <SnapshotDetails snapshot={query.data} />;
}

function SnapshotDetails({ snapshot }: { snapshot: BootstrapSnapshotV1 }) {
  const iocs = bounded(snapshot.iocs, MAX_ROWS);
  const coverage = bounded(snapshot.coverage, MAX_ROWS);
  return (
    <section aria-labelledby="overview-snapshot-heading">
      <h3 id="overview-snapshot-heading">Snapshot bootstrap</h3>
      <dl className="workshop-meta">
        <dt>Snapshot</dt>
        <dd>
          <code>{snapshot.snapshot_id}</code>
        </dd>
        <dt>Généré le</dt>
        <dd>
          <time dateTime={snapshot.generated_at}>{snapshot.generated_at}</time>
        </dd>
        <dt>État du plan</dt>
        <dd>{PLAN_STATE_LABELS[snapshot.plan_state]}</dd>
        <dt>Enveloppe G2</dt>
        <dd>
          {snapshot.envelope_version === null
            ? "Aucune version approuvée"
            : `Version ${snapshot.envelope_version}`}
        </dd>
        <dt>Empreintes</dt>
        <dd>
          inventaire <code>{snapshot.inventory_sha256}</code> · plan{" "}
          <code>{snapshot.plan_sha256}</code>
        </dd>
        <dt>Limites</dt>
        <dd>
          {formatCount(snapshot.limits.iocs_included)} IOC inclus sur{" "}
          {formatCount(snapshot.limits.max_iocs)} ·{" "}
          {formatCount(snapshot.limits.operations_recorded)} opérations sur{" "}
          {formatCount(snapshot.limits.max_operations)}
          {snapshot.limits.inventory_truncated ||
          snapshot.limits.operations_truncated
            ? " · snapshot tronqué"
            : " · snapshot complet"}
        </dd>
        {snapshot.limits.stopped_reason ? (
          <>
            <dt>Arrêt</dt>
            <dd>{REASON_LABELS[snapshot.limits.stopped_reason]}</dd>
          </>
        ) : null}
      </dl>

      <section aria-labelledby="overview-snapshot-iocs-heading">
        <h4 id="overview-snapshot-iocs-heading">
          IOC traités ({formatCount(snapshot.iocs.length)})
        </h4>
        {snapshot.iocs.length === 0 ? (
          <p>Aucun IOC n’a été traité dans ce snapshot.</p>
        ) : (
          <ul>
            {iocs.visible.map((ioc) => (
              <SnapshotIoc key={ioc.ioc_id} ioc={ioc} />
            ))}
          </ul>
        )}
        {iocs.hidden > 0 ? (
          <p role="note">
            {formatCount(iocs.hidden)} autre(s) IOC non affiché(s) dans cette
            vue bornée.
          </p>
        ) : null}
      </section>

      <section aria-labelledby="overview-snapshot-coverage-heading">
        <h4 id="overview-snapshot-coverage-heading">Couverture déclarée</h4>
        {snapshot.coverage.length === 0 ? (
          <p>Aucune limite de couverture n’est déclarée.</p>
        ) : (
          <ul>
            {coverage.visible.map((entry) => (
              <li key={`${entry.kind}-${entry.target}`}>
                <strong>
                  {COVERAGE_KIND_LABELS[entry.kind]}
                  {entry.blocking ? " · bloquant" : ""}
                </strong>{" "}
                — {entry.target} : {entry.detail}
              </li>
            ))}
          </ul>
        )}
        {coverage.hidden > 0 ? (
          <p role="note">
            {formatCount(coverage.hidden)} autre(s) entrée(s) de couverture non
            affichée(s).
          </p>
        ) : null}
      </section>

      {snapshot.limitations.length > 0 ? (
        <Limitations items={snapshot.limitations} />
      ) : null}
      {snapshot.limits.stopped_reason === "partial" ? (
        <WorkshopPartialState reason="Le lot de bootstrap s’est arrêté sur un résultat partiel ; les opérations enregistrées restent visibles." />
      ) : null}
    </section>
  );
}

function SnapshotIoc({ ioc }: { ioc: BootstrapSnapshotIocV1 }) {
  return (
    <li>
      <p>
        {IOC_KIND_LABELS[ioc.kind]} · <code>{ioc.value}</code> ·{" "}
        {ioc.origin === "seed" ? "graine" : "découvert"}
      </p>
      <p>
        Statut {INVOCATION_STATUS_LABELS[ioc.status]}
        {ioc.reason ? ` · ${REASON_LABELS[ioc.reason]}` : ""} ·{" "}
        {formatCount(ioc.operations.length)} opération(s) ·{" "}
        {formatCount(ioc.association_ids.length)} association(s)
        {ioc.download
          ? ` · téléchargement ${labelOf(
              DOWNLOAD_STATUS_LABELS,
              ioc.download.status,
            )}`
          : ""}
      </p>
    </li>
  );
}

function Limitations({ items }: { items: readonly string[] }) {
  if (items.length === 0) return null;
  const { visible, hidden } = bounded(items, MAX_LIMITATIONS);
  return (
    <section aria-label="Limites déclarées">
      <h4>Limites déclarées</h4>
      <ul>
        {visible.map((limitation) => (
          <li key={limitation}>{limitation}</li>
        ))}
      </ul>
      {hidden > 0 ? (
        <p role="note">
          {formatCount(hidden)} autre(s) limite(s) non affichée(s).
        </p>
      ) : null}
    </section>
  );
}
